"""Google Tasks 기반 투두 생성 모듈.

미답변 메일을 Google Tasks에 등록합니다.
- 캘린더에 체크박스 형태로 표시됨
- 완료 시 체크하여 관리 가능
- 날짜별로 그룹화되어 일별 Task로 표시

Google Tasks API를 사용하며, Calendar Events API와 달리
체크리스트 형태의 할 일 관리를 지원합니다.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.prioritizer import PrioritizedEmail

TRACKING_FILE = "output/task_tracking.json"
TASKLIST_TITLE = "메일 투두"


def _load_tracking() -> set[str]:
    """이미 등록된 message_id 집합을 로드."""
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


def _get_or_create_tasklist(tasks_service, title: str = TASKLIST_TITLE) -> str:
    """전용 태스크 리스트를 찾거나 새로 생성. tasklist ID를 반환."""
    result = tasks_service.tasklists().list(maxResults=100).execute()
    for tl in result.get("items", []):
        if tl["title"] == title:
            return tl["id"]

    # 없으면 생성
    new_list = tasks_service.tasklists().insert(body={"title": title}).execute()
    return new_list["id"]


def _next_workday() -> datetime:
    """다음 영업일(월~금)을 반환."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


def _build_task(email: PrioritizedEmail, todo_prefix: str) -> dict:
    """PrioritizedEmail을 Google Tasks 형식으로 변환."""
    msg = email.message
    cls = email.classification

    title = f"{todo_prefix} {msg.subject}"
    if len(title) > 100:
        title = title[:97] + "..."

    notes_lines = [
        f"발신자: {msg.sender}",
        f"수신일: {msg.date.strftime('%Y-%m-%d %H:%M')}",
        f"우선순위: #{email.rank} (점수: {email.score})",
        f"사유: {cls.reason}",
        "",
        msg.snippet,
        "",
        f"[message_id:{msg.message_id}]",
    ]

    due = _next_workday()

    return {
        "title": title,
        "notes": "\n".join(notes_lines),
        "due": due.strftime("%Y-%m-%dT00:00:00.000Z"),
        "status": "needsAction",
    }


def create_todos(
    tasks_service,
    emails: list[PrioritizedEmail],
    todo_prefix: str = "[메일투두]",
    dry_run: bool = True,
) -> list[dict]:
    """우선순위 메일을 Google Tasks 체크리스트로 등록.

    Args:
        tasks_service: 인증된 Google Tasks API 서비스 객체
        emails: 우선순위 정렬된 메일 리스트
        todo_prefix: 태스크 제목 접두사
        dry_run: True면 실제 등록하지 않고 미리보기만

    Returns:
        생성된 태스크 리스트
    """
    tracking = _load_tracking()
    created_tasks = []

    tasklist_id = None
    if not dry_run:
        tasklist_id = _get_or_create_tasklist(tasks_service)

    for email in emails:
        if email.message.message_id in tracking:
            continue

        task = _build_task(email, todo_prefix)

        if dry_run:
            created_tasks.append(
                {"status": "dry_run", "title": task["title"], "due": task["due"]}
            )
        else:
            result = (
                tasks_service.tasks()
                .insert(tasklist=tasklist_id, body=task)
                .execute()
            )
            tracking.add(email.message.message_id)
            created_tasks.append(
                {
                    "status": "created",
                    "title": task["title"],
                    "due": task["due"],
                    "task_id": result.get("id", ""),
                }
            )

    if not dry_run:
        _save_tracking(tracking)

    return created_tasks
