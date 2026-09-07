from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from lazyreview.db import (
    create_feedback_action,
    get_pr,
    get_run,
    list_findings,
    run_is_selectable,
)
from lazyreview.services.consolidate import preview_for_pr
from lazyreview.services.gh import GhError, post_pr_comment, submit_pr_review

router = APIRouter(tags=["feedback"])


class FeedbackBody(BaseModel):
    run_ids: list[int] = Field(..., min_length=1)
    action: str = Field(..., pattern="^(comment|approve|request_changes)$")
    body: str | None = None


def _load_run_pairs(conn, pr_id: int, run_ids: list[int]):
    pairs = []
    for rid in run_ids:
        run = get_run(conn, rid)
        if not run or int(run["pr_id"]) != pr_id:
            raise HTTPException(status_code=400, detail=f"Invalid run_id {rid}")
        if not run_is_selectable(run):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Run #{rid} is not eligible for consolidation "
                    "(must be completed, not consolidated, and not deprecated)"
                ),
            )
        pairs.append((run, list_findings(conn, rid)))
    return pairs


@router.post("/prs/{pr_id}/feedback/preview")
def feedback_preview(pr_id: int, body: FeedbackBody, request: Request):
    """Legacy heuristic preview (UI uses async consolidations instead)."""
    conn = request.app.state.db
    pr = get_pr(conn, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    pairs = _load_run_pairs(conn, pr_id, body.run_ids)
    try:
        result = preview_for_pr(pr["owner"], pr["repo"], int(pr["number"]), pairs)
    except GhError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


@router.post("/prs/{pr_id}/feedback")
def feedback_post(pr_id: int, body: FeedbackBody, request: Request):
    """Legacy sync post path — does not mark runs consolidated (use consolidations API)."""
    conn = request.app.state.db
    pr = get_pr(conn, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    pairs = _load_run_pairs(conn, pr_id, body.run_ids)
    try:
        preview = preview_for_pr(pr["owner"], pr["repo"], int(pr["number"]), pairs)
    except GhError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    text = body.body if body.body is not None else preview["body"]
    gh_url = None
    gh_review_id = None
    try:
        if body.action == "comment":
            res = post_pr_comment(pr["owner"], pr["repo"], int(pr["number"]), text)
            gh_url = res.get("output")
        elif body.action == "approve":
            res = submit_pr_review(
                pr["owner"], pr["repo"], int(pr["number"]), text, "APPROVE"
            )
            gh_url = res.get("html_url")
            gh_review_id = res.get("id")
        elif body.action == "request_changes":
            res = submit_pr_review(
                pr["owner"], pr["repo"], int(pr["number"]), text, "REQUEST_CHANGES"
            )
            gh_url = res.get("html_url")
            gh_review_id = res.get("id")
    except GhError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    action = create_feedback_action(
        conn,
        pr_id=pr_id,
        action=body.action,
        source_run_ids=body.run_ids,
        body=text,
        gh_comment_url=gh_url,
        gh_review_id=gh_review_id,
        dedup_summary=preview.get("dedup_summary"),
    )
    return {"action": action, "preview": preview}
