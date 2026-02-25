"""실행 액션 모듈.

분류 결과에 따라 실제 동작을 수행합니다:
- JUNK → Gmail 읽음 처리
- STALE → 재연락 리포트 생성
"""

import logging
from datetime import datetime
from pathlib import Path

from src.classifier import ClassificationResult
from src.fetcher import EmailMessage

logger = logging.getLogger(__name__)


def mark_as_read(
    gmail_service,
    messages: list[EmailMessage],
    dry_run: bool = True,
) -> list[dict]:
    """JUNK 메일들을 읽음 처리 (UNREAD 라벨 제거).

    Args:
        gmail_service: 인증된 Gmail API 서비스 객체
        messages: 읽음 처리할 메일 리스트
        dry_run: True면 실제 처리하지 않음

    Returns:
        처리 결과 리스트
    """
    results = []

    for msg in messages:
        if not msg.is_unread:
            continue

        if dry_run:
            results.append(
                {
                    "status": "dry_run",
                    "message_id": msg.message_id,
                    "subject": msg.subject,
                    "sender": msg.sender,
                }
            )
        else:
            try:
                gmail_service.users().messages().modify(
                    userId="me",
                    id=msg.message_id,
                    body={"removeLabelIds": ["UNREAD"]},
                ).execute()
                results.append(
                    {
                        "status": "marked_read",
                        "message_id": msg.message_id,
                        "subject": msg.subject,
                        "sender": msg.sender,
                    }
                )
            except Exception as e:
                logger.error("읽음 처리 실패: %s (id=%s)", e, msg.message_id)
                results.append(
                    {
                        "status": "error",
                        "message_id": msg.message_id,
                        "error": str(e),
                    }
                )

    return results


def generate_reengage_report(
    messages: list[EmailMessage],
    classifications: list[ClassificationResult],
    output_format: str = "markdown",
) -> str:
    """재연락 대상 메일을 리포트로 생성.

    Args:
        messages: 메일 리스트
        classifications: 분류 결과 리스트
        output_format: 출력 형식 (markdown, json, csv)

    Returns:
        생성된 파일 경로
    """
    cls_map = {c.message_id: c for c in classifications}
    stale_items = []

    for msg in messages:
        cls = cls_map.get(msg.message_id)
        if not cls or cls.classification != "STALE":
            continue
        stale_items.append((msg, cls))

    if not stale_items:
        return ""

    # 날짜 내림차순 정렬
    stale_items.sort(key=lambda x: x[0].date, reverse=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)

    if output_format == "markdown":
        return _write_markdown(stale_items, output_dir, timestamp)
    elif output_format == "json":
        return _write_json(stale_items, output_dir, timestamp)
    elif output_format == "csv":
        return _write_csv(stale_items, output_dir, timestamp)
    else:
        return _write_markdown(stale_items, output_dir, timestamp)


def _write_markdown(
    items: list[tuple[EmailMessage, ClassificationResult]],
    output_dir: Path,
    timestamp: str,
) -> str:
    """마크다운 리포트 생성."""
    filepath = output_dir / f"reengage_report_{timestamp}.md"

    lines = [
        f"# 재연락 대상 리포트",
        f"",
        f"생성일: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"총 {len(items)}건",
        f"",
        "---",
        "",
    ]

    for i, (msg, cls) in enumerate(items, 1):
        last_reply = (
            msg.my_last_reply_date.strftime("%Y-%m-%d")
            if msg.my_last_reply_date
            else "답장 없음"
        )

        lines.extend(
            [
                f"## {i}. {msg.subject}",
                f"",
                f"| 항목 | 내용 |",
                f"|------|------|",
                f"| 발신자 | {msg.sender} |",
                f"| 수신일 | {msg.date.strftime('%Y-%m-%d')} |",
                f"| 스레드 길이 | {msg.thread_length}건 |",
                f"| 내 마지막 답장 | {last_reply} |",
                f"| 판정 사유 | {cls.reason} |",
                f"",
                f"> {msg.snippet}",
                f"",
                "---",
                "",
            ]
        )

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return str(filepath)


def _write_json(
    items: list[tuple[EmailMessage, ClassificationResult]],
    output_dir: Path,
    timestamp: str,
) -> str:
    """JSON 리포트 생성."""
    import json

    filepath = output_dir / f"reengage_report_{timestamp}.json"

    data = []
    for msg, cls in items:
        data.append(
            {
                "subject": msg.subject,
                "sender": msg.sender,
                "date": msg.date.isoformat(),
                "thread_length": msg.thread_length,
                "my_last_reply": (
                    msg.my_last_reply_date.isoformat()
                    if msg.my_last_reply_date
                    else None
                ),
                "reason": cls.reason,
                "snippet": msg.snippet,
            }
        )

    filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return str(filepath)


def _write_csv(
    items: list[tuple[EmailMessage, ClassificationResult]],
    output_dir: Path,
    timestamp: str,
) -> str:
    """CSV 리포트 생성."""
    import csv
    import io

    filepath = output_dir / f"reengage_report_{timestamp}.csv"

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Subject", "Sender", "Date", "Thread Length", "My Last Reply", "Reason"]
    )

    for msg, cls in items:
        last_reply = (
            msg.my_last_reply_date.strftime("%Y-%m-%d")
            if msg.my_last_reply_date
            else ""
        )
        writer.writerow(
            [
                msg.subject,
                msg.sender,
                msg.date.strftime("%Y-%m-%d"),
                msg.thread_length,
                last_reply,
                cls.reason,
            ]
        )

    filepath.write_text(output.getvalue(), encoding="utf-8")
    return str(filepath)
