"""Google Tasks + Calendar 이벤트 모듈.

1. Tasks: 미답변 메일을 reply_type별 요약 불릿으로 체크리스트 등록
2. Calendar Event: 긴급 답신 시 30분 스케줄 블록 삽입
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.classifier import ClassificationResult
from src.prioritizer import PrioritizedEmail

logger = logging.getLogger(__name__)

TRACKING_FILE = "output/task_tracking.json"
TASKLIST_TITLE = "메일 투두"

KST = timezone(timedelta(hours=9))

# reply_type별 태스크 접두 이모지 대체 텍스트
REPLY_TYPE_TAG = {
    "DECISION": "[판단]",
    "SIMPLE_REPLY": "[답장]",
    "SCHEDULE": "[일정]",
    "INFO_SHARE": "[자료]",
    "DELEGATE": "[위임]",
}


def _load_tracking() -> set[str]:
    p = Path(TRACKING_FILE)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text())
        return set(data.get("registered_ids", []))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("태스크 트래킹 파일 읽기 실패: %s", e)
        return set()


def _save_tracking(ids: set[str]) -> None:
    p = Path(TRACKING_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"registered_ids": sorted(ids)}, indent=2))


def _get_or_create_tasklist(tasks_service, title: str = TASKLIST_TITLE) -> str:
    result = tasks_service.tasklists().list(maxResults=100).execute()
    for tl in result.get("items", []):
        if tl["title"] == title:
            return tl["id"]
    new_list = tasks_service.tasklists().insert(body={"title": title}).execute()
    return new_list["id"]


def _next_workday() -> datetime:
    now = datetime.now(KST)
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


def _build_task_with_summary(
    email: PrioritizedEmail,
    cls: ClassificationResult,
    draft_info: dict | None,
) -> dict:
    """reply_type별 요약 불릿을 포함한 태스크를 생성."""
    msg = email.message
    tag = REPLY_TYPE_TAG.get(cls.reply_type or "", "")
    title = f"{tag} {msg.subject}"
    if len(title) > 100:
        title = title[:97] + "..."

    # 요약 불릿 포인트 구성
    bullets = [
        f"From: {msg.sender}",
        f"수신: {msg.date.strftime('%m/%d %H:%M')}",
        f"유형: {cls.reply_type or 'N/A'}",
        f"사유: {cls.reason}",
    ]

    if cls.reply_type == "DECISION" and cls.decision_options:
        bullets.append("")
        bullets.append("의사결정 옵션:")
        for i, opt in enumerate(cls.decision_options, 1):
            bullets.append(f"  {i}. {opt}")

    if cls.reply_type == "SCHEDULE" and draft_info and draft_info.get("schedule_info"):
        bullets.append("")
        bullets.append(draft_info["schedule_info"])

    if cls.reply_type == "INFO_SHARE" and cls.suggested_file:
        bullets.append("")
        bullets.append(f"전달 파일: {cls.suggested_file}")

    if draft_info and draft_info.get("status") == "created":
        bullets.append("")
        bullets.append("Gmail 드래프트 생성됨 - 확인 후 발송하세요")

    bullets.append("")
    bullets.append(msg.snippet)

    due = _next_workday()

    return {
        "title": title,
        "notes": "\n".join(bullets),
        "due": due.strftime("%Y-%m-%dT00:00:00.000Z"),
        "status": "needsAction",
    }


def create_todos(
    tasks_service,
    emails: list[PrioritizedEmail],
    classifications: list[ClassificationResult],
    draft_results: list[dict] | None = None,
    dry_run: bool = True,
) -> list[dict]:
    """우선순위 메일을 reply_type 요약 포함 Tasks로 등록.

    Args:
        tasks_service: Tasks API 서비스 객체
        emails: 우선순위 메일 리스트
        classifications: 분류 결과 (reply_type 포함)
        draft_results: 드래프트 생성 결과 (있으면 연결)
        dry_run: 미리보기 여부
    """
    tracking = _load_tracking()
    cls_map = {c.message_id: c for c in classifications}
    draft_map = {}
    if draft_results:
        for d in draft_results:
            tid = d.get("thread_id")
            if tid:
                draft_map[tid] = d

    created = []

    tasklist_id = None
    if not dry_run:
        tasklist_id = _get_or_create_tasklist(tasks_service)

    for email in emails:
        if email.message.message_id in tracking:
            continue

        cls = cls_map.get(email.message.message_id)
        if not cls:
            continue

        draft_info = draft_map.get(email.message.thread_id)
        task = _build_task_with_summary(email, cls, draft_info)

        if dry_run:
            created.append({
                "status": "dry_run",
                "title": task["title"],
                "due": task["due"],
                "reply_type": cls.reply_type,
            })
        else:
            try:
                result = (
                    tasks_service.tasks()
                    .insert(tasklist=tasklist_id, body=task)
                    .execute()
                )
                tracking.add(email.message.message_id)
                created.append({
                    "status": "created",
                    "title": task["title"],
                    "due": task["due"],
                    "task_id": result.get("id", ""),
                    "reply_type": cls.reply_type,
                })
            except Exception as e:
                logger.error("Tasks 등록 실패: %s (title=%s)", e, task["title"])
                created.append({
                    "status": "error",
                    "title": task["title"],
                    "error": str(e),
                })

    if not dry_run:
        _save_tracking(tracking)

    return created


def insert_urgent_schedule(
    calendar_service,
    subject: str,
    count: int = 1,
    calendar_id: str = "primary",
    dry_run: bool = True,
) -> dict | None:
    """긴급 답변이 필요한 경우 캘린더에 30분 스케줄 블록을 삽입.

    Args:
        calendar_service: Calendar API 서비스 객체
        subject: 메일 건명
        count: 긴급 건 수
        calendar_id: 캘린더 ID
        dry_run: 미리보기 여부

    Returns:
        생성된 이벤트 정보 또는 None
    """
    now = datetime.now(KST)
    # 다음 정각 또는 30분 단위로
    if now.minute < 30:
        start = now.replace(minute=30, second=0, microsecond=0)
    else:
        start = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    # 근무 시간 외면 다음 영업일 10시로
    if start.hour >= 18 or start.hour < 10 or start.weekday() >= 5:
        start = _next_workday().replace(hour=10, minute=0)

    end = start + timedelta(minutes=30)

    summary = f"메일답신 요망: \"{subject}\" 외 {count - 1}건" if count > 1 else f"메일답신 요망: \"{subject}\""

    event = {
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Seoul"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Seoul"},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 5}],
        },
        "colorId": "11",
    }

    if dry_run:
        return {
            "status": "dry_run",
            "summary": summary,
            "start": start.isoformat(),
        }

    try:
        result = (
            calendar_service.events()
            .insert(calendarId=calendar_id, body=event)
            .execute()
        )
    except Exception as e:
        logger.error("캘린더 이벤트 삽입 실패: %s", e)
        return {
            "status": "error",
            "summary": summary,
            "error": str(e),
        }

    return {
        "status": "created",
        "summary": summary,
        "link": result.get("htmlLink", ""),
    }
