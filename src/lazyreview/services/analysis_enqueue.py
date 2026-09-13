"""Helpers to enqueue PR analyses from sync, API, or head-move paths."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from lazyreview.db import (
    create_pr_analysis,
    create_pr_event,
    get_current_pr_analysis,
    get_pr,
)
from lazyreview.services.analysis_jobs import AnalysisRunner, analyses_dir
from lazyreview.services.settings import get_pr_analysis_config

logger = logging.getLogger(__name__)


def ensure_analysis_for_pr(
    conn: sqlite3.Connection,
    settings: Any,
    runner: AnalysisRunner | None,
    pr_id: int,
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    """
    Ensure a pr_analyses row exists for the PR's current head and enqueue it.

    If force=True, re-queue failed/cancelled (and replace succeeded) for a fresh run.
    Returns the analysis row, or None if the PR has no head_sha.
    """
    pr = get_pr(conn, pr_id)
    if not pr:
        return None
    head_sha = (pr.get("head_sha") or "").strip()
    if not head_sha:
        return None

    existing = get_current_pr_analysis(conn, pr_id)
    if existing and not force and existing["status"] in ("queued", "running", "succeeded"):
        return existing

    if force and existing and existing["status"] in ("queued", "running"):
        if runner is not None:
            try:
                runner.cancel(int(existing["id"]))
            except (KeyError, ValueError):
                pass

    if force and existing and existing["status"] == "succeeded":
        # Mark prior success cancelled so create_pr_analysis can insert a new queued row
        # for the same head (UNIQUE). Easiest: update in place to queued.
        from lazyreview.db import update_pr_analysis

        cfg = get_pr_analysis_config(conn)
        updated = update_pr_analysis(
            conn,
            int(existing["id"]),
            status="queued",
            agent=cfg["agent"],
            model=cfg["model"],
            effort=cfg["effort"] or None,
            brief_markdown=None,
            brief_path=None,
            error_message=None,
            started_at=None,
            finished_at=None,
            duration_ms=None,
            cli_command=None,
        )
        assert updated is not None
        work_dir = analyses_dir(settings) / str(updated["id"])
        work_dir.mkdir(parents=True, exist_ok=True)
        updated = update_pr_analysis(conn, int(updated["id"]), workdir_path=str(work_dir)) or updated
        _record_analysis_triggered(conn, updated)
        if runner is not None:
            runner.enqueue(int(updated["id"]))
        return updated

    cfg = get_pr_analysis_config(conn)
    was_terminal = bool(
        existing and existing["status"] in ("failed", "cancelled")
    )
    row = create_pr_analysis(
        conn,
        pr_id=pr_id,
        head_sha=head_sha,
        agent=cfg["agent"],
        model=cfg["model"],
        effort=cfg["effort"] or None,
        workdir_path="",
    )
    work_dir = analyses_dir(settings) / str(row["id"])
    work_dir.mkdir(parents=True, exist_ok=True)
    from lazyreview.db import update_pr_analysis

    row = update_pr_analysis(conn, int(row["id"]), workdir_path=str(work_dir)) or row
    # Record when we newly queue or re-queue a terminal analysis.
    if row["status"] == "queued" and (existing is None or was_terminal or force):
        _record_analysis_triggered(conn, row)
    if runner is not None and row["status"] == "queued":
        runner.enqueue(int(row["id"]))
    return row


def _record_analysis_triggered(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    create_pr_event(
        conn,
        pr_id=int(row["pr_id"]),
        kind="analysis_triggered",
        message="PR analysis triggered",
        agent=row.get("agent") or None,
        model=row.get("model") or None,
        effort=row.get("effort") or None,
        analysis_id=int(row["id"]),
    )
