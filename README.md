# Email Task Automation

Gmail 받은편지함을 자동 분류하여 불필요한 메일은 읽음 처리하고, 미답변 고객 메일은 Google Calendar 투두로 등록하며, 흐지부지된 영업 대화를 리포트로 정리하는 CLI 도구입니다.

## 주요 기능

- **Auto-Read**: 프로모션, 자동알림 등 쓸모없는 메일을 자동 읽음 처리
- **Todo-Inject**: 4 영업일 이상 미답변한 고객 질문/일정 요청을 Google Calendar 투두로 등록
- **Re-engage**: 대화가 멈춘 영업/업무 스레드를 별도 리포트로 정리

## 설치

```bash
pip install -r requirements.txt
```

## 초기 설정

### 1. Google Cloud 프로젝트 설정

1. [Google Cloud Console](https://console.cloud.google.com/)에서 프로젝트 생성
2. **Gmail API**, **Google Calendar API**, **Google Docs API** 활성화
3. OAuth 2.0 클라이언트 ID 생성 (데스크톱 앱)
4. `credentials.json` 다운로드 → `config/` 폴더에 저장

### 2. 설정 파일 준비

```bash
cp .env.example .env
cp config/settings.yaml.example config/settings.yaml
cp config/rules.yaml.example config/rules.yaml
```

`.env`에 Anthropic API 키를 입력하고, `config/settings.yaml`에서 이메일 주소와 고객 도메인 목록을 수정하세요.

### 3. OAuth 인증

```bash
python main.py auth
```

브라우저가 열리면 회사 Google 계정으로 로그인하여 권한을 승인합니다.

## 사용법

```bash
# 미리보기 (실제 변경 없음)
python main.py run --dry-run

# 실행 (메일 읽음 처리 + 캘린더 투두 생성)
python main.py run --no-dry-run

# 규칙 관리
python main.py rules list              # 규칙 목록
python main.py rules disable rule_001  # 규칙 비활성화
python main.py rules stats             # 매칭 통계

# API 비용 확인
python main.py cost
```

## 자동 실행 시 Google Docs 로그

cron 등으로 `run`을 자동 실행할 때, 실행 결과를 **Google Docs** 한 문서에 계속 쌓이게 할 수 있습니다.

1. [Google Docs](https://docs.google.com/)에서 새 문서를 하나 만듭니다 (제목 예: "Email Automation 실행 로그").
2. URL에서 **문서 ID**를 복사합니다.  
   `https://docs.google.com/document/d/여기가_문서_ID/edit` → `여기가_문서_ID` 부분만 복사.
3. `config/settings.yaml`에 다음을 추가/수정합니다.

```yaml
docs_log:
  enabled: true
  document_id: "복사한_문서_ID"
```

4. **최초 1회**: 스코프가 추가되었으므로 `python main.py auth`를 다시 실행해 Docs 권한을 승인합니다.
5. 이후 `python main.py run`이 실행될 때마다 해당 문서 **맨 끝**에 실행 시각과 요약(메일 건수, 분류 결과, 드래프트/투두 건수, 비용 등)이 자동으로 추가됩니다.

## 프로젝트 구조

```
├── main.py                  # CLI 진입점
├── config/
│   ├── settings.yaml        # 전역 설정
│   ├── rules.yaml           # 자동 읽음 규칙 (LLM 생성 + 수동)
│   └── credentials.json     # Google OAuth (gitignore)
├── src/
│   ├── auth.py              # Google OAuth2 인증
│   ├── fetcher.py           # Gmail 메일 조회
│   ├── classifier.py        # Claude AI 메일 분류
│   ├── rules_engine.py      # 규칙 기반 필터
│   ├── prioritizer.py       # 미답변 메일 우선순위
│   ├── calendar_client.py   # Google Calendar 투두 생성
│   ├── actions.py           # 읽음 처리, 리포트 생성
│   ├── cost_tracker.py      # API 비용 추적
│   └── docs_logger.py       # Google Docs 실행 로그 기록
└── output/                  # 리포트 출력 (gitignore)
```

자세한 설계 문서는 [DESIGN.md](DESIGN.md)를 참고하세요.
