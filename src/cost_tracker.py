"""API 호출 비용 추적 모듈.

Claude API 호출의 토큰 사용량과 추정 비용을 기록합니다.
"""

import json
from datetime import datetime
from pathlib import Path

COST_LOG_FILE = "output/cost_log.json"

# 모델별 가격 (USD per 1M tokens, 2026-02 기준)
MODEL_PRICING = {
    "claude-sonnet-4-5-20250929": {
        "input": 3.0,
        "output": 15.0,
    },
    "claude-haiku-4-5-20251001": {
        "input": 0.80,
        "output": 4.0,
    },
}


def _load_log() -> list[dict]:
    """비용 로그를 로드."""
    p = Path(COST_LOG_FILE)
    if not p.exists():
        return []
    return json.loads(p.read_text())


def _save_log(entries: list[dict]) -> None:
    """비용 로그를 저장."""
    p = Path(COST_LOG_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, ensure_ascii=False, indent=2))


def record_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    operation: str = "classify",
) -> dict:
    """API 호출 사용량을 기록하고 추정 비용을 반환.

    Args:
        model: 사용한 모델 ID
        input_tokens: 입력 토큰 수
        output_tokens: 출력 토큰 수
        operation: 작업 유형 (classify, rule_learn 등)

    Returns:
        기록된 엔트리
    """
    pricing = MODEL_PRICING.get(model, {"input": 3.0, "output": 15.0})

    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]

    entry = {
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "operation": operation,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(input_cost + output_cost, 6),
    }

    log = _load_log()
    log.append(entry)
    _save_log(log)

    return entry


def get_summary() -> dict:
    """누적 비용 요약을 반환."""
    log = _load_log()

    if not log:
        return {
            "total_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost_usd": 0.0,
        }

    return {
        "total_calls": len(log),
        "total_input_tokens": sum(e["input_tokens"] for e in log),
        "total_output_tokens": sum(e["output_tokens"] for e in log),
        "total_cost_usd": round(sum(e["estimated_cost_usd"] for e in log), 4),
        "first_call": log[0]["timestamp"],
        "last_call": log[-1]["timestamp"],
    }
