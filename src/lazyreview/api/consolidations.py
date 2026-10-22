"""Consolidation job APIs."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from lazyreview.agents import get_adapter
from lazyreview.db import (
    active_consolidation_run_ids,
    create_consolidation,
    create_feedback_action,
    get_consolidation,
    get_pr,
    get_run,
    in_flight_consolidation_run_ids,
    list_consolidations_for_pr,
    mark_consolidation_feedback_provided,
    run_is_selectable,
)
from lazyreview.services.analysis_gate import require_succeeded_analysis
from lazyreview.services.consolidation_jobs import consolidations_dir
from lazyreview.services.gh import (
    GhError,
    list_pr_changed_paths,
    post_pr_comment,
    submit_pr_review,
)
from lazyreview.services.pr_diff import commentable_lines_by_path, list_pr_file_patches
from lazyreview.services.review_post import (
    build_review_comments,
    merge_summary_findings_into_review_body,
    parse_findings_list,
    partition_findings_for_github_post,
)
from lazyreview.services.settings import get_consolidator_config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["consolidations"])

_ACTION_TO_EVENT = {
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}


class CreateConsolidationBody(BaseModel):
    run_ids: list[int] = Field(..., min_length=1)


class PostConsolidationBody(BaseModel):
    action: str = Field(..., pattern="^(comment|approve|request_changes)$")
    body: str | None = None


def _validate_run_ids(conn, pr_id: int, run_ids: list[int]) -> list[int]:
    active = active_consolidation_run_ids(conn, pr_id)
    out: list[int] = []
    for rid in run_ids:
        run = get_run(conn, rid)
        if not run or int(run["pr_id"]) != pr_id:
            raise HTTPException(status_code=400, detail=f"Invalid run_id {rid}")
        if not run_is_selectable(run):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Run #{rid} is not eligible for consolidation "
                    "(must be completed, not consolidated, and not obsolete)"
                ),
            )
        if rid in active:
            raise HTTPException(
                status_code=400,
                detail=f"Run #{rid} is already in an active consolidation",
            )
        out.append(int(rid))
    return out


@router.post("/prs/{pr_id}/consolidations")
def create_pr_consolidation(pr_id: int, body: CreateConsolidationBody, request: Request):
    conn = request.app.state.db
    settings = request.app.state.settings
    pr = get_pr(conn, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")

    require_succeeded_analysis(conn, pr_id)

    run_ids = _validate_run_ids(conn, pr_id, body.run_ids)
    cfg = get_consolidator_config(conn)
    try:
        adapter = get_adapter(cfg["agent"])
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not adapter.available():
        raise HTTPException(
            status_code=400,
            detail=f"Consolidator agent {cfg['agent']} binary not available on PATH",
        )

    settings.ensure_dirs()
    # Placeholder workdir; id assigned on insert
    row = create_consolidation(
        conn,
        pr_id=pr_id,
        head_sha=pr.get("head_sha") or "",
        source_run_ids=run_ids,
        agent=cfg["agent"],
        model=cfg["model"],
        effort=cfg["effort"] or None,
        workdir_path="",
    )
    work_dir = consolidations_dir(settings) / str(row["id"])
    work_dir.mkdir(parents=True, exist_ok=True)
    from lazyreview.db import update_consolidation

    row = update_consolidation(conn, int(row["id"]), workdir_path=str(work_dir)) or row
    request.app.state.consolidations.enqueue(int(row["id"]))
    return row


@router.get("/prs/{pr_id}/consolidations")
def list_pr_consolidations(pr_id: int, request: Request):
    conn = request.app.state.db
    pr = get_pr(conn, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    from lazyreview.db import list_feedback_actions

    rows = list_consolidations_for_pr(conn, pr_id)
    active = sorted(active_consolidation_run_ids(conn, pr_id))
    consolidating = sorted(in_flight_consolidation_run_ids(conn, pr_id))
    feedback = list_feedback_actions(conn, pr_id)
    return {
        "consolidations": rows,
        "feedback": feedback,
        "active_run_ids": active,
        "consolidating_run_ids": consolidating,
    }


@router.get("/consolidations/{consolidation_id}")
def get_consolidation_detail(consolidation_id: int, request: Request):
    row = get_consolidation(request.app.state.db, consolidation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Consolidation not found")
    return row


@router.get("/consolidations/{consolidation_id}/logs")
def get_consolidation_logs(consolidation_id: int, request: Request):
    row = get_consolidation(request.app.state.db, consolidation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Consolidation not found")
    workdir = Path(row["workdir_path"] or "")
    stdout_path = Path(row["log_stdout_path"] or (workdir / "stdout.log"))
    stderr_path = Path(row["log_stderr_path"] or (workdir / "stderr.log"))

    def _read(path: Path, limit: int = 400_000) -> str:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > limit:
            return text[-limit:]
        return text

    return {
        "id": consolidation_id,
        "status": row["status"],
        "stdout": _read(stdout_path),
        "stderr": _read(stderr_path),
        "error_message": row.get("error_message"),
        "body": row.get("body"),
        "feedback_provided": int(row.get("feedback_provided") or 0),
        "source_run_ids": row.get("source_run_ids") or [],
    }


@router.post("/consolidations/{consolidation_id}/cancel")
def cancel_consolidation(consolidation_id: int, request: Request):
    row = get_consolidation(request.app.state.db, consolidation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Consolidation not found")
    try:
        return request.app.state.consolidations.cancel(consolidation_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/consolidations/{consolidation_id}/regenerate")
def regenerate_consolidation(consolidation_id: int, request: Request):
    """Re-queue a finished consolidation with current consolidator settings."""
    from lazyreview.db import update_consolidation

    conn = request.app.state.db
    row = get_consolidation(conn, consolidation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Consolidation not found")
    if int(row.get("feedback_provided") or 0):
        raise HTTPException(
            status_code=400,
            detail="Cannot regenerate after feedback was posted to GitHub",
        )
    if row["status"] in ("queued", "running"):
        raise HTTPException(
            status_code=400,
            detail=f"Consolidation is {row['status']}, cancel it first or wait",
        )
    if row["status"] not in ("succeeded", "failed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"Consolidation is {row['status']}, cannot regenerate",
        )

    # Source runs must not already be in another active consolidation.
    active = active_consolidation_run_ids(conn, int(row["pr_id"]))
    for rid in row.get("source_run_ids") or []:
        if int(rid) in active:
            raise HTTPException(
                status_code=400,
                detail=f"Run #{rid} is already in an active consolidation",
            )

    cfg = get_consolidator_config(conn)
    try:
        adapter = get_adapter(cfg["agent"])
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not adapter.available():
        raise HTTPException(
            status_code=400,
            detail=f"Consolidator agent {cfg['agent']} binary not available on PATH",
        )

    pr = get_pr(conn, int(row["pr_id"]))
    updated = update_consolidation(
        conn,
        consolidation_id,
        status="queued",
        agent=cfg["agent"],
        model=cfg["model"],
        effort=cfg["effort"] or None,
        head_sha=(pr.get("head_sha") if pr else None) or row.get("head_sha") or "",
        body=None,
        findings_json=None,
        dedup_summary=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_usd=None,
        error_message=None,
        started_at=None,
        finished_at=None,
        duration_ms=None,
        cli_command=None,
        recommended_action=None,
        recommended_action_rationale=None,
        publish_body=None,
        ready_to_post=0,
        # Keep workdir / log paths; worker overwrites logs on the next run.
    )
    request.app.state.consolidations.enqueue(consolidation_id)
    return updated


@router.post("/consolidations/{consolidation_id}/regenerate-publish")
def regenerate_publish(consolidation_id: int, request: Request):
    """Re-run only the GitHub publish transform (keeps the internal report)."""
    from lazyreview.db import update_consolidation

    conn = request.app.state.db
    row = get_consolidation(conn, consolidation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Consolidation not found")
    if int(row.get("feedback_provided") or 0):
        raise HTTPException(
            status_code=400,
            detail="Cannot regenerate publish draft after feedback was posted to GitHub",
        )
    if row["status"] in ("queued", "running", "dormant"):
        raise HTTPException(
            status_code=400,
            detail=f"Consolidation is {row['status']}, wait for it to finish",
        )
    if row["status"] not in ("succeeded", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"Consolidation is {row['status']}, cannot regenerate publish draft",
        )
    if not (row.get("body") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="No internal report to publish from",
        )

    cfg = get_consolidator_config(conn)
    try:
        adapter = get_adapter(cfg["agent"])
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not adapter.available():
        raise HTTPException(
            status_code=400,
            detail=f"Consolidator agent {cfg['agent']} binary not available on PATH",
        )

    updated = update_consolidation(
        conn,
        consolidation_id,
        ready_to_post=0,
        error_message=None,
    )
    request.app.state.consolidations.enqueue_publish(consolidation_id)
    return updated


@router.post("/consolidations/{consolidation_id}/post")
def post_consolidation(consolidation_id: int, body: PostConsolidationBody, request: Request):
    conn = request.app.state.db
    row = get_consolidation(conn, consolidation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Consolidation not found")
    if row["status"] != "succeeded":
        raise HTTPException(
            status_code=400,
            detail=f"Consolidation is {row['status']}, cannot post",
        )
    if int(row.get("feedback_provided") or 0):
        raise HTTPException(status_code=400, detail="Feedback already provided")
    if not int(row.get("ready_to_post") or 0) or not (row.get("publish_body") or "").strip():
        raise HTTPException(
            status_code=409,
            detail="GitHub draft is not ready yet (publish transform incomplete)",
        )

    pr = get_pr(conn, int(row["pr_id"]))
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")

    text = body.body if body.body is not None else (row.get("publish_body") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="Empty publish body")

    findings = parse_findings_list(row.get("findings_json") or row.get("findings"))
    head_sha = str(pr.get("head_sha") or row.get("head_sha") or "").strip()

    gh_url = None
    gh_review_id = None
    dropped: list = []
    skipped_off_diff = 0
    skipped_bad_line = 0
    posted_inline = 0
    appended_summary = 0
    changed_paths: frozenset[str] | None = None
    commentable_lines: dict[str, frozenset[int]] | None = None
    try:
        patches = list_pr_file_patches(pr["owner"], pr["repo"], int(pr["number"]))
        changed_paths = frozenset(patches.keys())
        commentable_lines = commentable_lines_by_path(patches)
    except GhError as e:
        logger.warning("Could not list PR diff for inline filter: %s", e)

    inline_findings, summary_only = partition_findings_for_github_post(
        findings, changed_paths, commentable_lines
    )
    review_comments = build_review_comments(inline_findings)
    text, appended_summary = merge_summary_findings_into_review_body(
        text, summary_only, changed_paths=changed_paths
    )

    try:
        event = _ACTION_TO_EVENT.get(body.action)
        if event is None:
            raise HTTPException(status_code=400, detail=f"Invalid action {body.action}")
        # Summary-only comment action with no inlines: keep issue-comment behavior.
        if body.action == "comment" and not review_comments:
            res = post_pr_comment(pr["owner"], pr["repo"], int(pr["number"]), text)
            gh_url = res.get("output")
        else:
            res = submit_pr_review(
                pr["owner"],
                pr["repo"],
                int(pr["number"]),
                text,
                event,
                commit_id=head_sha or None,
                comments=review_comments or None,
                changed_paths=changed_paths,
                commentable_lines=commentable_lines,
            )
            gh_url = res.get("html_url")
            gh_review_id = res.get("id")
            dropped = list(res.get("dropped_comments") or [])
            skipped_off_diff = int(res.get("skipped_off_diff_count") or 0)
            skipped_bad_line = int(res.get("skipped_bad_line_count") or 0)
            posted_inline = int(res.get("posted_inline_count") or 0)
    except GhError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    dedup = row.get("dedup_summary") or ""
    if review_comments:
        note = f"Posted {posted_inline} inline comment(s)"
        if skipped_off_diff:
            note += f"; skipped {skipped_off_diff} off-diff path(s)"
        if skipped_bad_line:
            note += f"; skipped {skipped_bad_line} non-diff line(s)"
        attach_dropped = len(dropped) - skipped_off_diff - skipped_bad_line
        if attach_dropped > 0:
            note += f"; dropped {attach_dropped} bad line anchor(s)"
        dedup = f"{dedup}; {note}".strip("; ")
    if appended_summary:
        extra = f"appended {appended_summary} finding(s) to review body"
        dedup = f"{dedup}; {extra}".strip("; ")

    updated = mark_consolidation_feedback_provided(
        conn,
        consolidation_id,
        gh_action=body.action,
        gh_comment_url=gh_url,
        gh_review_id=gh_review_id,
    )
    action = create_feedback_action(
        conn,
        pr_id=int(row["pr_id"]),
        action=body.action,
        source_run_ids=list(row.get("source_run_ids") or []),
        body=text,
        gh_comment_url=gh_url,
        gh_review_id=gh_review_id,
        dedup_summary=dedup or None,
    )
    return {
        "consolidation": updated,
        "action": action,
        "inline_comments": posted_inline,
        "dropped_inline_comments": len(dropped),
        "skipped_off_diff_comments": skipped_off_diff,
        "skipped_bad_line_comments": skipped_bad_line,
    }
