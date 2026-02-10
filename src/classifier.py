"""Claude API 기반 메일 분류 모듈.

1차: JUNK / NEEDS_REPLY / STALE / NORMAL 분류
2차: NEEDS_REPLY에 대해 reply_type 서브 분류
  - DECISION: 내 판단이 필요 (의사결정 옵션 3개 제시)
  - SIMPLE_REPLY: 단순 답장 (컨펌, 감사 인사 등)
  - SCHEDULE: 일정 잡기 (캘린더 확인 필요)
  - INFO_SHARE: 정보/파일 전달 필요
  - DELEGATE: 팀원에게 위임/전달
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional

import anthropic

from src.fetcher import EmailMessage

BATCH_PROMPT_HEADER = """이메일 {count}개를 분류하세요. JSON 배열만 반환.

분류:
- JUNK: 프로모션, 뉴스레터, 자동알림, 답장 불필요
- NEEDS_REPLY: 나에게 직접 질문 또는 일정/미팅 요청, 미답장
- STALE: 업무/영업 스레드가 14일+ 무응답
- NORMAL: 조치 불필요

NEEDS_REPLY인 경우 reply_type도 판정:
- DECISION: 내 판단/의사결정이 필요한 요청 (제안 수락, 가격 협상, 방향 결정 등)
- SIMPLE_REPLY: 단순 답장 (일정 컨펌, 감사 인사, 수신 확인 등)
- SCHEDULE: 미팅/일정을 잡아야 하는 요청
- INFO_SHARE: 자료/파일을 보내야 하는 요청
- DELEGATE: 내가 아닌 다른 팀원이 답해야 하는 건

형식:
{{"message_id": "ID", "classification": "JUNK|NEEDS_REPLY|STALE|NORMAL", "reply_type": "DECISION|SIMPLE_REPLY|SCHEDULE|INFO_SHARE|DELEGATE|null", "confidence": 0.0-1.0, "reason": "1줄", "is_customer": bool, "has_direct_question": bool, "has_schedule_request": bool, "decision_options": ["옵션A", "옵션B", "옵션C"] 또는 null, "suggested_file": "파일명" 또는 null, "proposed_time": "상대가 제안한 시간" 또는 null}}

JSON 배열만:

"""

RULE_SUGGEST_PROMPT = """아래는 JUNK으로 분류된 이메일 발신자와 제목 목록입니다.
이 메일들을 자동 필터링할 수 있는 간단한 규칙을 제안해주세요.

규칙은 2가지 필드로만 구성됩니다:
- sender: 발신자 주소에 포함된 문자열 (예: "@github.com", "noreply@")
- keywords: 제목에 포함된 키워드 리스트 (선택사항)

비슷한 메일은 하나의 규칙으로 통합하세요. 최대 {max_rules}개.
JSON 배열만 반환하세요:

[{{"sender": "@example.com"}}, {{"sender": "noreply@", "keywords": ["알림"]}}]

JUNK 메일 목록:
{junk_list}

JSON 배열만:
"""


@dataclass
class ClassificationResult:
    """분류 결과 데이터 클래스."""

    message_id: str
    classification: str  # JUNK, NEEDS_REPLY, STALE, NORMAL
    confidence: float
    reason: str
    is_customer: bool
    has_direct_question: bool
    has_schedule_request: bool
    reply_type: Optional[str] = None  # DECISION, SIMPLE_REPLY, SCHEDULE, INFO_SHARE, DELEGATE
    decision_options: list[str] = field(default_factory=list)
    suggested_file: Optional[str] = None
    proposed_time: Optional[str] = None


def _format_email_for_prompt(msg: EmailMessage) -> str:
    """메일을 프롬프트에 삽입할 텍스트로 포맷."""
    my_reply = (
        msg.my_last_reply_date.strftime("%Y-%m-%d %H:%M")
        if msg.my_last_reply_date
        else "없음"
    )
    return (
        f"[Message ID: {msg.message_id}]\n"
        f"- From: {msg.sender}\n"
        f"- To: {', '.join(msg.recipients)}\n"
        f"- Subject: {msg.subject}\n"
        f"- Date: {msg.date.strftime('%Y-%m-%d %H:%M')}\n"
        f"- Labels: {', '.join(msg.labels)}\n"
        f"- Thread length: {msg.thread_length}\n"
        f"- My last reply: {my_reply}\n"
        f"- Body: {msg.body_preview}\n"
    )


def _parse_classification(data: dict) -> ClassificationResult:
    """JSON dict를 ClassificationResult로 변환."""
    return ClassificationResult(
        message_id=data.get("message_id", ""),
        classification=data.get("classification", "NORMAL"),
        confidence=float(data.get("confidence", 0.5)),
        reason=data.get("reason", ""),
        is_customer=bool(data.get("is_customer", False)),
        has_direct_question=bool(data.get("has_direct_question", False)),
        has_schedule_request=bool(data.get("has_schedule_request", False)),
        reply_type=data.get("reply_type"),
        decision_options=data.get("decision_options") or [],
        suggested_file=data.get("suggested_file"),
        proposed_time=data.get("proposed_time"),
    )


def classify_batch(
    messages: list[EmailMessage],
    model: str = "claude-sonnet-4-5-20250929",
    max_tokens: int = 2048,
) -> list[ClassificationResult]:
    """메일 리스트를 배치로 Claude에 분류 요청.

    Args:
        messages: 분류 대상 메일 리스트
        model: Claude 모델 ID
        max_tokens: 최대 출력 토큰

    Returns:
        ClassificationResult 리스트
    """
    if not messages:
        return []

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = BATCH_PROMPT_HEADER.format(count=len(messages))
    for msg in messages:
        prompt += _format_email_for_prompt(msg) + "\n---\n"

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.content[0].text.strip()

    # JSON 파싱 (```json``` 블록이 있을 수 있으므로 정리)
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])

    parsed = json.loads(response_text)

    if isinstance(parsed, dict):
        parsed = [parsed]

    results = []
    for i, item in enumerate(parsed):
        if "message_id" not in item and i < len(messages):
            item["message_id"] = messages[i].message_id
        results.append(_parse_classification(item))

    return results


def suggest_rules(
    junk_messages: list[EmailMessage],
    model: str = "claude-sonnet-4-5-20250929",
    max_rules: int = 5,
) -> list[dict]:
    """JUNK 메일 목록을 보고 통합된 필터 규칙을 제안 (1회 호출).

    Args:
        junk_messages: JUNK으로 분류된 메일 리스트
        model: Claude 모델 ID
        max_rules: 최대 제안 규칙 수

    Returns:
        [{"sender": "...", "keywords": [...]}] 형태의 규칙 리스트
    """
    if not junk_messages:
        return []

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    junk_list = "\n".join(
        f"- From: {m.sender} | Subject: {m.subject}" for m in junk_messages
    )

    prompt = RULE_SUGGEST_PROMPT.format(max_rules=max_rules, junk_list=junk_list)

    response = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.content[0].text.strip()
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])

    parsed = json.loads(response_text)
    if isinstance(parsed, dict):
        parsed = [parsed]

    return parsed
