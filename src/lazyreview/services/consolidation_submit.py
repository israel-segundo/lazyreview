"""Post consolidation feedback to GitHub (manual or label-gated auto-submit)."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from lazyreview.db import (
    create_feedback_action,
    get_consolidation,
    get_pr,
    mark_consolidation_feedback_provided,
    upsert_pr,
)
from lazyreview.services.analysis_eligibility import review_blocked_reason
from lazyreview.services.gh import GhError, post_pr_comment, submit_pr_review, view_pr
from lazyreview.services.pr_diff import commentable_lines_by_path, list_pr_file_patches
from lazyreview.services.pr_labels import pr_has_any_label
from lazyreview.services.review_post import (
    build_review_comments,
    merge_summary_findings_into_review_body,
    parse_findings_list,
    partition_findings_for_github_post,
)
from lazyreview.services.settings import get_auto_submit_labels

logger = logging.getLogger(__name__)

_ACTION_TO_EVENT = {
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}
_VALID_ACTIONS = frozenset(_ACTION_TO_EVENT)


class ConsolidationSubmitError(Exception):
    """Raised when a consolidation cannot be posted to GitHub."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def submit_consolidation_to_github(
    conn: sqlite3.Connection,
    consolidation_id: int,
    *,
    action: str,
    body: str | None = None,
    auto_submitted: bool = False,
) -> dict[str, Any]:
    """
    Post the consolidation publish draft to GitHub and record feedback.

    Returns ``{consolidation, action, inline_comments, ...}``.
    """
    row = get_consolidation(conn, consolidation_id)
    if not row:
        raise ConsolidationSubmitError("Consolidation not found", status_code=404)
    if row["status"] != "succeeded":
        raise ConsolidationSubmitError(
            f"Consolidation is {row['status']}, cannot post",
            status_code=400,
        )
    if int(row.get("feedback_provided") or 0):
        raise ConsolidationSubmitError("Feedback already provided", status_code=400)
    if not int(row.get("ready_to_post") or 0) or not (row.get("publish_body") or "").strip():
        raise ConsolidationSubmitError(
            "GitHub draft is not ready yet (publish transform incomplete)",
            status_code=409,
        )

    action_key = (action or "").strip()
    if action_key not in _VALID_ACTIONS:
        raise ConsolidationSubmitError(f"Invalid action {action}", status_code=400)

    pr = get_pr(conn, int(row["pr_id"]))
    if not pr:
        raise ConsolidationSubmitError("PR not found", status_code=404)
    blocked = review_blocked_reason(pr)
    if blocked:
        raise ConsolidationSubmitError(blocked, status_code=409)

    text = body if body is not None else (row.get("publish_body") or "")
    if not str(text).strip():
        raise ConsolidationSubmitError("Empty publish body", status_code=400)

    from lazyreview.findings_taxonomy import coerce_findings_for_persist

    findings = coerce_findings_for_persist(
        parse_findings_list(row.get("findings_json") or row.get("findings")),
        default_origin="model",
    )
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
        event = _ACTION_TO_EVENT[action_key]
        if action_key == "comment" and not review_comments:
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
        raise ConsolidationSubmitError(str(e), status_code=400) from e

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
    if auto_submitted:
        dedup = f"{dedup}; auto-submitted via PR label".strip("; ")

    updated = mark_consolidation_feedback_provided(
        conn,
        consolidation_id,
        gh_action=action_key,
        gh_comment_url=gh_url,
        gh_review_id=gh_review_id,
        auto_submitted=auto_submitted,
    )
    action_row = create_feedback_action(
        conn,
        pr_id=int(row["pr_id"]),
        action=action_key,
        source_run_ids=list(row.get("source_run_ids") or []),
        body=text,
        gh_comment_url=gh_url,
        gh_review_id=gh_review_id,
        dedup_summary=dedup or None,
        auto_submitted=auto_submitted,
    )
    return {
        "consolidation": updated,
        "action": action_row,
        "inline_comments": posted_inline,
        "dropped_inline_comments": len(dropped),
        "skipped_off_diff_comments": skipped_off_diff,
        "skipped_bad_line_comments": skipped_bad_line,
        "auto_submitted": bool(auto_submitted),
    }


def _refresh_pr_labels(conn: sqlite3.Connection, pr: dict[str, Any]) -> dict[str, Any]:
    """Best-effort live label refresh so auto-submit sees newly added labels."""
    url = (pr.get("url") or "").strip()
    if not url:
        return pr
    try:
        live = view_pr(url)
    except Exception as e:
        logger.warning("Could not refresh PR labels before auto-submit: %s", e)
        return pr
    try:
        upsert_pr(
            conn,
            owner=live["owner"],
            repo=live["repo"],
            number=live["number"],
            url=live["url"],
            title=live.get("title") or pr.get("title") or "",
            author=live.get("author") or pr.get("author") or "",
            base_ref=live.get("base_ref") or pr.get("base_ref") or "",
            head_ref=live.get("head_ref") or pr.get("head_ref") or "",
            head_sha=live.get("head_sha") or pr.get("head_sha") or "",
            state=live.get("state") or pr.get("state") or "open",
            is_draft=bool(live.get("is_draft")),
            in_merge_queue=bool(live.get("in_merge_queue")),
            has_merge_conflicts=bool(live.get("has_merge_conflicts")),
            labels=live.get("labels") if "labels" in live else None,
            additions=live.get("additions"),
            deletions=live.get("deletions"),
            changed_files=live.get("changed_files"),
            head_updated_at=live.get("head_updated_at"),
            review_requested=None,
        )
    except Exception as e:
        logger.warning("Could not persist refreshed PR labels: %s", e)
        return pr
    return get_pr(conn, int(pr["id"])) or pr


def maybe_auto_submit_consolidation(
    conn: sqlite3.Connection, consolidation_id: int
) -> dict[str, Any] | None:
    """
    If the PR carries a configured auto-submit label, post the recommended verdict.

    Returns the submit result dict, or None when auto-submit does not apply / fails
    soft (logged). Hard GitHub errors are logged and swallowed so consolidation
    success is not rolled back.
    """
    row = get_consolidation(conn, consolidation_id)
    if not row:
        return None
    if row.get("status") != "succeeded":
        return None
    if int(row.get("feedback_provided") or 0):
        return None
    if not int(row.get("ready_to_post") or 0):
        return None

    configured = get_auto_submit_labels(conn)
    if not configured:
        return None

    pr = get_pr(conn, int(row["pr_id"]))
    if not pr:
        return None
    pr = _refresh_pr_labels(conn, pr)
    if not pr_has_any_label(pr, configured):
        return None

    action = str(row.get("recommended_action") or "comment").strip()
    if action not in _VALID_ACTIONS:
        action = "comment"

    try:
        result = submit_consolidation_to_github(
            conn,
            consolidation_id,
            action=action,
            body=None,
            auto_submitted=True,
        )
        logger.info(
            "Auto-submitted consolidation %s for PR %s/%s#%s as %s (labels=%s)",
            consolidation_id,
            pr.get("owner"),
            pr.get("repo"),
            pr.get("number"),
            action,
            configured,
        )
        return result
    except ConsolidationSubmitError as e:
        logger.warning(
            "Auto-submit skipped for consolidation %s: %s",
            consolidation_id,
            e.message,
        )
        return None
    except Exception:
        logger.exception("Auto-submit failed for consolidation %s", consolidation_id)
        return None
