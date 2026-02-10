"""Google Docs에 자동화 실행 로그를 기록하는 모듈.

설정에서 docs_log.enabled=true, docs_log.document_id에
Google Docs 문서 ID(URL의 /d/DOCUMENT_ID/ 부분)를 넣으면
run 완료 시 해당 문서 끝에 로그가 append 됩니다.
"""

from datetime import datetime


def _get_document_end_index(docs_service, document_id: str) -> int:
    """문서의 끝 위치(인덱스)를 반환. 여기에 텍스트를 삽입하면 append 됨."""
    doc = docs_service.documents().get(documentId=document_id).execute()
    body = doc.get("body", {})
    content = body.get("content", [])
    if not content:
        return 1
    return content[-1].get("endIndex", 1)


def append_run_log(
    docs_service,
    document_id: str,
    log_text: str,
) -> bool:
    """Google Docs 문서 끝에 실행 로그를 추가합니다.

    Args:
        docs_service: Google Docs API 서비스 객체
        document_id: 문서 ID (URL에서 https://docs.google.com/document/d/{document_id}/edit)
        log_text: 추가할 로그 텍스트

    Returns:
        성공 여부
    """
    if not document_id or not log_text.strip():
        return False
    try:
        index = _get_document_end_index(docs_service, document_id)
        # 실행 구분을 위해 구분선 + 타임스탬프 + 로그 본문
        block = f"\n\n---\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n{log_text.strip()}\n"
        requests = [
            {
                "insertText": {
                    "location": {"index": index},
                    "text": block,
                }
            }
        ]
        docs_service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()
        return True
    except Exception:
        return False


def format_run_summary(
    *,
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
    error: str = "",
) -> str:
    """run 결과를 Google Docs에 쓸 요약 텍스트로 포맷합니다."""
    lines = [
        "[Email Task Automation 실행 로그]",
        f"모드: {'DRY RUN (미리보기)' if dry_run else '실행'}",
        f"메일 조회: {messages_count}건",
        f"규칙 매칭: {rule_matched}건, LLM 분류 대상: {remaining}건",
        f"분류 결과: {classification_counts}",
        f"우선순위 상위: {prioritized_count}건",
        f"드래프트: {draft_count}건",
        f"Tasks 투두: {todos_count}건",
        f"읽음 처리: {read_count}건",
    ]
    if reengage_path:
        lines.append(f"재연락 리포트: {reengage_path}")
    if cost_summary:
        lines.append(cost_summary)
    if error:
        lines.append(f"오류: {error}")
    return "\n".join(lines)
