"""Email Task Automation — CLI 진입점.

사용법:
    python main.py auth          # OAuth 인증 (최초 1회)
    python main.py run           # 메일 분류 + 액션 실행
    python main.py run --dry-run # 미리보기만 (실제 변경 없음)
    python main.py rules list    # 규칙 목록 조회
    python main.py rules disable <rule_id>  # 규칙 비활성화
    python main.py rules stats   # 규칙 매칭 통계
    python main.py cost          # API 비용 요약
"""

import click
import yaml

from src.auth import get_gmail_service, get_tasks_service, get_credentials
from src.fetcher import fetch_messages
from src.rules_engine import load_rules, save_rules, apply_rules, merge_rules, get_stats, disable_rule
from src.classifier import classify_batch, suggest_rules, ClassificationResult
from src.prioritizer import prioritize
from src.calendar_client import create_todos
from src.actions import mark_as_read, generate_reengage_report
from src.cost_tracker import record_usage, get_summary


def _load_settings() -> dict:
    """settings.yaml 로드. 없으면 기본값 사용."""
    try:
        with open("config/settings.yaml") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        click.echo(
            "config/settings.yaml이 없습니다. "
            "config/settings.yaml.example을 복사해서 사용하세요."
        )
        raise click.Abort()


@click.group()
def cli():
    """Email Task Automation — Gmail 메일 자동 분류 및 캘린더 투두 생성 도구."""
    pass


@cli.command()
def auth():
    """Google OAuth 인증을 수행합니다 (최초 1회)."""
    click.echo("Google OAuth 인증을 시작합니다...")
    click.echo("브라우저가 열리면 회사 Google 계정으로 로그인하세요.")
    try:
        creds = get_credentials()
        click.echo(f"인증 성공! 토큰이 저장되었습니다.")
    except FileNotFoundError as e:
        click.echo(f"오류: {e}")
        raise click.Abort()


@cli.command()
@click.option("--dry-run/--no-dry-run", default=None, help="미리보기 모드 (실제 변경 없음)")
@click.option("--verbose/--quiet", default=None, help="상세 로그 출력")
def run(dry_run, verbose):
    """메일을 분류하고 액션을 실행합니다."""
    settings = _load_settings()

    # CLI 옵션이 없으면 settings.yaml 값 사용
    if dry_run is None:
        dry_run = settings.get("execution", {}).get("dry_run", True)
    if verbose is None:
        verbose = settings.get("execution", {}).get("verbose", True)

    if dry_run:
        click.echo("=== DRY RUN 모드 (실제 변경 없음) ===\n")

    # 1. Gmail 서비스 초기화
    click.echo("[1/6] Gmail 연결 중...")
    gmail = get_gmail_service()

    # 2. 메일 조회
    email_config = settings.get("email", {})
    my_email = email_config.get("my_email", "")
    if not my_email:
        click.echo("오류: config/settings.yaml에 email.my_email을 설정하세요.")
        raise click.Abort()

    click.echo(f"[2/6] 최근 {email_config.get('lookback_days', 30)}일 메일 조회 중...")
    messages = fetch_messages(
        gmail,
        my_email=my_email,
        max_results=email_config.get("max_results", 100),
        lookback_days=email_config.get("lookback_days", 30),
    )
    click.echo(f"       → {len(messages)}건 조회됨")

    if not messages:
        click.echo("처리할 메일이 없습니다.")
        return

    # 3. 규칙 엔진 1차 필터링
    click.echo("[3/6] 규칙 기반 필터링 중...")
    rules = load_rules()
    rule_matched, remaining = apply_rules(messages, rules)
    save_rules(rules)  # match_count 업데이트
    click.echo(f"       → 규칙 매칭: {len(rule_matched)}건 (JUNK), 남은 메일: {len(remaining)}건")

    # 4. Claude 분류
    click.echo("[4/6] Claude AI 분류 중...")
    llm_config = settings.get("llm", {})
    batch_size = llm_config.get("batch_size", 5)

    all_classifications: list[ClassificationResult] = []

    # 규칙 매칭된 것은 JUNK로 직접 설정
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

    # 나머지는 배치로 Claude 분류
    for i in range(0, len(remaining), batch_size):
        batch = remaining[i : i + batch_size]
        if verbose:
            click.echo(f"       → 배치 {i // batch_size + 1}: {len(batch)}건 분류 중...")

        results = classify_batch(
            batch,
            model=llm_config.get("model", "claude-sonnet-4-5-20250929"),
            max_tokens=llm_config.get("max_tokens", 300) * len(batch),
        )
        all_classifications.extend(results)

    # 규칙 학습: JUNK 메일을 모아서 한 번에 통합 규칙 제안
    junk_from_llm = [
        msg for msg in remaining
        for cls in all_classifications
        if cls.message_id == msg.message_id and cls.classification == "JUNK"
    ]
    if junk_from_llm:
        click.echo(f"       → JUNK {len(junk_from_llm)}건에서 규칙 학습 중...")
        suggested = suggest_rules(
            junk_from_llm,
            model=llm_config.get("model", "claude-sonnet-4-5-20250929"),
        )
        added = merge_rules(rules, suggested)
        if added and verbose:
            for r in added:
                click.echo(f"       → 새 규칙: {r.get('sender', '')} {r.get('keywords', '')}")

    save_rules(rules)

    # 분류 결과 요약
    counts = {"JUNK": 0, "NEEDS_REPLY": 0, "STALE": 0, "NORMAL": 0}
    for cls in all_classifications:
        counts[cls.classification] = counts.get(cls.classification, 0) + 1
    click.echo(f"       → 분류 결과: {counts}")

    # 5. 미답변 메일 우선순위화 + Google Tasks 투두
    click.echo("[5/6] 미답변 메일 우선순위화 및 Tasks 투두 생성...")
    cal_config = settings.get("calendar", {})
    priority_config = settings.get("priority", {})
    customer_domains = settings.get("customer_domains", [])

    prioritized = prioritize(
        messages=messages + remaining,
        classifications=all_classifications,
        customer_domains=customer_domains,
        weights=priority_config if priority_config else None,
        max_results=cal_config.get("max_todos_per_run", 10),
    )

    if prioritized:
        if verbose:
            click.echo("\n  투두 대상 메일:")
            for p in prioritized:
                customer_tag = "[고객]" if p.classification.is_customer else ""
                click.echo(
                    f"    #{p.rank} (점수: {p.score}) {customer_tag} "
                    f"{p.message.subject[:50]} — {p.message.sender}"
                )
            click.echo()

        tasks = get_tasks_service()
        created = create_todos(
            tasks,
            prioritized,
            todo_prefix=cal_config.get("todo_prefix", "[메일투두]"),
            dry_run=dry_run,
        )
        click.echo(f"       → Tasks 투두: {len(created)}건 {'(미리보기)' if dry_run else '생성됨'}")
    else:
        click.echo("       → 미답변 메일 없음")

    # 6. JUNK 읽음 처리 + 재연락 리포트
    click.echo("[6/6] JUNK 읽음 처리 및 재연락 리포트 생성...")

    junk_msgs = [
        msg
        for msg in messages
        for cls in all_classifications
        if cls.message_id == msg.message_id and cls.classification == "JUNK"
    ]
    read_results = mark_as_read(gmail, junk_msgs, dry_run=dry_run)
    unread_count = sum(1 for r in read_results if r["status"] in ("marked_read", "dry_run"))
    click.echo(f"       → 읽음 처리: {unread_count}건 {'(미리보기)' if dry_run else '완료'}")

    # 재연락 리포트
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
    if summary["total_calls"] > 0:
        click.echo(f"누적 API 비용: ${summary['total_cost_usd']:.4f}")


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
