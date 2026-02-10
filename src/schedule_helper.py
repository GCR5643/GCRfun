"""Google Calendar 일정 조회 및 빈 시간 제안 모듈.

고객이 제안한 시간의 가용 여부를 확인하고,
불가 시 대안 시간대를 제안합니다.

설정 기반:
  - 근무 시간: 월~금 10:00-18:00 KST
  - 점심 제외: 12:00-13:00
  - 미팅 기본 길이: 60분
"""

from datetime import datetime, timedelta, timezone, time
from dataclasses import dataclass

from dateutil import parser as date_parser

KST = timezone(timedelta(hours=9))


@dataclass
class TimeSlot:
    """시간 슬롯."""

    start: datetime
    end: datetime

    def __str__(self):
        return (
            f"{self.start.strftime('%m/%d(%a) %H:%M')}"
            f"~{self.end.strftime('%H:%M')}"
        )


def get_busy_slots(
    calendar_service,
    start_date: datetime,
    end_date: datetime,
    calendar_id: str = "primary",
) -> list[TimeSlot]:
    """캘린더에서 바쁜 시간대를 조회."""
    body = {
        "timeMin": start_date.isoformat(),
        "timeMax": end_date.isoformat(),
        "timeZone": "Asia/Seoul",
        "items": [{"id": calendar_id}],
    }

    result = calendar_service.freebusy().query(body=body).execute()
    busy_list = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])

    slots = []
    for b in busy_list:
        slots.append(
            TimeSlot(
                start=date_parser.parse(b["start"]),
                end=date_parser.parse(b["end"]),
            )
        )
    return slots


def _is_work_hour(dt: datetime, work_start: int, work_end: int) -> bool:
    """근무 시간 내인지 확인."""
    h = dt.astimezone(KST).hour
    return work_start <= h < work_end


def _is_lunch(dt: datetime, lunch_start: int = 12, lunch_end: int = 13) -> bool:
    """점심 시간인지 확인."""
    h = dt.astimezone(KST).hour
    return lunch_start <= h < lunch_end


def _overlaps(slot: TimeSlot, busy: list[TimeSlot]) -> bool:
    """슬롯이 바쁜 시간과 겹치는지 확인."""
    for b in busy:
        if slot.start < b.end and slot.end > b.start:
            return True
    return False


def check_proposed_time(
    calendar_service,
    proposed_time_str: str,
    meeting_minutes: int = 60,
    calendar_id: str = "primary",
) -> dict:
    """고객이 제안한 시간이 가능한지 확인.

    Returns:
        {"available": bool, "slot": TimeSlot or None, "conflict": str or None}
    """
    try:
        proposed = date_parser.parse(proposed_time_str)
        if proposed.tzinfo is None:
            proposed = proposed.replace(tzinfo=KST)
    except (ValueError, TypeError):
        return {"available": False, "slot": None, "conflict": "시간 파싱 실패"}

    proposed_end = proposed + timedelta(minutes=meeting_minutes)
    slot = TimeSlot(start=proposed, end=proposed_end)

    # 주말 체크
    if proposed.weekday() >= 5:
        return {"available": False, "slot": slot, "conflict": "주말"}

    # 근무 시간 체크
    if not _is_work_hour(proposed, 10, 18):
        return {"available": False, "slot": slot, "conflict": "근무 시간 외"}

    # 점심 체크
    if _is_lunch(proposed) or _is_lunch(proposed_end - timedelta(minutes=1)):
        return {"available": False, "slot": slot, "conflict": "점심 시간"}

    # 기존 일정 충돌 체크
    day_start = proposed.replace(hour=0, minute=0, second=0)
    day_end = day_start + timedelta(days=1)
    busy = get_busy_slots(calendar_service, day_start, day_end, calendar_id)

    if _overlaps(slot, busy):
        return {"available": False, "slot": slot, "conflict": "기존 일정 충돌"}

    return {"available": True, "slot": slot, "conflict": None}


def find_available_slots(
    calendar_service,
    num_slots: int = 3,
    meeting_minutes: int = 60,
    search_days: int = 10,
    work_start: int = 10,
    work_end: int = 18,
    calendar_id: str = "primary",
) -> list[TimeSlot]:
    """다음 가능한 빈 시간대를 N개 찾아 반환.

    Args:
        calendar_service: Calendar API 서비스 객체
        num_slots: 찾을 슬롯 수
        meeting_minutes: 미팅 길이 (분)
        search_days: 검색 범위 (일)
        work_start: 근무 시작 시간 (KST)
        work_end: 근무 종료 시간 (KST)
        calendar_id: 캘린더 ID

    Returns:
        TimeSlot 리스트
    """
    now = datetime.now(KST)
    search_end = now + timedelta(days=search_days)

    busy = get_busy_slots(calendar_service, now, search_end, calendar_id)

    found: list[TimeSlot] = []
    current = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    while len(found) < num_slots and current < search_end:
        # 주말 건너뛰기
        if current.weekday() >= 5:
            current = current.replace(hour=work_start, minute=0)
            current += timedelta(days=(7 - current.weekday()))
            continue

        hour = current.astimezone(KST).hour

        # 근무 시간 밖이면 다음 날 시작으로
        if hour < work_start:
            current = current.replace(hour=work_start, minute=0)
            continue
        if hour >= work_end:
            current += timedelta(days=1)
            current = current.replace(hour=work_start, minute=0)
            continue

        # 점심 시간 건너뛰기
        if _is_lunch(current):
            current = current.replace(hour=13, minute=0)
            continue

        slot_end = current + timedelta(minutes=meeting_minutes)

        # 슬롯이 점심에 걸치는지 확인
        if _is_lunch(slot_end - timedelta(minutes=1)):
            current = current.replace(hour=13, minute=0)
            continue

        # 근무 시간 넘어가는지 확인
        if slot_end.astimezone(KST).hour > work_end or (
            slot_end.astimezone(KST).hour == work_end
            and slot_end.astimezone(KST).minute > 0
        ):
            current += timedelta(days=1)
            current = current.replace(hour=work_start, minute=0)
            continue

        slot = TimeSlot(start=current, end=slot_end)

        if not _overlaps(slot, busy):
            found.append(slot)

        current += timedelta(minutes=30)  # 30분 단위로 탐색

    return found


def format_schedule_options(
    calendar_service,
    proposed_time_str: str | None,
    meeting_minutes: int = 60,
    calendar_id: str = "primary",
) -> dict:
    """일정 제안에 필요한 모든 정보를 정리하여 반환.

    Returns:
        {
            "proposed_check": {...} or None,
            "alternatives": [TimeSlot, ...],
            "summary": "사람이 읽을 수 있는 요약 텍스트"
        }
    """
    proposed_check = None
    if proposed_time_str:
        proposed_check = check_proposed_time(
            calendar_service, proposed_time_str, meeting_minutes, calendar_id
        )

    alternatives = find_available_slots(
        calendar_service, num_slots=3, meeting_minutes=meeting_minutes,
        calendar_id=calendar_id,
    )

    # 요약 텍스트 생성
    lines = []

    if proposed_check:
        if proposed_check["available"]:
            lines.append(f"고객 제안 시간: {proposed_check['slot']} → 가능")
        else:
            lines.append(
                f"고객 제안 시간: {proposed_check['slot']} "
                f"→ 불가 ({proposed_check['conflict']})"
            )

    if alternatives:
        lines.append("대안 시간:")
        for i, slot in enumerate(alternatives, 1):
            lines.append(f"  {i}. {slot}")

    return {
        "proposed_check": proposed_check,
        "alternatives": alternatives,
        "summary": "\n".join(lines),
    }
