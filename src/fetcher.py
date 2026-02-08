"""Gmail 메일 조회 모듈.

Gmail API를 사용하여 메일을 가져오고,
스레드 정보와 함께 분류에 필요한 메타데이터를 추출합니다.
"""

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Optional

from dateutil import parser as date_parser


@dataclass
class EmailMessage:
    """분류/처리에 필요한 메일 정보를 담는 데이터 클래스."""

    message_id: str
    thread_id: str
    sender: str
    sender_domain: str
    recipients: list[str]
    subject: str
    date: datetime
    labels: list[str]
    snippet: str
    body_preview: str
    has_unsubscribe_header: bool
    thread_length: int = 1
    my_last_reply_date: Optional[datetime] = None
    is_unread: bool = True
    headers_raw: dict = field(default_factory=dict)


def _extract_domain(email_addr: str) -> str:
    """이메일 주소에서 도메인 추출."""
    _, addr = parseaddr(email_addr)
    match = re.search(r"@([\w.-]+)", addr)
    return match.group(1).lower() if match else ""


def _decode_body(payload: dict) -> str:
    """메일 payload에서 텍스트 본문을 디코딩하여 반환 (최대 500자)."""
    body_text = ""

    if payload.get("body", {}).get("data"):
        raw = payload["body"]["data"]
        body_text = base64.urlsafe_b64decode(raw).decode("utf-8", errors="replace")
    elif payload.get("parts"):
        for part in payload["parts"]:
            mime = part.get("mimeType", "")
            if mime == "text/plain" and part.get("body", {}).get("data"):
                raw = part["body"]["data"]
                body_text = base64.urlsafe_b64decode(raw).decode(
                    "utf-8", errors="replace"
                )
                break
            if mime.startswith("multipart/") and part.get("parts"):
                body_text = _decode_body(part)
                if body_text:
                    break

    return body_text[:500]


def _get_header(headers: list[dict], name: str) -> str:
    """헤더 리스트에서 특정 헤더 값을 추출."""
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def fetch_messages(
    gmail_service,
    my_email: str,
    max_results: int = 100,
    lookback_days: int = 30,
) -> list[EmailMessage]:
    """메일을 가져와서 EmailMessage 리스트로 반환.

    Args:
        gmail_service: 인증된 Gmail API 서비스 객체
        my_email: 내 이메일 주소
        max_results: 최대 조회 수
        lookback_days: 조회 범위 (최근 N일)

    Returns:
        EmailMessage 리스트 (내가 보낸 메일 제외)
    """
    after_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
        "%Y/%m/%d"
    )
    query = f"after:{after_date}"

    results = (
        gmail_service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )

    raw_messages = results.get("messages", [])
    messages = []

    # 스레드별 메시지 수 및 내 마지막 답장 추적
    thread_counts: dict[str, int] = {}
    thread_my_replies: dict[str, datetime] = {}

    for raw in raw_messages:
        msg = (
            gmail_service.users()
            .messages()
            .get(userId="me", id=raw["id"], format="full")
            .execute()
        )

        headers = msg.get("payload", {}).get("headers", [])
        sender = _get_header(headers, "From")
        thread_id = msg.get("threadId", "")

        thread_counts[thread_id] = thread_counts.get(thread_id, 0) + 1

        # 내가 보낸 메일이면 답장 일시만 추적하고 분류 대상에서 제외
        sender_addr = parseaddr(sender)[1].lower()
        if sender_addr == my_email.lower():
            msg_date_str = _get_header(headers, "Date")
            if msg_date_str:
                try:
                    msg_date = date_parser.parse(msg_date_str)
                    existing = thread_my_replies.get(thread_id)
                    if not existing or msg_date > existing:
                        thread_my_replies[thread_id] = msg_date
                except (ValueError, TypeError):
                    pass
            continue

        date_str = _get_header(headers, "Date")
        try:
            parsed_date = date_parser.parse(date_str)
        except (ValueError, TypeError):
            parsed_date = datetime.now(timezone.utc)

        labels = msg.get("labelIds", [])
        body_preview = _decode_body(msg.get("payload", {}))
        has_unsub = bool(_get_header(headers, "List-Unsubscribe"))

        recipients_str = _get_header(headers, "To")
        recipients = [
            addr.strip() for addr in recipients_str.split(",") if addr.strip()
        ]

        email_msg = EmailMessage(
            message_id=msg["id"],
            thread_id=thread_id,
            sender=sender,
            sender_domain=_extract_domain(sender),
            recipients=recipients,
            subject=_get_header(headers, "Subject"),
            date=parsed_date,
            labels=labels,
            snippet=msg.get("snippet", ""),
            body_preview=body_preview,
            has_unsubscribe_header=has_unsub,
            is_unread="UNREAD" in labels,
            headers_raw={h["name"]: h["value"] for h in headers},
        )

        messages.append(email_msg)

    # 스레드 정보 보강
    for msg in messages:
        msg.thread_length = thread_counts.get(msg.thread_id, 1)
        msg.my_last_reply_date = thread_my_replies.get(msg.thread_id)

    return messages
