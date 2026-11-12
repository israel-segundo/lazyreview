"""Hard gate: reviews/consolidations require a succeeded analysis for the current head."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import HTTPException

from lazyreview.db import analysis_succeeded_for_current_head, get_current_pr_analysis, get_pr
from lazyreview.services.analysis_eligibility import review_blocked_reason


def require_succeeded_analysis(conn: sqlite3.Connection, pr_id: int) -> dict[str, Any]:
    """Return the current-head analysis row or raise HTTP 409."""
    pr = get_pr(conn, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    analysis = get_current_pr_analysis(conn, pr_id)
    if analysis and analysis.get("status") == "succeeded":
        return analysis
    status = analysis.get("status") if analysis else None
    if status in ("queued", "running"):
        detail = "PR analysis is still in progress for the current head"
    elif status == "failed":
        detail = "PR analysis failed for the current head — retry analysis before reviewing"
    elif status == "cancelled":
        detail = "PR analysis was cancelled — start analysis before reviewing"
    else:
        detail = "PR analysis has not completed for the current head"
    raise HTTPException(
        status_code=409,
        detail={
            "message": detail,
            "analysis_status": status,
            "analysis_id": int(analysis["id"]) if analysis else None,
            "head_sha": pr.get("head_sha"),
        },
    )


def require_reviewable_pr(conn: sqlite3.Connection, pr_id: int) -> dict[str, Any]:
    """Return the PR or raise HTTP 409 when merge-queue / closed / merged."""
    pr = get_pr(conn, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    blocked = review_blocked_reason(pr)
    if blocked:
        raise HTTPException(
            status_code=409,
            detail={
                "message": blocked,
                "in_merge_queue": bool(pr.get("in_merge_queue")),
                "state": pr.get("state"),
            },
        )
    return pr


def analysis_ready(conn: sqlite3.Connection, pr_id: int) -> bool:
    return analysis_succeeded_for_current_head(conn, pr_id)
