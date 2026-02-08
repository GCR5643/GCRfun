"""규칙 기반 필터 엔진.

LLM이 생성한 규칙과 사용자가 직접 추가한 규칙을 YAML로 관리합니다.
규칙 로드, 매칭, 추가, 통계 조회를 지원합니다.
"""

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from src.fetcher import EmailMessage

DEFAULT_RULES_PATH = "config/rules.yaml"


def _ensure_rules_file(path: str) -> Path:
    """rules.yaml이 없으면 빈 구조로 생성."""
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.dump({"rules": []}, allow_unicode=True))
    return p


def load_rules(path: str = DEFAULT_RULES_PATH) -> list[dict]:
    """YAML 파일에서 규칙 리스트를 로드."""
    p = _ensure_rules_file(path)
    data = yaml.safe_load(p.read_text()) or {}
    return data.get("rules", [])


def save_rules(rules: list[dict], path: str = DEFAULT_RULES_PATH) -> None:
    """규칙 리스트를 YAML 파일에 저장."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.dump({"rules": rules}, allow_unicode=True, default_flow_style=False),
    )


def match_rule(message: EmailMessage, rule: dict) -> bool:
    """단일 규칙이 메일에 매칭되는지 검사.

    conditions 내의 모든 조건이 충족되어야 매칭 (AND 로직).
    """
    if not rule.get("enabled", True):
        return False

    conditions = rule.get("conditions", {})

    # sender_pattern 검사
    sender_pat = conditions.get("sender_pattern")
    if sender_pat:
        if not re.search(sender_pat, message.sender, re.IGNORECASE):
            return False

    # subject_contains 검사 (OR - 하나라도 포함되면 매칭)
    subject_keywords = conditions.get("subject_contains")
    if subject_keywords:
        subject_lower = message.subject.lower()
        if not any(kw.lower() in subject_lower for kw in subject_keywords):
            return False

    # has_unsubscribe_header 검사
    unsub = conditions.get("has_unsubscribe_header")
    if unsub is not None:
        if message.has_unsubscribe_header != unsub:
            return False

    # label_is 검사 (OR - 하나라도 있으면 매칭)
    label_check = conditions.get("label_is")
    if label_check:
        msg_labels_lower = [lb.lower() for lb in message.labels]
        if not any(lb.lower() in msg_labels_lower for lb in label_check):
            return False

    return True


def apply_rules(
    messages: list[EmailMessage],
    rules: list[dict],
) -> tuple[list[EmailMessage], list[EmailMessage]]:
    """규칙을 메일 리스트에 적용하여 (매칭된 메일, 남은 메일)을 반환.

    매칭된 규칙의 match_count를 증가시킵니다.
    """
    matched = []
    remaining = []

    for msg in messages:
        was_matched = False
        for rule in rules:
            if match_rule(msg, rule):
                rule["match_count"] = rule.get("match_count", 0) + 1
                matched.append(msg)
                was_matched = True
                break
        if not was_matched:
            remaining.append(msg)

    return matched, remaining


def add_rule(
    rules: list[dict],
    name: str,
    conditions: dict,
    source: str = "llm_generated",
    action: str = "mark_read",
) -> dict:
    """새 규칙을 추가하고 반환."""
    rule = {
        "id": f"rule_{uuid.uuid4().hex[:8]}",
        "name": name,
        "created_at": datetime.now().strftime("%Y-%m-%d"),
        "source": source,
        "enabled": True,
        "conditions": conditions,
        "action": action,
        "match_count": 0,
    }
    rules.append(rule)
    return rule


def disable_rule(rules: list[dict], rule_id: str) -> Optional[dict]:
    """규칙을 비활성화. 성공 시 해당 규칙 반환."""
    for rule in rules:
        if rule["id"] == rule_id:
            rule["enabled"] = False
            return rule
    return None


def get_stats(rules: list[dict]) -> list[dict]:
    """규칙별 매칭 통계를 반환."""
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "enabled": r.get("enabled", True),
            "source": r.get("source", "unknown"),
            "match_count": r.get("match_count", 0),
        }
        for r in rules
    ]
