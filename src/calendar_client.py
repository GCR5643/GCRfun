"""Google Calendar 투두 생성 모듈.

미답변 메일을 Google Calendar 이벤트(투두)로 등록합니다.
중복 등록을 방지하기 위해 message_id를 이벤트 description에 포함합니다.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.prioritizer import PrioritizedEmail

# 이미 등록된 메일 ID를 추적하는 파일
TRACKING_FILE = "output/calendar_tracking.json"


def _load_tracking() -> set[str]:
    """이미 캘린더에 등록된 message_id 집합을 로드."""
    p = Path(TRACKING_FILE)
    if not p.exists():
        return set()
    data = json.loads(p.read_text())
    return set(data.get("registered_ids", []))


def _save_tracking(ids: set[str]) -> None:
    """등록된 message_id 집합을 저장."""
    p = Path(TRACKING_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"registered_ids": sorted(ids)}, indent=2))


def _build_event(
    email: PrioritizedEmail,
    todo_prefix: str,
    duration_minutes: int,
) -> dict:
    """PrioritizedEmail을 Google Calendar 이벤트 dict로 변환."""
    msg = email.message
    cls = email.classification

    summary = f"{todo_prefix} {msg.subject}"
    if len(summary) > 100:
        summary = summary[:97] + "..."

    description_lines = [
        f"발신자: {msg.sender}",
        f"수신일: {msg.date.strftime('%Y-%m-%d %H:%M')}",
        f"우선순위 점수: {email.score}",
        f"판정 사유: {cls.reason}",
        "",
        "--- 메일 미리보기 ---",
        msg.snippet,
        "",
        f"[message_id: {msg.message_id}]",
    ]

    # 투두 시간: 오늘(또는 다음 영업일) 오전 9시, 기본 30분
    now = datetime.now(timezone.utc)
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if start < now:
        start += timedelta(days=1)
    # 주말 건너뛰기
    while start.weekday() >= 5:
        start += timedelta(days=1)

    end = start + timedelta(minutes=duration_minutes)

    return {
        "summary": summary,
        "description": "\n".join(description_lines),
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": "Asia/Seoul",
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": "Asia/Seoul",
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 10},
            ],
        },
        "colorId": "11",  # 빨간색 (중요)
    }


def create_todos(
    calendar_service,
    emails: list[PrioritizedEmail],
    calendar_id: str = "primary",
    todo_prefix: str = "[메일투두]",
    duration_minutes: int = 30,
    dry_run: bool = True,
) -> list[dict]:
    """우선순위 메일을 캘린더 투두로 등록.

    Args:
        calendar_service: 인증된 Calendar API 서비스 객체
        emails: 우선순위 정렬된 메일 리스트
        calendar_id: 대상 캘린더 ID
        todo_prefix: 이벤트 제목 접두사
        duration_minutes: 이벤트 길이 (분)
        dry_run: True면 실제 등록하지 않고 미리보기만

    Returns:
        생성된 이벤트 리스트
    """
    tracking = _load_tracking()
    created_events = []

    for email in emails:
        # 중복 등록 방지
        if email.message.message_id in tracking:
            continue

        event = _build_event(email, todo_prefix, duration_minutes)

        if dry_run:
            created_events.append(
                {"status": "dry_run", "summary": event["summary"], "event": event}
            )
        else:
            result = (
                calendar_service.events()
                .insert(calendarId=calendar_id, body=event)
                .execute()
            )
            tracking.add(email.message.message_id)
            created_events.append(
                {
                    "status": "created",
                    "summary": event["summary"],
                    "calendar_link": result.get("htmlLink", ""),
                }
            )

    if not dry_run:
        _save_tracking(tracking)

    return created_events
