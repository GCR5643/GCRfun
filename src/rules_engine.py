"""규칙 기반 필터 엔진.

간소화된 규칙 구조:
  - sender: 발신자 주소에 포함된 문자열 (예: "@github.com")
  - keywords: 제목에 포함된 키워드 리스트 (OR 매칭, 선택사항)
  - enabled: 활성/비활성

규칙 예시 (rules.yaml):
  rules:
    - sender: "@github.com"
      enabled: true
    - keywords: ["프로모션", "뉴스레터"]
      enabled: true
    - sender: "@marketing.example.com"
      keywords: ["할인", "offer"]
      enabled: true
"""

from pathlib import Path

import yaml

from src.fetcher import EmailMessage

DEFAULT_RULES_PATH = "config/rules.yaml"
MAX_AUTO_RULES = 30  # LLM이 자동 생성하는 규칙 최대 개수


def load_rules(path: str = DEFAULT_RULES_PATH) -> list[dict]:
    """YAML 파일에서 규칙 리스트를 로드."""
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.dump({"rules": []}, allow_unicode=True))
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

    - sender만 있으면: 발신자에 해당 문자열이 포함되면 매칭
    - keywords만 있으면: 제목에 키워드 중 하나라도 포함되면 매칭
    - 둘 다 있으면: sender AND keywords 모두 충족해야 매칭
    """
    if not rule.get("enabled", True):
        return False

    sender_pat = rule.get("sender", "")
    keywords = rule.get("keywords", [])

    # sender도 keywords도 없으면 매칭 안 됨
    if not sender_pat and not keywords:
        return False

    sender_ok = True
    if sender_pat:
        sender_ok = sender_pat.lower() in message.sender.lower()

    keywords_ok = True
    if keywords:
        subject_lower = message.subject.lower()
        keywords_ok = any(kw.lower() in subject_lower for kw in keywords)

    return sender_ok and keywords_ok


def apply_rules(
    messages: list[EmailMessage],
    rules: list[dict],
) -> tuple[list[EmailMessage], list[EmailMessage]]:
    """규칙을 메일 리스트에 적용하여 (매칭된 메일, 남은 메일)을 반환."""
    matched = []
    remaining = []

    for msg in messages:
        was_matched = False
        for rule in rules:
            if match_rule(msg, rule):
                rule["hits"] = rule.get("hits", 0) + 1
                matched.append(msg)
                was_matched = True
                break
        if not was_matched:
            remaining.append(msg)

    return matched, remaining


def merge_rules(existing: list[dict], new_rules: list[dict]) -> list[dict]:
    """새 규칙을 기존 규칙에 병합. 중복은 건너뛰고, 최대 개수를 초과하면 추가하지 않음.

    중복 기준: sender가 동일하면 중복으로 간주.
    """
    existing_senders = {
        r.get("sender", "").lower() for r in existing if r.get("sender")
    }
    auto_count = sum(1 for r in existing if r.get("source") == "auto")

    added = []
    for rule in new_rules:
        sender = rule.get("sender", "").lower()

        # 중복 체크
        if sender and sender in existing_senders:
            continue

        # 최대 개수 체크
        if auto_count >= MAX_AUTO_RULES:
            break

        rule.setdefault("enabled", True)
        rule.setdefault("source", "auto")
        rule.setdefault("hits", 0)
        existing.append(rule)
        added.append(rule)

        if sender:
            existing_senders.add(sender)
        auto_count += 1

    return added


def disable_rule(rules: list[dict], index: int) -> bool:
    """인덱스로 규칙을 비활성화. (CLI에서 번호로 관리)"""
    if 0 <= index < len(rules):
        rules[index]["enabled"] = False
        return True
    return False


def get_stats(rules: list[dict]) -> list[dict]:
    """규칙별 매칭 통계를 반환."""
    return [
        {
            "index": i,
            "sender": r.get("sender", "-"),
            "keywords": r.get("keywords", []),
            "enabled": r.get("enabled", True),
            "source": r.get("source", "manual"),
            "hits": r.get("hits", 0),
        }
        for i, r in enumerate(rules)
    ]
