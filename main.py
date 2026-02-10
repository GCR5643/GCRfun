"""Email Task Automation — CLI 진입점.

사용법:
    python main.py auth          # OAuth 인증 (최초 1회)
    python main.py run           # 메일 분류 + 드래프트 + 투두 실행
    python main.py run --dry-run # 미리보기만 (실제 변경 없음)
    python main.py rules list    # 규칙 목록 조회
    python main.py rules disable <index>  # 규칙 비활성화
    python main.py rules stats   # 규칙 매칭 통계
    python main.py cost          # API 비용 요약
"""

import click
import yaml

from src.auth import (
    get_gmail_service,
    get_tasks_service,
    get_calendar_service,
    get_docs_service,
    get_credentials,
)
from src.fetcher import fetch_messages
from src.rules_engine import (
    load_rules, save_rules, apply_rules, merge_rules, get_stats, disable_rule,
)
from src.classifier import classify_batch, suggest_rules, ClassificationResult
from src.prioritizer import prioritize
from src.draft_composer import compose_and_save_drafts
from src.calendar_client import create_todos, insert_urgent_schedule
from src.actions import mark_as_read, generate_reengage_report
from src.cost_tracker import get_summary
from src.docs_logger import append_run_log, format_run_summary

REPLY_TYPE_LABELS = {
    "DECISION": "[판단]",
    "SIMPLE_REPLY": "[답장]",
    "SCHEDULE": "[일정]",
    "INFO_SHARE": "[자료]",
    "DELEGATE": "[위임]",
}


def _load_settings() -> dict:
    try:
        with open("config/settings.yaml") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        click.echo(
            "config/settings.yaml이 없습니다. "
            "config/settings.yaml.example을 복사해서 사용하세요."
        )
        raise click.Abort()


def _write_docs_log_if_enabled(
    settings: dict,
    dry_run: bool,
    messages_count: int,
    rule_matched: int,
    remaining: int,
    classification_counts: dict,
    prioritized_count: int,
    draft_count: int,
    todos_count: int,
    read_count: int,
    reengage_path: str = "",
    cost_summary: str = "",
) -> None:
    """docs_log 설정이 켜져 있으면 Google Docs 문서 끝에 실행 로그를 append 합니다."""
    docs_cfg = settings.get("docs_log", {}) or {}
    if not docs_cfg.get("enabled"):
        return
    doc_id = (docs_cfg.get("document_id") or "").strip()
    if not doc_id:
        return
    log_text = format_run_summary(
        dry_run=dry_run,
        messages_count=messages_count,
        rule_matched=rule_matched,
        remaining=remaining,
        classification_counts=classification_counts,
        prioritized_count=prioritized_count,
        draft_count=draft_count,
        todos_count=todos_count,
        read_count=read_count,
        reengage_path=reengage_path,
        cost_summary=cost_summary,
    )
    try:
        docs = get_docs_service()
        if append_run_log(docs, doc_id, log_text):
            click.echo("       → 실행 로그를 Google Docs에 기록했습니다.")
    except Exception as e:
        click.echo(f"       → Google Docs 로그 기록 실패: {e}")


@click.group()
def cli():
    """Email Task Automation — Gmail 메일 자동 분류, 드래프트 생성, 캘린더 투두."""
    pass


@cli.command()
def auth():
    """Google OAuth 인증을 수행합니다 (최초 1회)."""
    click.echo("Google OAuth 인증을 시작합니다...")
    click.echo("브라우저가 열리면 회사 Google 계정으로 로그인하세요.")
    try:
        creds = get_credentials()
        click.echo("인증 성공! 토큰이 저장되었습니다.")
    except FileNotFoundError as e:
        click.echo(f"오류: {e}")
        raise click.Abort()


@cli.command()
@click.option("--dry-run/--no-dry-run", default=None, help="미리보기 모드")
@click.option("--verbose/--quiet", default=None, help="상세 로그")
def run(dry_run, verbose):
    """메일을 분류하고 드래프트 생성 + 투두 등록을 실행합니다."""
    settings = _load_settings()

    if dry_run is None:
        dry_run = settings.get("execution", {}).get("dry_run", True)
    if verbose is None:
        verbose = settings.get("execution", {}).get("verbose", True)

    if dry_run:
        click.echo("=== DRY RUN 모드 (실제 변경 없음) ===\n")

    # ── 1. 서비스 초기화 ──
    click.echo("[1/8] 서비스 연결 중...")
    gmail = get_gmail_service()
    tasks = get_tasks_service()
    calendar = get_calendar_service()

    # ── 2. 메일 조회 ──
    email_config = settings.get("email", {})
    my_email = email_config.get("my_email", "")
    if not my_email:
        click.echo("오류: config/settings.yaml에 email.my_email을 설정하세요.")
        raise click.Abort()

    click.echo(f"[2/8] 최근 {email_config.get('lookback_days', 30)}일 메일 조회 중...")
    messages = fetch_messages(
        gmail,
        my_email=my_email,
        max_results=email_config.get("max_results", 100),
        lookback_days=email_config.get("lookback_days", 30),
    )
    click.echo(f"       → {len(messages)}건 조회됨")

    if not messages:
        click.echo("처리할 메일이 없습니다.")
        _write_docs_log_if_enabled(
            settings=settings,
            dry_run=dry_run,
            messages_count=0,
            rule_matched=0,
            remaining=0,
            classification_counts={},
            prioritized_count=0,
            draft_count=0,
            todos_count=0,
            read_count=0,
            reengage_path="",
            cost_summary="",
        )
        return

    # ── 3. 규칙 엔진 1차 필터링 ──
    click.echo("[3/8] 규칙 기반 필터링 중...")
    rules = load_rules()
    rule_matched, remaining = apply_rules(messages, rules)
    save_rules(rules)
    click.echo(f"       → 규칙 매칭: {len(rule_matched)}건, 남은 메일: {len(remaining)}건")

    # ── 4. Claude 분류 (reply_type 포함) ──
    click.echo("[4/8] Claude AI 분류 중...")
    llm_config = settings.get("llm", {})
    batch_size = llm_config.get("batch_size", 5)
    model = llm_config.get("model", "claude-sonnet-4-5-20250929")

    all_classifications: list[ClassificationResult] = []

    for msg in rule_matched:
        all_classifications.append(
            ClassificationResult(
                message_id=msg.message_id,
                classification="JUNK",
                confidence=1.0,
                reason="규칙 매칭",
                is_customer=False,
                has_direct_question=False,
                has_schedule_request=False,
            )
        )

    for i in range(0, len(remaining), batch_size):
        batch = remaining[i : i + batch_size]
        if verbose:
            click.echo(f"       → 배치 {i // batch_size + 1}: {len(batch)}건 분류 중...")
        results = classify_batch(batch, model=model, max_tokens=500 * len(batch))
        all_classifications.extend(results)

    # 규칙 학습
    junk_from_llm = [
        msg for msg in remaining
        for cls in all_classifications
        if cls.message_id == msg.message_id and cls.classification == "JUNK"
    ]
    if junk_from_llm:
        click.echo(f"       → JUNK {len(junk_from_llm)}건에서 규칙 학습 중...")
        suggested = suggest_rules(junk_from_llm, model=model)
        added = merge_rules(rules, suggested)
        if added and verbose:
            for r in added:
                click.echo(f"       → 새 규칙: {r.get('sender', '')} {r.get('keywords', '')}")
    save_rules(rules)

    # 분류 결과 요약
    counts = {"JUNK": 0, "NEEDS_REPLY": 0, "STALE": 0, "NORMAL": 0}
    reply_type_counts = {}
    for cls in all_classifications:
        counts[cls.classification] = counts.get(cls.classification, 0) + 1
        if cls.reply_type:
            reply_type_counts[cls.reply_type] = reply_type_counts.get(cls.reply_type, 0) + 1
    click.echo(f"       → 분류: {counts}")
    if reply_type_counts:
        click.echo(f"       → 답장 유형: {reply_type_counts}")

    # ── 5. 우선순위화 ──
    click.echo("[5/8] 미답변 메일 우선순위화...")
    cal_config = settings.get("calendar", {})
    priority_config = settings.get("priority", {})
    customer_domains = settings.get("customer_domains", [])

    prioritized = prioritize(
        messages=messages,
        classifications=all_classifications,
        customer_domains=customer_domains,
        weights=priority_config if priority_config else None,
        max_results=cal_config.get("max_todos_per_run", 10),
    )

    if prioritized and verbose:
        click.echo("\n  답신 대상 메일:")
        for p in prioritized:
            tag = REPLY_TYPE_LABELS.get(p.classification.reply_type or "", "")
            customer_tag = "[고객]" if p.classification.is_customer else ""
            click.echo(
                f"    #{p.rank} {tag}{customer_tag} (점수:{p.score}) "
                f"{p.message.subject[:40]} — {p.message.sender}"
            )
            if p.classification.reply_type == "DECISION" and p.classification.decision_options:
                for j, opt in enumerate(p.classification.decision_options, 1):
                    click.echo(f"       옵션{j}: {opt}")
            if p.classification.reply_type == "INFO_SHARE" and p.classification.suggested_file:
                click.echo(f"       파일: {p.classification.suggested_file}")
        click.echo()

    # ── 6. 드래프트 생성 ──
    click.echo("[6/8] Gmail 드래프트 생성 중...")
    draft_results = []
    if prioritized:
        prioritized_msgs = [p.message for p in prioritized]
        draft_results = compose_and_save_drafts(
            gmail,
            calendar,
            prioritized_msgs,
            all_classifications,
            model=model,
            dry_run=dry_run,
        )

        draft_count = len(draft_results)
        click.echo(f"       → 드래프트: {draft_count}건 {'(미리보기)' if dry_run else '생성됨'}")
        if verbose:
            for d in draft_results:
                tag = REPLY_TYPE_LABELS.get(d.get("reply_type", ""), "")
                click.echo(f"       {tag} {d.get('subject', d.get('to', ''))}")
    else:
        click.echo("       → 드래프트 대상 없음")

    # ── 7. Tasks 투두 + 긴급 캘린더 스케줄 ──
    click.echo("[7/8] Tasks 투두 등록 및 긴급 스케줄 확인...")
    created_tasks = []

    if prioritized:
        # Tasks 투두 생성 (reply_type별 요약 불릿 포함)
        created_tasks = create_todos(
            tasks,
            prioritized,
            classifications=all_classifications,
            draft_results=draft_results,
            dry_run=dry_run,
        )
        click.echo(f"       → Tasks: {len(created_tasks)}건 {'(미리보기)' if dry_run else '등록됨'}")

        # 긴급 건 확인: 고객 + confidence 높은 건이 있으면 캘린더 블록
        urgent = [
            p for p in prioritized
            if p.classification.is_customer and p.classification.confidence >= 0.8
        ]
        if urgent:
            urgent_result = insert_urgent_schedule(
                calendar,
                subject=urgent[0].message.subject,
                count=len(urgent),
                dry_run=dry_run,
            )
            if urgent_result:
                click.echo(
                    f"       → 긴급 스케줄: {urgent_result['summary']} "
                    f"{'(미리보기)' if dry_run else '삽입됨'}"
                )
    else:
        click.echo("       → 투두 대상 없음")

    # ── 8. JUNK 읽음 처리 + 재연락 리포트 ──
    click.echo("[8/8] JUNK 읽음 처리 및 재연락 리포트 생성...")

    junk_msgs = [
        msg
        for msg in messages
        for cls in all_classifications
        if cls.message_id == msg.message_id and cls.classification == "JUNK"
    ]
    read_results = mark_as_read(gmail, junk_msgs, dry_run=dry_run)
    unread_count = sum(
        1 for r in read_results if r["status"] in ("marked_read", "dry_run")
    )
    click.echo(f"       → 읽음 처리: {unread_count}건 {'(미리보기)' if dry_run else '완료'}")

    reengage_config = settings.get("reengage", {})
    report_path = generate_reengage_report(
        messages,
        all_classifications,
        output_format=reengage_config.get("output_format", "markdown"),
    )
    if report_path:
        click.echo(f"       → 재연락 리포트: {report_path}")
    else:
        click.echo("       → 재연락 대상 없음")

    # 비용 요약
    click.echo("\n=== 완료 ===")
    summary = get_summary()
    cost_str = ""
    if summary["total_calls"] > 0:
        cost_str = f"누적 API 비용: ${summary['total_cost_usd']:.4f}"
        click.echo(cost_str)

    # Google Docs 실행 로그 (설정 시)
    draft_count = len(draft_results) if draft_results else 0
    _write_docs_log_if_enabled(
        settings=settings,
        dry_run=dry_run,
        messages_count=len(messages),
        rule_matched=len(rule_matched),
        remaining=len(remaining),
        classification_counts=counts,
        prioritized_count=len(prioritized) if prioritized else 0,
        draft_count=draft_count,
        todos_count=len(created_tasks),
        read_count=unread_count,
        reengage_path=report_path or "",
        cost_summary=cost_str,
    )


# ── rules 서브커맨드 ──

@cli.group()
def rules():
    """자동 읽음 규칙을 관리합니다."""
    pass


@rules.command("list")
def rules_list():
    """현재 규칙 목록을 출력합니다."""
    all_rules = load_rules()
    if not all_rules:
        click.echo("등록된 규칙이 없습니다.")
        return

    click.echo(f"총 {len(all_rules)}개 규칙:\n")
    for i, r in enumerate(all_rules):
        status = "ON" if r.get("enabled", True) else "OFF"
        sender = r.get("sender", "")
        keywords = r.get("keywords", [])
        hits = r.get("hits", 0)
        source = r.get("source", "manual")

        parts = [f"  [{i}] {status}"]
        if sender:
            parts.append(f"sender={sender}")
        if keywords:
            parts.append(f"keywords={keywords}")
        parts.append(f"(매칭:{hits}, {source})")
        click.echo(" ".join(parts))


@rules.command("disable")
@click.argument("index", type=int)
def rules_disable(index):
    """규칙을 비활성화합니다 (번호로 지정)."""
    all_rules = load_rules()
    if disable_rule(all_rules, index):
        save_rules(all_rules)
        click.echo(f"규칙 #{index} 비활성화됨")
    else:
        click.echo(f"유효하지 않은 번호: {index} (총 {len(all_rules)}개)")


@rules.command("stats")
def rules_stats():
    """규칙별 매칭 통계를 출력합니다."""
    stats = get_stats(load_rules())
    if not stats:
        click.echo("등록된 규칙이 없습니다.")
        return

    click.echo(f"{'#':<4} {'발신자':<30} {'키워드':<25} {'매칭':<6} {'상태':<5} {'출처'}")
    click.echo("-" * 80)
    for s in sorted(stats, key=lambda x: x["hits"], reverse=True):
        status = "ON" if s["enabled"] else "OFF"
        kw = ", ".join(s["keywords"][:3]) if s["keywords"] else "-"
        click.echo(
            f"{s['index']:<4} {s['sender']:<30} {kw:<25} "
            f"{s['hits']:<6} {status:<5} {s['source']}"
        )


@cli.command()
def cost():
    """API 비용 요약을 출력합니다."""
    summary = get_summary()
    if summary["total_calls"] == 0:
        click.echo("API 호출 기록이 없습니다.")
        return

    click.echo("=== API 비용 요약 ===")
    click.echo(f"총 호출 수: {summary['total_calls']}회")
    click.echo(f"총 입력 토큰: {summary['total_input_tokens']:,}")
    click.echo(f"총 출력 토큰: {summary['total_output_tokens']:,}")
    click.echo(f"추정 총 비용: ${summary['total_cost_usd']:.4f}")
    click.echo(f"기간: {summary.get('first_call', '?')} ~ {summary.get('last_call', '?')}")


if __name__ == "__main__":
    cli()
