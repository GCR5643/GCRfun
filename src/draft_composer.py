"""메일 드래프트 작성 모듈.

reply_type에 따라 Claude로 답장 초안을 생성하고,
Gmail 드래프트로 저장합니다. (스레드에 이어서)

드래프트는 사용자가 Gmail에서 직접 확인하고 Send해야만 발송됩니다.
"""

import base64
import json
import os
from email.mime.text import MIMEText

import anthropic

from src.classifier import ClassificationResult
from src.fetcher import EmailMessage

# reply_type별 한국어 라벨
REPLY_TYPE_LABELS = {
    "DECISION": "판단 필요",
    "SIMPLE_REPLY": "단순 답장",
    "SCHEDULE": "일정 조율",
    "INFO_SHARE": "자료 전달",
    "DELEGATE": "위임/전달",
}

DRAFT_PROMPT = """당신은 비즈니스 이메일 답장 작성 전문가입니다.
아래 이메일 스레드를 읽고, 답장 초안을 작성하세요.

작성 규칙:
- 스레드의 언어에 맞추되, 한국어 중심
- 격식체(~합니다) 기반, 스레드 내 기존 어투 유지
- 간결하고 명확하게
- 서명은 넣지 마세요 (사용자가 Gmail에서 추가)
{extra_instructions}

이메일 정보:
- From: {sender}
- Subject: {subject}
- 스레드 맥락: {body_preview}

답장 유형: {reply_type_label}
{context}

답장 본문만 반환하세요 (인사부터 시작, 서명 제외):
"""


def _build_draft_prompt(
    msg: EmailMessage,
    cls: ClassificationResult,
    extra_context: str = "",
) -> str:
    """reply_type에 맞는 드래프트 프롬프트를 생성."""
    reply_label = REPLY_TYPE_LABELS.get(cls.reply_type or "", "일반 답장")

    context_parts = []

    if cls.reply_type == "DECISION":
        context_parts.append("의사결정 옵션:")
        for i, opt in enumerate(cls.decision_options, 1):
            context_parts.append(f"  {i}. {opt}")
        context_parts.append(
            "가장 가능성 높은 옵션으로 답장을 작성하되, "
            "선택한 옵션을 명시하세요."
        )

    elif cls.reply_type == "SIMPLE_REPLY":
        context_parts.append(
            "단순 답장입니다. "
            "일정 확정 컨펌, 감사 인사, 수신 확인 등 간단한 내용으로 작성하세요."
        )

    elif cls.reply_type == "SCHEDULE":
        context_parts.append("일정 조율이 필요합니다.")
        if extra_context:
            context_parts.append(extra_context)

    elif cls.reply_type == "INFO_SHARE":
        context_parts.append("자료/파일 전달이 필요합니다.")
        if cls.suggested_file:
            context_parts.append(f"전달할 파일 추정: {cls.suggested_file}")
        context_parts.append(
            "파일을 첨부할 것임을 알리는 내용으로 작성하세요. "
            "(실제 파일은 사용자가 직접 첨부)"
        )

    elif cls.reply_type == "DELEGATE":
        context_parts.append(
            "이 건은 다른 팀원에게 전달해야 합니다. "
            "정중하게 담당자를 확인 후 연결하겠다는 내용으로 답장하세요."
        )

    extra_inst = ""
    if cls.reply_type == "DECISION" and cls.decision_options:
        extra_inst = f"- 선택한 의사결정: '{cls.decision_options[0]}' 기반으로 작성"

    return DRAFT_PROMPT.format(
        sender=msg.sender,
        subject=msg.subject,
        body_preview=msg.body_preview,
        reply_type_label=reply_label,
        context="\n".join(context_parts),
        extra_instructions=extra_inst,
    )


def generate_draft_text(
    msg: EmailMessage,
    cls: ClassificationResult,
    schedule_context: str = "",
    model: str = "claude-sonnet-4-5-20250929",
) -> str:
    """Claude로 답장 초안 텍스트를 생성.

    Args:
        msg: 원본 메일
        cls: 분류 결과
        schedule_context: 일정 관련 추가 컨텍스트 (빈 시간 등)
        model: Claude 모델 ID

    Returns:
        답장 본문 텍스트
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = _build_draft_prompt(msg, cls, schedule_context)

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text.strip()


def create_gmail_draft(
    gmail_service,
    msg: EmailMessage,
    draft_body: str,
    dry_run: bool = True,
) -> dict:
    """Gmail 드래프트를 스레드에 이어서 생성.

    Args:
        gmail_service: Gmail API 서비스 객체
        msg: 원본 메일 (thread_id, message_id, sender 등)
        draft_body: 답장 본문 텍스트
        dry_run: True면 실제 생성하지 않음

    Returns:
        {"status": ..., "draft_id": ..., "thread_id": ...}
    """
    if dry_run:
        return {
            "status": "dry_run",
            "thread_id": msg.thread_id,
            "to": msg.sender,
            "subject": msg.subject,
            "body_preview": draft_body[:100],
        }

    # MIME 메시지 생성
    mime_msg = MIMEText(draft_body, "plain", "utf-8")
    mime_msg["To"] = msg.sender
    mime_msg["Subject"] = (
        msg.subject
        if msg.subject.lower().startswith("re:")
        else f"Re: {msg.subject}"
    )
    mime_msg["In-Reply-To"] = msg.headers_raw.get("Message-ID", "")
    mime_msg["References"] = msg.headers_raw.get("Message-ID", "")

    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("utf-8")

    draft = (
        gmail_service.users()
        .drafts()
        .create(
            userId="me",
            body={
                "message": {
                    "raw": raw,
                    "threadId": msg.thread_id,
                }
            },
        )
        .execute()
    )

    return {
        "status": "created",
        "draft_id": draft["id"],
        "thread_id": msg.thread_id,
        "to": msg.sender,
        "subject": mime_msg["Subject"],
    }


def compose_and_save_drafts(
    gmail_service,
    calendar_service,
    messages: list[EmailMessage],
    classifications: list[ClassificationResult],
    model: str = "claude-sonnet-4-5-20250929",
    dry_run: bool = True,
) -> list[dict]:
    """NEEDS_REPLY 메일들에 대해 드래프트를 생성.

    Args:
        gmail_service: Gmail API
        calendar_service: Calendar API (SCHEDULE용)
        messages: 메일 리스트
        classifications: 분류 결과 리스트
        model: Claude 모델
        dry_run: 미리보기 여부

    Returns:
        생성된 드래프트 정보 리스트
    """
    from src.schedule_helper import format_schedule_options

    cls_map = {c.message_id: c for c in classifications}
    results = []

    for msg in messages:
        cls = cls_map.get(msg.message_id)
        if not cls or cls.classification != "NEEDS_REPLY" or not cls.reply_type:
            continue

        schedule_context = ""

        # SCHEDULE인 경우 캘린더 확인
        if cls.reply_type == "SCHEDULE":
            sched_info = format_schedule_options(
                calendar_service,
                proposed_time_str=cls.proposed_time,
                meeting_minutes=60,
            )
            schedule_context = sched_info["summary"]

        # 드래프트 텍스트 생성
        draft_body = generate_draft_text(msg, cls, schedule_context, model)

        # Gmail 드래프트 저장
        draft_result = create_gmail_draft(gmail_service, msg, draft_body, dry_run)
        draft_result["reply_type"] = cls.reply_type
        draft_result["reply_type_label"] = REPLY_TYPE_LABELS.get(
            cls.reply_type, "일반"
        )

        if cls.reply_type == "DECISION":
            draft_result["decision_options"] = cls.decision_options
        if cls.reply_type == "INFO_SHARE" and cls.suggested_file:
            draft_result["suggested_file"] = cls.suggested_file
        if cls.reply_type == "SCHEDULE":
            draft_result["schedule_info"] = schedule_context

        results.append(draft_result)

    return results
