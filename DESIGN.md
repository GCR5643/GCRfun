# Email Task Automation — 설계 문서

## 1. 프로젝트 개요

Gmail 받은편지함을 분석하여 다음 3가지 자동화를 수행하는 로컬 Python CLI 도구.

| 기능 | 설명 |
|------|------|
| **Auto-Read** | 쓸모없는 메일(프로모션, 자동알림 등)을 자동 읽음 처리 |
| **Todo-Inject** | 4 영업일 이상 미답변한 고객 질문/일정 요청을 Google Calendar 투두로 등록 |
| **Re-engage** | 흐지부지된 영업/업무 스레드를 별도 리포트로 정리 |

---

## 2. 아키텍처

```
gmail_task_automation/
│
├── config/
│   ├── settings.yaml          # 전역 설정 (개수 제한, 기간, 캘린더 ID 등)
│   ├── rules.yaml             # LLM이 생성한 자동읽음 규칙 (사용자 수정 가능)
│   └── credentials.json       # Google OAuth 클라이언트 (gitignore)
│
├── src/
│   ├── __init__.py
│   ├── auth.py                # Google OAuth2 인증 (Gmail + Calendar)
│   ├── fetcher.py             # Gmail API로 메일 조회
│   ├── classifier.py          # Claude API로 메일 분류
│   ├── rules_engine.py        # 규칙 기반 필터 (YAML 로드/저장/매칭)
│   ├── prioritizer.py         # 미답변 메일 우선순위 스코어링
│   ├── actions.py             # 읽음 처리, 캘린더 등록, 리포트 생성
│   ├── calendar_client.py     # Google Calendar API 클라이언트
│   └── cost_tracker.py        # API 호출 토큰 사용량 추적
│
├── output/
│   └── (런타임에 생성되는 리포트 파일)
│
├── main.py                    # CLI 진입점
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## 3. 데이터 흐름

```
[1] Gmail API: 미읽은 메일 + 최근 30일 스레드 가져오기
         │
         ▼
[2] Rules Engine: 기존 YAML 규칙으로 1차 필터링
    ├─ 매칭됨 → 즉시 "쓸모없음" 분류
    └─ 매칭 안됨 → Claude 분류로 전달
         │
         ▼
[3] Claude Classifier: 메일 분류 (3-버킷)
    ├─ JUNK      → Auto-Read 대상
    ├─ NEEDS_REPLY → 미답변 후보
    └─ STALE     → 재연락 후보
         │
         ▼
[4] Rule Learning: JUNK 판정 메일의 패턴 → rules.yaml에 새 규칙 추가
         │
         ▼
[5] Prioritizer: NEEDS_REPLY 메일을 스코어링
    - 최신순 가중치 (0.4)
    - 고객 여부 가중치 (0.3)
    - 질문/일정요청 유형 가중치 (0.2)
    - 스레드 길이 가중치 (0.1)
         │
         ▼
[6] Actions:
    ├─ JUNK → Gmail API: 읽음 처리 (batch)
    ├─ NEEDS_REPLY (상위 N개) → Calendar API: 투두 이벤트 생성
    └─ STALE → output/reengage_report_{date}.md 생성
```

---

## 4. 핵심 설계 결정

### 4.1 OAuth 토큰 관리 (쉽게 설명)

Google API를 쓰려면 "나 대신 내 메일을 읽어도 좋다"는 허가증이 필요합니다.

1. Google Cloud Console에서 프로젝트를 만들고 `credentials.json`을 다운로드
2. 최초 실행 시 브라우저가 열리고, 회사 Google 계정으로 로그인하여 권한 승인
3. 승인 후 `token.json`이 로컬에 저장됨 (이것이 "허가증")
4. `token.json`은 자동 갱신되므로 이후에는 브라우저 열 필요 없음
5. `credentials.json`과 `token.json`은 `.gitignore`로 Git에 올라가지 않음

### 4.2 규칙 엔진 (rules.yaml)

LLM이 메일을 JUNK로 판정할 때마다, 그 판정 근거를 규칙으로 추출하여 저장합니다.

```yaml
# rules.yaml 예시
rules:
  - id: rule_001
    name: "프로모션 뉴스레터"
    created_at: "2026-02-08"
    source: "llm_generated"      # llm_generated | manual
    enabled: true
    conditions:
      sender_pattern: ".*@marketing\\.example\\.com"
      subject_contains: ["할인", "프로모션", "뉴스레터", "구독"]
      has_unsubscribe_header: true
    action: "mark_read"
    match_count: 0               # 몇 번 매칭되었는지 추적

  - id: rule_002
    name: "GitHub 알림"
    created_at: "2026-02-08"
    source: "llm_generated"
    enabled: true
    conditions:
      sender_pattern: "notifications@github\\.com"
      label_is: ["notifications"]
    action: "mark_read"
    match_count: 0

  - id: rule_003
    name: "Jira 자동 알림"
    created_at: "2026-02-09"
    source: "manual"             # 사용자가 직접 추가한 규칙
    enabled: true
    conditions:
      sender_pattern: "jira@.*\\.atlassian\\.net"
    action: "mark_read"
    match_count: 0
```

사용자는 이 파일을 직접 열어서:
- `enabled: false`로 규칙 비활성화
- `conditions` 수정
- 새 규칙 수동 추가 (`source: manual`)
- `match_count`로 규칙이 실제로 쓰이는지 확인

### 4.3 캘린더 투두 개수 제한

- **기본값: 1회 실행당 최대 10개**
- 우선순위 스코어 상위 N개만 등록
- 이미 등록된 메일은 중복 등록하지 않음 (message_id 기반 추적)
- 설정 파일에서 `max_todos_per_run` 으로 조정 가능

### 4.4 영업일 계산

- Python `numpy.busday_count` 또는 경량 라이브러리 사용
- 한국 공휴일 반영 옵션 (기본 OFF, settings.yaml에서 ON)

### 4.5 재연락(Re-engage) 판정 기준

| 조건 | 설명 |
|------|------|
| 스레드 길이 ≥ 2 | 최소 1회 이상 주고받은 대화 |
| 마지막 메일 후 14일+ 경과 | 대화가 멈춘 것으로 판단 |
| 스레드 라벨이 업무/영업 관련 | Claude가 스레드 컨텍스트로 판단 |
| 마지막 발신자 무관 | 내가 안 답한 것 + 고객이 안 답한 것 모두 포함 |

---

## 5. Claude API 분류 프롬프트 설계

### 5.1 메일 분류 프롬프트

```
당신은 이메일 분류 전문가입니다. 아래 이메일을 분석하여 JSON으로 분류하세요.

분류 기준:
- JUNK: 프로모션, 뉴스레터, 자동알림(GitHub/Jira/CI 등), 답장 불필요한 메일
- NEEDS_REPLY: 나에게 직접 질문하거나 일정/미팅을 요청한 메일로, 아직 답장하지 않은 것
- STALE: 업무/영업 관련 스레드에서 대화가 진행되다 멈춘 것 (14일 이상 무응답)
- NORMAL: 위 어디에도 해당하지 않는 일반 메일 (조치 불필요)

이메일 정보:
- From: {sender}
- To: {recipients}
- Subject: {subject}
- Date: {date}
- Labels: {labels}
- Thread length: {thread_length}
- My last reply in thread: {my_last_reply_date or "없음"}
- Body (첫 500자): {body_preview}

JSON 응답 형식:
{
  "classification": "JUNK|NEEDS_REPLY|STALE|NORMAL",
  "confidence": 0.0-1.0,
  "reason": "판정 근거 1줄",
  "is_customer": true/false,
  "has_direct_question": true/false,
  "has_schedule_request": true/false,
  "suggested_rule": null 또는 {"sender_pattern": "...", "subject_contains": [...]}
}
```

### 5.2 배치 처리

- 한 번에 최대 5개 메일을 묶어서 분류 요청 (토큰 효율)
- Rules Engine에서 이미 걸러진 메일은 Claude에 보내지 않음 (비용 절감)

---

## 6. API 비용 추정

### 가정
- 하루 수신 메일: ~50통
- 월 영업일: 22일
- Rules Engine이 약 60%를 1차 필터링 → Claude 분류 대상: ~20통/일

### Claude API (claude-sonnet-4-5-20250929 권장)
| 항목 | 수치 |
|------|------|
| 분류 대상 메일/월 | 20 × 22 = 440통 |
| 입력 토큰/메일 | ~800 토큰 (프롬프트 + 메일 본문) |
| 출력 토큰/메일 | ~150 토큰 (JSON 응답) |
| 총 입력 토큰/월 | 440 × 800 = 352,000 |
| 총 출력 토큰/월 | 440 × 150 = 66,000 |
| Sonnet 가격 | 입력 $3/M, 출력 $15/M |
| **월 예상 비용** | **$1.06 + $0.99 = ~$2.05/월** |

### 규칙 학습이 진행될수록
- 1개월차: ~$2/월 (규칙 적음, Claude 의존도 높음)
- 3개월차: ~$0.80/월 (규칙이 70-80% 커버)
- 6개월차: ~$0.40/월 (규칙이 90%+ 커버)

### Google API
- Gmail API, Calendar API 모두 **무료 tier** 내에서 충분 (일 250 quota units)

> **결론: 월 $1~2 수준으로 매우 저렴. Sonnet 사용 권장.**

---

## 7. 설정 파일 구조 (settings.yaml)

```yaml
# 이메일 조회 설정
email:
  max_results: 100              # 1회 실행 시 최대 조회 메일 수
  lookback_days: 30             # 최근 N일 내 메일만 대상
  my_email: "you@company.com"   # 내 회사 이메일 주소

# 미답변 판정
unanswered:
  working_days_threshold: 4     # N 영업일 이상 미답변
  include_korean_holidays: false

# 캘린더 투두
calendar:
  calendar_id: "primary"        # 또는 특정 캘린더 ID
  max_todos_per_run: 10         # 1회 실행당 최대 투두 생성 수
  todo_prefix: "[메일투두]"      # 캘린더 이벤트 제목 접두사
  default_duration_minutes: 30  # 투두 이벤트 기본 길이

# 재연락 판정
reengage:
  stale_days: 14                # N일 이상 무응답이면 흐지부지 판정
  min_thread_length: 2          # 최소 스레드 길이
  output_format: "markdown"     # markdown | json | csv

# 우선순위 가중치 (합계 = 1.0)
priority:
  recency_weight: 0.4           # 최신 메일일수록 높은 점수
  customer_weight: 0.3          # 고객 발신 여부
  request_type_weight: 0.2      # 질문/일정요청 유형
  thread_depth_weight: 0.1      # 스레드가 길수록 중요

# 고객 도메인 목록 (이 도메인에서 온 메일 = 고객)
customer_domains:
  - "client-a.com"
  - "client-b.co.kr"
  # 여기에 고객사 도메인 추가

# Claude API
llm:
  model: "claude-sonnet-4-5-20250929"
  max_tokens: 300
  batch_size: 5                 # 한 번에 분류할 메일 수

# 실행 모드
execution:
  dry_run: true                 # true면 실제 변경 없이 미리보기만
  verbose: true                 # 상세 로그 출력
```

---

## 8. 향후 자동화 방안

| 방식 | 설명 | 적합 시점 |
|------|------|-----------|
| **cron (로컬)** | `0 9 * * 1-5` — 평일 오전 9시 자동 실행 | 즉시 가능 |
| **systemd timer** | cron 대안, 로그 관리 용이 | 로컬 안정화 후 |
| **Google Cloud Functions** | 서버리스, Gmail Push Notification 연동 | 실시간 처리 필요 시 |
| **Cloud Scheduler + Cloud Run** | 스케줄 기반 컨테이너 실행 | 팀 공유 필요 시 |

---

## 9. 1차 실행 시나리오

```bash
# 1. 초기 설정
cp .env.example .env            # API 키 입력
cp config/settings.yaml.example config/settings.yaml  # 설정 수정

# 2. OAuth 인증 (최초 1회)
python main.py auth

# 3. 드라이런 (실제 변경 없이 미리보기)
python main.py run --dry-run

# 4. 실행
python main.py run

# 5. 규칙 관리
python main.py rules list       # 현재 규칙 조회
python main.py rules disable rule_003  # 규칙 비활성화
python main.py rules stats      # 규칙별 매칭 통계
```
