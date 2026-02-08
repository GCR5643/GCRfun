"""Claude API 기반 메일 분류 모듈.

메일을 JUNK / NEEDS_REPLY / STALE / NORMAL 중 하나로 분류하고,
JUNK 판정 시 자동 규칙을 제안합니다.
"""

import json
import os
from dataclasses import dataclass
from typing import Optional

import anthropic

from src.fetcher import EmailMessage

CLASSIFICATION_PROMPT = """당신은 이메일 분류 전문가입니다. 아래 이메일을 분석하여 JSON으로 분류하세요.

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
- My last reply in thread: {my_last_reply}
- Body (첫 500자): {body_preview}

JSON 응답만 반환하세요:
{{
  "classification": "JUNK|NEEDS_REPLY|STALE|NORMAL",
  "confidence": 0.0-1.0,
  "reason": "판정 근거 1줄",
  "is_customer": true/false,
  "has_direct_question": true/false,
  "has_schedule_request": true/false,
  "suggested_rule": null 또는 {{"sender_pattern": "...", "subject_contains": [...]}}
}}"""

BATCH_PROMPT_HEADER = """당신은 이메일 분류 전문가입니다. 아래 이메일 {count}개를 각각 분석하여 JSON 배열로 분류하세요.

분류 기준:
- JUNK: 프로모션, 뉴스레터, 자동알림(GitHub/Jira/CI 등), 답장 불필요한 메일
- NEEDS_REPLY: 나에게 직접 질문하거나 일정/미팅을 요청한 메일로, 아직 답장하지 않은 것
- STALE: 업무/영업 관련 스레드에서 대화가 진행되다 멈춘 것 (14일 이상 무응답)
- NORMAL: 위 어디에도 해당하지 않는 일반 메일 (조치 불필요)

각 이메일에 대해 다음 형식의 JSON 객체를 반환하세요:
{{
  "message_id": "메일ID",
  "classification": "JUNK|NEEDS_REPLY|STALE|NORMAL",
  "confidence": 0.0-1.0,
  "reason": "판정 근거 1줄",
  "is_customer": true/false,
  "has_direct_question": true/false,
  "has_schedule_request": true/false,
  "suggested_rule": null 또는 {{"sender_pattern": "...", "subject_contains": [...]}}
}}

JSON 배열만 반환하세요. 설명이나 마크다운 없이 순수 JSON만:

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
    suggested_rule: Optional[dict]


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
        suggested_rule=data.get("suggested_rule"),
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


def classify_single(
    message: EmailMessage,
    model: str = "claude-sonnet-4-5-20250929",
    max_tokens: int = 300,
) -> ClassificationResult:
    """단일 메일을 Claude에 분류 요청."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    my_reply = (
        message.my_last_reply_date.strftime("%Y-%m-%d %H:%M")
        if message.my_last_reply_date
        else "없음"
    )

    prompt = CLASSIFICATION_PROMPT.format(
        sender=message.sender,
        recipients=", ".join(message.recipients),
        subject=message.subject,
        date=message.date.strftime("%Y-%m-%d %H:%M"),
        labels=", ".join(message.labels),
        thread_length=message.thread_length,
        my_last_reply=my_reply,
        body_preview=message.body_preview,
    )

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.content[0].text.strip()
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])

    data = json.loads(response_text)
    data["message_id"] = message.message_id
    return _parse_classification(data)
