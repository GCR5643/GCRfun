"""미답변 메일 우선순위 스코어링 모듈.

최신순, 고객 여부, 요청 유형, 스레드 깊이를 가중치로 조합하여
미답변 메일의 우선순위를 결정합니다.
"""

from datetime import datetime, timezone
from dataclasses import dataclass

from src.classifier import ClassificationResult
from src.fetcher import EmailMessage


@dataclass
class PrioritizedEmail:
    """우선순위가 매겨진 메일."""

    message: EmailMessage
    classification: ClassificationResult
    score: float
    rank: int = 0


def _recency_score(msg_date: datetime, now: datetime) -> float:
    """최신 메일일수록 1.0에 가깝게, 오래될수록 0.0에 가깝게.

    30일 기준으로 정규화. 오늘 = 1.0, 30일 전 = 0.0
    """
    delta = (now - msg_date).total_seconds()
    max_seconds = 30 * 24 * 3600  # 30일
    score = max(0.0, 1.0 - (delta / max_seconds))
    return round(score, 4)


def _customer_score(
    sender_domain: str,
    is_customer_from_llm: bool,
    customer_domains: list[str],
) -> float:
    """고객 여부에 따른 점수. 고객이면 1.0, 아니면 0.2."""
    if sender_domain.lower() in [d.lower() for d in customer_domains]:
        return 1.0
    if is_customer_from_llm:
        return 0.8
    return 0.2


def _request_type_score(result: ClassificationResult) -> float:
    """질문/일정 요청 유형에 따른 점수."""
    score = 0.0
    if result.has_direct_question:
        score += 0.6
    if result.has_schedule_request:
        score += 0.4
    return min(score, 1.0)


def _thread_depth_score(thread_length: int) -> float:
    """스레드 길이에 따른 점수. 길수록 중요 (최대 10에서 1.0)."""
    return min(thread_length / 10.0, 1.0)


def prioritize(
    messages: list[EmailMessage],
    classifications: list[ClassificationResult],
    customer_domains: list[str],
    weights: dict | None = None,
    max_results: int = 10,
) -> list[PrioritizedEmail]:
    """NEEDS_REPLY 메일을 우선순위에 따라 정렬하여 상위 N개를 반환.

    Args:
        messages: 메일 리스트
        classifications: 분류 결과 리스트
        customer_domains: 고객 도메인 목록
        weights: 가중치 (기본값: settings.yaml 기본값)
        max_results: 최대 반환 개수

    Returns:
        PrioritizedEmail 리스트 (스코어 내림차순)
    """
    if weights is None:
        weights = {
            "recency_weight": 0.4,
            "customer_weight": 0.3,
            "request_type_weight": 0.2,
            "thread_depth_weight": 0.1,
        }

    # message_id → classification 매핑
    cls_map = {c.message_id: c for c in classifications}

    now = datetime.now(timezone.utc)
    prioritized = []

    for msg in messages:
        cls = cls_map.get(msg.message_id)
        if not cls or cls.classification != "NEEDS_REPLY":
            continue

        recency = _recency_score(msg.date, now)
        customer = _customer_score(
            msg.sender_domain, cls.is_customer, customer_domains
        )
        request = _request_type_score(cls)
        depth = _thread_depth_score(msg.thread_length)

        score = (
            weights["recency_weight"] * recency
            + weights["customer_weight"] * customer
            + weights["request_type_weight"] * request
            + weights["thread_depth_weight"] * depth
        )

        prioritized.append(
            PrioritizedEmail(
                message=msg,
                classification=cls,
                score=round(score, 4),
            )
        )

    # 스코어 내림차순 정렬
    prioritized.sort(key=lambda x: x.score, reverse=True)

    # 순위 부여 및 상위 N개 제한
    for i, item in enumerate(prioritized[:max_results]):
        item.rank = i + 1

    return prioritized[:max_results]
