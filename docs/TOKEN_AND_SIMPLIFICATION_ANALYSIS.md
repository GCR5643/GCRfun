# 레포 간소화 및 토큰 사용량 절감 분석

## 1. 요약

| 구분 | 내용 |
|------|------|
| **LLM 호출 위치** | `classifier.py` (분류 배치 + 규칙 학습), `draft_composer.py` (드래프트 생성) |
| **비용 추적** | `cost_tracker.record_usage()`가 **호출되지 않음** → 실제 비용/토큰 집계 불가 |
| **데이터 버그** | `prioritize(messages=messages + remaining)` → 메일 중복 전달 (messages만 넘겨야 함) |
| **토큰 절감 여지** | 입력: body_preview 길이, 분류용 메일 포맷, 프롬프트 문구 / 출력: max_tokens 이미 설정됨 |

---

## 2. 토큰 사용량이 발생하는 구간

### 2.1 분류 (classifier.py)

- **classify_batch**: 메일 N건을 한 번에 한 프롬프트로 전송.  
  - 입력: `BATCH_PROMPT_HEADER` + 메일당 `_format_email_for_prompt(msg)` (From, To, Subject, Date, Labels, Thread length, My last reply, **Body 500자**).
  - 출력: `max_tokens` = main에서 `500 * len(batch)` (배치당).
- **suggest_rules**: JUNK 메일 목록(From | Subject 한 줄씩)으로 규칙 제안 1회.  
  - 출력: `max_tokens=512`.

**절감 포인트**

- 메일당 본문 **500자 → 300~350자**로 제한 (fetcher 또는 포맷 단계).
- 분류용 포맷에서 **Labels** 제거 또는 한 줄 축약 (JUNK 판별에선 상대적으로 덜 중요).
- **BATCH_PROMPT_HEADER** 문구 압축 (반복 설명 축소).

### 2.2 드래프트 생성 (draft_composer.py)

- **generate_draft_text**: NEEDS_REPLY 1통당 1회 호출 (최대 `max_todos_per_run`회, 기본 10).
  - 입력: DRAFT_PROMPT + sender, subject, **body_preview 전체**.
  - 출력: `max_tokens=1024`.

**절감 포인트**

- 드래프트용으로는 본문 **300자 정도**만 넘겨도 충분. 분류에서 이미 본 메일이므로 중복 컨텍스트 최소화.

### 2.3 규칙 학습 (classifier.suggest_rules)

- JUNK 건만 입력으로 사용하고, From | Subject만 전달하므로 이미 효율적.
- `max_rules`로 제안 개수 제한되어 있음. 추가 절감은 프롬프트 문구만 약간 압축 가능.

---

## 3. 간소화 포인트

### 3.1 버그 및 동작 정리

- **prioritize 인자**: `messages`가 이미 전체 수신 메일 리스트이고, `rule_matched`와 `remaining`은 그 일부/나머지이므로, `messages + remaining`은 **remaining이 한 번 더 들어가 중복**. → **`messages`만 전달**하도록 수정.
- **비용 추적**: `cost_tracker.record_usage()`가 classifier / draft_composer 어디에서도 호출되지 않아, `python main.py cost` 및 run 종료 시 비용이 항상 0. → **API 응답의 usage를 사용해 record_usage 호출**하도록 연동.

### 3.2 구조/설정

- **규칙 엔진**: 이미 sender / keywords / enabled 중심으로 단순화되어 있음. DESIGN.md의 conditions 구조와 다르지만, 코드 기준으로 일관됨.
- **배치 크기**: `llm.batch_size` (기본 5)로 분류 호출 횟수 제어 가능. 메일 많으면 배치만 키우면 됨 (한 번에 보내는 입력 토큰은 늘어남).
- **body_preview 길이**: 설정 항목(예: `llm.body_preview_chars`)으로 두면, 분류/드래프트 간 다른 길이도 가능. 선택 사항.

### 3.3 문서/중복

- DESIGN.md와 실제 rules 구조(rules_engine) 차이는 있으나, 코드가 단순 규칙으로 통일되어 있어 유지해도 됨. 필요 시 DESIGN.md만 실제 구조에 맞게 정리하면 됨.

---

## 4. 적용 권장 사항 (우선순위)

1. **필수**: `prioritize(messages=messages + remaining, ...)` → `prioritize(messages=messages, ...)` 로 수정.
2. **필수**: classifier의 `classify_batch` / `suggest_rules`와 draft_composer의 `generate_draft_text`에서 API 호출 직후 **response.usage**로 `cost_tracker.record_usage()` 호출.
3. **권장**: `body_preview` 500자 → 350자 제한 (fetcher 또는 분류/드래프트 입력 포맷 단계).
4. **권장**: 분류용 `_format_email_for_prompt`에서 Labels 생략 또는 한 줄로 축약.
5. **선택**: BATCH_PROMPT_HEADER, RULE_SUGGEST_PROMPT 문구 압축; 드래프트 프롬프트에 넘기는 본문 길이 300자로 제한.

---

## 5. 예상 토큰/비용 영향 (참고)

- **body_preview 500→350자**: 메일당 입력 약 150자(한글 기준 대략 100~200 토큰) 절감.  
  하루 20통 분류 시 약 2,000~4,000 입력 토큰/일 절감.
- **Labels 제거**: 메일당 수십 토큰 수준 절감.
- **실제 비용 가시화**: record_usage 연동 후 `main.py cost` 및 run 종료 시 추정 비용이 정상 표시됨.

이 문서는 위 권장 사항을 반영한 코드 수정과 함께 활용할 수 있습니다.
