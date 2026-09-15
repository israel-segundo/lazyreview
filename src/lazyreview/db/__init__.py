"""Database access helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def upsert_pr(
    conn: sqlite3.Connection,
    *,
    owner: str,
    repo: str,
    number: int,
    url: str,
    title: str,
    base_ref: str,
    head_ref: str,
    head_sha: str,
    state: str = "open",
    review_requested: bool | None = None,
    author: str | None = None,
    is_draft: bool | None = None,
    additions: int | None = None,
    deletions: int | None = None,
    changed_files: int | None = None,
    head_updated_at: str | None = None,
) -> dict[str, Any]:
    now = utcnow()
    existing = conn.execute(
        "SELECT * FROM prs WHERE owner = ? AND repo = ? AND number = ?",
        (owner, repo, number),
    ).fetchone()
    old_head = (existing["head_sha"] or "") if existing is not None else ""
    if review_requested is None:
        rr = int(existing["review_requested"]) if existing is not None else 0
    else:
        rr = 1 if review_requested else 0

    def _keep(col: str, new: Any, default: Any = None) -> Any:
        if new is not None:
            return new
        if existing is not None and existing[col] is not None:
            return existing[col]
        return default

    author_v = _keep("author", author, "")
    draft_v = int(bool(is_draft)) if is_draft is not None else (
        int(existing["is_draft"]) if existing is not None else 0
    )
    additions_v = _keep("additions", additions)
    deletions_v = _keep("deletions", deletions)
    changed_v = _keep("changed_files", changed_files)

    head_changed = bool(head_sha) and (existing is None or head_sha != old_head)
    existing_head_at = (
        (existing["head_updated_at"] or "") if existing is not None else ""
    )
    if head_updated_at:
        # Commit time of the current head is the source of truth for age.
        head_at = head_updated_at
    elif head_changed or not existing_head_at:
        head_at = now
    else:
        head_at = existing_head_at

    conn.execute(
        """
        INSERT INTO prs (
          owner, repo, number, url, title, author, base_ref, head_ref, head_sha,
          state, is_draft, additions, deletions, changed_files,
          review_requested, head_updated_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(owner, repo, number) DO UPDATE SET
          url = excluded.url,
          title = excluded.title,
          author = excluded.author,
          base_ref = excluded.base_ref,
          head_ref = excluded.head_ref,
          head_sha = excluded.head_sha,
          state = excluded.state,
          is_draft = excluded.is_draft,
          additions = excluded.additions,
          deletions = excluded.deletions,
          changed_files = excluded.changed_files,
          review_requested = excluded.review_requested,
          head_updated_at = excluded.head_updated_at,
          updated_at = excluded.updated_at
        """,
        (
            owner,
            repo,
            number,
            url,
            title,
            author_v or "",
            base_ref,
            head_ref,
            head_sha,
            state,
            draft_v,
            additions_v,
            deletions_v,
            changed_v,
            rr,
            head_at,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM prs WHERE owner = ? AND repo = ? AND number = ?",
        (owner, repo, number),
    ).fetchone()
    assert row is not None
    is_new = existing is None
    if is_new:
        create_pr_event(
            conn,
            pr_id=int(row["id"]),
            kind="pr_created",
            message="Added to LazyReview",
            created_at=row["created_at"],
            commit=False,
        )
    # New head → prior review runs are stale for consolidation.
    head_moved = bool(head_sha and old_head and head_sha != old_head)
    if head_moved:
        deprecate_runs_for_pr(conn, int(row["id"]))
    conn.commit()
    pr = dict(row)
    if head_moved:
        from lazyreview.services.lifecycle import on_pr_head_changed

        on_pr_head_changed(conn, int(pr["id"]))
    from lazyreview.services.lifecycle import on_pr_upserted

    on_pr_upserted(conn, pr, is_new=is_new)
    return pr


def list_stale_review_request_prs(
    conn: sqlite3.Connection,
    keep: list[tuple[str, str, int]],
) -> list[dict[str, Any]]:
    """PRs currently flagged review_requested that are not in keep."""
    keep_ids: set[int] = set()
    for owner, repo, number in keep:
        row = conn.execute(
            "SELECT id FROM prs WHERE owner = ? AND repo = ? AND number = ?",
            (owner, repo, number),
        ).fetchone()
        if row:
            keep_ids.add(int(row["id"]))
    rows = conn.execute(
        "SELECT * FROM prs WHERE review_requested = 1"
    ).fetchall()
    return [dict(r) for r in rows if int(r["id"]) not in keep_ids]


def clear_stale_review_requests(
    conn: sqlite3.Connection,
    keep: list[tuple[str, str, int]],
) -> int:
    """Set review_requested=0 for rows not in keep."""
    if not keep:
        cur = conn.execute(
            "UPDATE prs SET review_requested = 0 WHERE review_requested = 1"
        )
        conn.commit()
        return int(cur.rowcount or 0)

    keep_ids: list[int] = []
    for owner, repo, number in keep:
        row = conn.execute(
            "SELECT id FROM prs WHERE owner = ? AND repo = ? AND number = ?",
            (owner, repo, number),
        ).fetchone()
        if row:
            keep_ids.append(int(row["id"]))

    if not keep_ids:
        cur = conn.execute(
            "UPDATE prs SET review_requested = 0 WHERE review_requested = 1"
        )
        conn.commit()
        return int(cur.rowcount or 0)

    placeholders = ",".join("?" for _ in keep_ids)
    cur = conn.execute(
        f"""
        UPDATE prs SET review_requested = 0
        WHERE review_requested = 1
          AND id NOT IN ({placeholders})
        """,
        keep_ids,
    )
    conn.commit()
    return int(cur.rowcount or 0)


def pr_internal_status(conn: sqlite3.Connection, pr: dict[str, Any]) -> dict[str, Any]:
    """
    Lazyreview review workflow status for the PR's current head.

    - needs_review: no successful consolidation submitted for this head
    - feedback_provided: consolidation posted as a comment
    - approved / changes_requested: consolidation posted as that review action

    A new head automatically returns needs_review (prior submissions are for
    an older head_sha).
    """
    head = (pr.get("head_sha") or "").strip()
    if not head:
        return {
            "status": "needs_review",
            "label": "needs review",
            "gh_action": None,
        }
    row = conn.execute(
        """
        SELECT gh_action FROM consolidations
        WHERE pr_id = ?
          AND head_sha = ?
          AND status = 'succeeded'
          AND feedback_provided = 1
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(pr["id"]), head),
    ).fetchone()
    if not row:
        return {
            "status": "needs_review",
            "label": "needs review",
            "gh_action": None,
        }
    action = (row["gh_action"] or "comment").strip() or "comment"
    if action == "approve":
        return {"status": "approved", "label": "approved", "gh_action": action}
    if action == "request_changes":
        return {
            "status": "changes_requested",
            "label": "changes requested",
            "gh_action": action,
        }
    return {
        "status": "feedback_provided",
        "label": "feedback provided",
        "gh_action": action,
    }


def head_age_days(head_updated_at: str | None, *, now: datetime | None = None) -> int | None:
    """Whole days since the current head started awaiting review."""
    if not head_updated_at:
        return None
    raw = str(head_updated_at).strip()
    if not raw:
        return None
    try:
        then = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    else:
        then = then.astimezone(timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0, (current - then).days)


def _attach_internal_status(conn: sqlite3.Connection, pr: dict[str, Any]) -> dict[str, Any]:
    info = pr_internal_status(conn, pr)
    out = dict(pr)
    out["internal_status"] = info["status"]
    out["internal_status_label"] = info["label"]
    out["feedback_gh_action"] = info["gh_action"]
    out["age_days"] = head_age_days(out.get("head_updated_at"))
    return out


_IN_FLIGHT = frozenset({"queued", "running"})
_FINISHED = frozenset({"succeeded", "failed", "cancelled"})


def pr_activity_summary(
    *,
    analysis_status: str | None,
    active_runs: int = 0,
    finished_runs: int = 0,
    active_consolidations: int = 0,
    finished_consolidations: int = 0,
) -> dict[str, Any]:
    """
    Aggregate home-page activity ball for a PR.

    - running: any analysis / review run / consolidation is queued or running
    - done: nothing in flight, but at least one finished activity exists
    - idle: nothing started yet
    """
    analysis = (analysis_status or "").strip().lower() or None
    analysis_running = analysis in _IN_FLIGHT
    analysis_done = analysis in _FINISHED
    active_runs = int(active_runs or 0)
    finished_runs = int(finished_runs or 0)
    active_consolidations = int(active_consolidations or 0)
    finished_consolidations = int(finished_consolidations or 0)

    if analysis_running or active_runs > 0 or active_consolidations > 0:
        parts: list[str] = []
        if analysis_running:
            parts.append("analysis " + (analysis or "running"))
        if active_runs:
            parts.append(
                f"{active_runs} review run{'s' if active_runs != 1 else ''} in progress"
            )
        if active_consolidations:
            parts.append(
                f"{active_consolidations} consolidation"
                f"{'s' if active_consolidations != 1 else ''} in progress"
            )
        return {
            "activity_status": "running",
            "activity_label": " · ".join(parts) or "Activity running",
        }

    if analysis_done or finished_runs > 0 or finished_consolidations > 0:
        parts = []
        if analysis_done:
            parts.append(f"analysis {analysis}")
        if finished_runs:
            parts.append(f"{finished_runs} review run{'s' if finished_runs != 1 else ''}")
        if finished_consolidations:
            parts.append(
                f"{finished_consolidations} consolidation"
                f"{'s' if finished_consolidations != 1 else ''}"
            )
        return {
            "activity_status": "done",
            "activity_label": "Activity finished — " + " · ".join(parts),
        }

    return {
        "activity_status": "idle",
        "activity_label": "No activity yet",
    }


_ACTIVITY_IN_FLIGHT_SQL = "('queued', 'running')"


def list_global_activity(
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
    offset: int = 0,
    running_only: bool = False,
) -> dict[str, Any]:
    """
    Cross-PR timeline of analyses, review runs, and consolidations.

    Sorted newest-first by COALESCE(finished_at, started_at, created_at).
    Returns {items, total, limit, offset}.
    """
    limit = max(1, min(int(limit or 25), 500))
    offset = max(0, int(offset or 0))
    a_filter = f"AND a.status IN {_ACTIVITY_IN_FLIGHT_SQL}" if running_only else ""
    r_filter = f"AND r.status IN {_ACTIVITY_IN_FLIGHT_SQL}" if running_only else ""
    c_filter = f"AND c.status IN {_ACTIVITY_IN_FLIGHT_SQL}" if running_only else ""
    union_sql = f"""
          SELECT
            'analysis' AS kind,
            a.id AS id,
            a.pr_id AS pr_id,
            p.owner AS owner,
            p.repo AS repo,
            p.number AS number,
            p.title AS title,
            a.status AS status,
            a.agent AS agent,
            a.model AS model,
            a.effort AS effort,
            a.head_sha AS head_sha,
            0 AS deprecated,
            0 AS consolidated,
            a.created_at AS created_at,
            a.started_at AS started_at,
            a.finished_at AS finished_at,
            COALESCE(a.finished_at, a.started_at, a.created_at) AS when_at
          FROM pr_analyses a
          JOIN prs p ON p.id = a.pr_id
          WHERE 1=1 {a_filter}

          UNION ALL

          SELECT
            'run' AS kind,
            r.id AS id,
            r.pr_id AS pr_id,
            p.owner AS owner,
            p.repo AS repo,
            p.number AS number,
            p.title AS title,
            r.status AS status,
            r.agent AS agent,
            r.model AS model,
            r.effort AS effort,
            r.head_sha AS head_sha,
            COALESCE(r.deprecated, 0) AS deprecated,
            COALESCE(r.consolidated, 0) AS consolidated,
            r.created_at AS created_at,
            r.started_at AS started_at,
            r.finished_at AS finished_at,
            COALESCE(r.finished_at, r.started_at, r.created_at) AS when_at
          FROM review_runs r
          JOIN prs p ON p.id = r.pr_id
          WHERE 1=1 {r_filter}

          UNION ALL

          SELECT
            'consolidation' AS kind,
            c.id AS id,
            c.pr_id AS pr_id,
            p.owner AS owner,
            p.repo AS repo,
            p.number AS number,
            p.title AS title,
            c.status AS status,
            c.agent AS agent,
            c.model AS model,
            c.effort AS effort,
            c.head_sha AS head_sha,
            0 AS deprecated,
            0 AS consolidated,
            c.created_at AS created_at,
            c.started_at AS started_at,
            c.finished_at AS finished_at,
            COALESCE(c.finished_at, c.started_at, c.created_at) AS when_at
          FROM consolidations c
          JOIN prs p ON p.id = c.pr_id
          WHERE 1=1 {c_filter}
    """
    total = int(
        conn.execute(f"SELECT COUNT(*) AS n FROM ({union_sql})").fetchone()["n"]
    )
    rows = conn.execute(
        f"""
        SELECT * FROM (
          {union_sql}
        )
        ORDER BY when_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        item["id"] = int(item["id"])
        item["pr_id"] = int(item["pr_id"])
        item["number"] = int(item["number"])
        item["deprecated"] = bool(int(item.get("deprecated") or 0))
        item["consolidated"] = bool(int(item.get("consolidated") or 0))
        item["pr_label"] = f"{item.get('repo') or ''}#{item.get('number')}"
        out.append(item)
    return {
        "items": out,
        "total": total,
        "limit": limit,
        "offset": offset,
    }

def count_global_activity_running(conn: sqlite3.Connection) -> int:
    """How many analyses / runs / consolidations are queued or running."""
    row = conn.execute(
        f"""
        SELECT
          (SELECT COUNT(*) FROM pr_analyses WHERE status IN {_ACTIVITY_IN_FLIGHT_SQL})
          + (SELECT COUNT(*) FROM review_runs WHERE status IN {_ACTIVITY_IN_FLIGHT_SQL})
          + (SELECT COUNT(*) FROM consolidations WHERE status IN {_ACTIVITY_IN_FLIGHT_SQL})
          AS n
        """
    ).fetchone()
    return int(row["n"] if row is not None else 0)


def list_prs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.*,
          (SELECT COUNT(*) FROM review_runs r WHERE r.pr_id = p.id) AS run_count,
          (SELECT r.status FROM review_runs r WHERE r.pr_id = p.id ORDER BY r.id DESC LIMIT 1) AS latest_status,
          (SELECT COUNT(*) FROM review_runs r
             WHERE r.pr_id = p.id AND r.status IN ('queued', 'running')) AS active_run_count,
          (SELECT COUNT(*) FROM review_runs r
             WHERE r.pr_id = p.id AND r.status IN ('succeeded', 'failed', 'cancelled')) AS finished_run_count,
          (SELECT COUNT(*) FROM consolidations c
             WHERE c.pr_id = p.id AND c.status IN ('queued', 'running')) AS active_consolidation_count,
          (SELECT COUNT(*) FROM consolidations c
             WHERE c.pr_id = p.id AND c.status IN ('succeeded', 'failed', 'cancelled')) AS finished_consolidation_count,
          CASE
            WHEN (SELECT COUNT(*) FROM review_runs r WHERE r.pr_id = p.id) = 0 THEN 0
            WHEN p.head_sha IS NULL OR p.head_sha = '' THEN 0
            WHEN EXISTS (
              SELECT 1 FROM review_runs r
              WHERE r.pr_id = p.id AND r.head_sha = p.head_sha
            ) THEN 0
            ELSE 1
          END AS runs_stale
        FROM prs p
        ORDER BY
          CASE WHEN p.state = 'open' THEN 0 ELSE 1 END,
          CASE WHEN p.head_updated_at IS NULL OR p.head_updated_at = '' THEN 1 ELSE 0 END,
          p.head_updated_at ASC,
          p.id ASC
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        pr = _attach_internal_status(conn, dict(r))
        pr["run_count"] = int(pr.get("run_count") or 0)
        pr["runs_stale"] = bool(int(pr.get("runs_stale") or 0))
        analysis = get_current_pr_analysis(conn, int(pr["id"]))
        pr["analysis_status"] = analysis["status"] if analysis else None
        pr["analysis_id"] = int(analysis["id"]) if analysis else None
        pr["analysis_error"] = (analysis.get("error_message") if analysis else None)
        pr["analysis_agent"] = (analysis.get("agent") if analysis else None)
        pr["analysis_model"] = (analysis.get("model") if analysis else None)
        pr["analysis_effort"] = (analysis.get("effort") if analysis else None)
        pr["analysis_duration_ms"] = (analysis.get("duration_ms") if analysis else None)
        pr.update(
            pr_activity_summary(
                analysis_status=pr.get("analysis_status"),
                active_runs=pr.get("active_run_count"),
                finished_runs=pr.get("finished_run_count"),
                active_consolidations=pr.get("active_consolidation_count"),
                finished_consolidations=pr.get("finished_consolidation_count"),
            )
        )
        out.append(pr)
    return out


def get_latest_succeeded_pr_analysis(
    conn: sqlite3.Connection, pr_id: int
) -> dict[str, Any] | None:
    """Most recent succeeded analysis for a PR (any head), preferring non-empty briefs."""
    return row_to_dict(
        conn.execute(
            """
            SELECT * FROM pr_analyses
            WHERE pr_id = ?
              AND status = 'succeeded'
              AND brief_markdown IS NOT NULL
              AND TRIM(brief_markdown) != ''
            ORDER BY id DESC
            LIMIT 1
            """,
            (pr_id,),
        ).fetchone()
    )


def analysis_display_for_pr(conn: sqlite3.Connection, pr: dict[str, Any]) -> dict[str, Any]:
    """
    Status for the current head, with brief fallback to the latest succeeded analysis.

    When the head moves and analysis is not re-run, Overview still shows the previous
    brief and marks it stale. Reviews remain gated on current-head success.
    """
    pr_id = int(pr["id"])
    current_head = (pr.get("head_sha") or "").strip()
    current = get_current_pr_analysis(conn, pr_id)
    latest_ok = get_latest_succeeded_pr_analysis(conn, pr_id)

    brief_src: dict[str, Any] | None = None
    if current and (current.get("brief_markdown") or "").strip():
        brief_src = current
    elif latest_ok:
        brief_src = latest_ok

    brief_head = ((brief_src or {}).get("head_sha") or "").strip()
    stale = bool(brief_src and current_head and brief_head and brief_head != current_head)
    meta = brief_src or current

    return {
        "analysis_status": current["status"] if current else None,
        "analysis_id": int(current["id"]) if current else None,
        "analysis_error": (current.get("error_message") if current else None),
        "analysis_brief": (brief_src.get("brief_markdown") if brief_src else None),
        "analysis_stale": stale,
        "analysis_brief_head_sha": brief_head or None,
        "analysis_agent": (meta.get("agent") if meta else None),
        "analysis_model": (meta.get("model") if meta else None),
        "analysis_effort": (meta.get("effort") if meta else None),
        "analysis_duration_ms": (meta.get("duration_ms") if meta else None),
    }


def get_pr(conn: sqlite3.Connection, pr_id: int) -> dict[str, Any] | None:
    row = row_to_dict(
        conn.execute("SELECT * FROM prs WHERE id = ?", (pr_id,)).fetchone()
    )
    if row is None:
        return None
    pr = _attach_internal_status(conn, row)
    pr.update(analysis_display_for_pr(conn, pr))
    return pr


def create_run(
    conn: sqlite3.Connection,
    *,
    pr_id: int,
    agent: str,
    model: str,
    effort: str | None,
    skill: str,
    head_sha: str,
    workdir_path: str,
) -> dict[str, Any]:
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, effort, skill, head_sha, status, workdir_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
        """,
        (pr_id, agent, model, effort, skill, head_sha, workdir_path, utcnow()),
    )
    conn.commit()
    run_id = cur.lastrowid
    assert run_id is not None
    row = get_run(conn, int(run_id))
    assert row is not None
    return row


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    return row_to_dict(
        conn.execute("SELECT * FROM review_runs WHERE id = ?", (run_id,)).fetchone()
    )


def list_runs_for_pr(conn: sqlite3.Connection, pr_id: int) -> list[dict[str, Any]]:
    return rows_to_list(
        conn.execute(
            "SELECT * FROM review_runs WHERE pr_id = ? ORDER BY id DESC", (pr_id,)
        ).fetchall()
    )


def update_run(conn: sqlite3.Connection, run_id: int, **fields: Any) -> dict[str, Any] | None:
    if not fields:
        return get_run(conn, run_id)
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE review_runs SET {cols} WHERE id = ?", (*fields.values(), run_id))
    conn.commit()
    return get_run(conn, run_id)


def reset_run_for_rerun(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    head_sha: str | None = None,
) -> dict[str, Any] | None:
    """Clear prior results and queue the same run config again."""
    replace_findings(conn, run_id, [])
    fields: dict[str, Any] = {
        "status": "queued",
        "started_at": None,
        "finished_at": None,
        "duration_ms": None,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "findings_sev1": 0,
        "findings_sev2": 0,
        "findings_sev3": 0,
        "report_md_path": None,
        "report_html_path": None,
        "prompt_path": None,
        "log_stdout_path": None,
        "log_stderr_path": None,
        "cli_command": None,
        "error_message": None,
        "consolidated": 0,
    }
    if head_sha is not None:
        fields["head_sha"] = head_sha
    return update_run(conn, run_id, **fields)


def deprecate_runs_for_pr(conn: sqlite3.Connection, pr_id: int) -> int:
    """Mark all non-deprecated runs on a PR as deprecated (new head arrived)."""
    cur = conn.execute(
        """
        UPDATE review_runs
        SET deprecated = 1
        WHERE pr_id = ? AND COALESCE(deprecated, 0) = 0
        """,
        (pr_id,),
    )
    return int(cur.rowcount or 0)


def mark_runs_consolidated(conn: sqlite3.Connection, run_ids: list[int]) -> int:
    if not run_ids:
        return 0
    placeholders = ",".join("?" for _ in run_ids)
    cur = conn.execute(
        f"""
        UPDATE review_runs
        SET consolidated = 1
        WHERE id IN ({placeholders})
        """,
        tuple(run_ids),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def sync_run_deprecation_for_pr(conn: sqlite3.Connection, pr_id: int) -> int:
    """Deprecate runs whose head_sha no longer matches the PR's current head."""
    pr = get_pr(conn, pr_id)
    if not pr:
        return 0
    head = (pr.get("head_sha") or "").strip()
    if not head:
        return 0
    cur = conn.execute(
        """
        UPDATE review_runs
        SET deprecated = 1
        WHERE pr_id = ?
          AND COALESCE(deprecated, 0) = 0
          AND head_sha != ''
          AND head_sha != ?
        """,
        (pr_id, head),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def run_is_selectable(run: dict[str, Any]) -> bool:
    """Succeeded, not consolidated, not deprecated — eligible for consolidate checkboxes."""
    return (
        run.get("status") == "succeeded"
        and not int(run.get("consolidated") or 0)
        and not int(run.get("deprecated") or 0)
    )


def create_consolidation(
    conn: sqlite3.Connection,
    *,
    pr_id: int,
    head_sha: str,
    source_run_ids: list[int],
    agent: str,
    model: str,
    effort: str | None,
    workdir_path: str,
    status: str = "queued",
) -> dict[str, Any]:
    if status not in ("queued", "dormant"):
        raise ValueError(f"Invalid initial consolidation status: {status}")
    cur = conn.execute(
        """
        INSERT INTO consolidations (
          pr_id, head_sha, source_run_ids, agent, model, effort, status, workdir_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pr_id,
            head_sha or "",
            json.dumps([int(x) for x in source_run_ids]),
            agent,
            model,
            effort,
            status,
            workdir_path,
            utcnow(),
        ),
    )
    conn.commit()
    row = get_consolidation(conn, int(cur.lastrowid))
    assert row is not None
    return row


def get_consolidation(conn: sqlite3.Connection, consolidation_id: int) -> dict[str, Any] | None:
    row = row_to_dict(
        conn.execute(
            "SELECT * FROM consolidations WHERE id = ?", (consolidation_id,)
        ).fetchone()
    )
    return _normalize_consolidation(row) if row else None


def list_consolidations_for_pr(conn: sqlite3.Connection, pr_id: int) -> list[dict[str, Any]]:
    rows = rows_to_list(
        conn.execute(
            "SELECT * FROM consolidations WHERE pr_id = ? ORDER BY id DESC",
            (pr_id,),
        ).fetchall()
    )
    return [_normalize_consolidation(r) for r in rows]


def update_consolidation(
    conn: sqlite3.Connection, consolidation_id: int, **fields: Any
) -> dict[str, Any] | None:
    if not fields:
        return get_consolidation(conn, consolidation_id)
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE consolidations SET {cols} WHERE id = ?",
        (*fields.values(), consolidation_id),
    )
    conn.commit()
    return get_consolidation(conn, consolidation_id)


def record_external_pr_review(
    conn: sqlite3.Connection,
    pr: dict[str, Any],
    *,
    gh_action: str,
    gh_review_id: str,
    body: str = "",
    gh_comment_url: str | None = None,
) -> bool:
    """
    Record the authenticated user's GitHub review when lazyreview has no
    submitted consolidation for the current head.
    """
    pr_id = int(pr["id"])
    head = (pr.get("head_sha") or "").strip()
    if not head or gh_action not in ("approve", "request_changes", "comment"):
        return False
    if pr_internal_status(conn, pr)["status"] != "needs_review":
        return False
    if gh_review_id:
        existing = conn.execute(
            """
            SELECT id FROM consolidations
            WHERE pr_id = ? AND head_sha = ? AND agent = 'external'
              AND gh_review_id = ?
            """,
            (pr_id, head, gh_review_id),
        ).fetchone()
        if existing:
            return False
    conn.execute(
        """
        INSERT INTO consolidations (
          pr_id, head_sha, source_run_ids, agent, model, effort, status, body,
          feedback_provided, gh_action, gh_review_id, gh_comment_url, workdir_path, created_at
        ) VALUES (?, ?, '[]', 'external', '', NULL, 'succeeded', ?, 1, ?, ?, ?, '', ?)
        """,
        (
            pr_id,
            head,
            body,
            gh_action,
            gh_review_id or None,
            gh_comment_url,
            utcnow(),
        ),
    )
    conn.commit()
    return True


def mark_consolidation_feedback_provided(
    conn: sqlite3.Connection,
    consolidation_id: int,
    *,
    gh_action: str,
    gh_comment_url: str | None = None,
    gh_review_id: str | None = None,
) -> dict[str, Any] | None:
    return update_consolidation(
        conn,
        consolidation_id,
        feedback_provided=1,
        gh_action=gh_action,
        gh_comment_url=gh_comment_url,
        gh_review_id=str(gh_review_id) if gh_review_id is not None else None,
    )


def active_consolidation_run_ids(
    conn: sqlite3.Connection,
    pr_id: int,
    *,
    statuses: tuple[str, ...] = ("dormant", "queued", "running"),
) -> set[int]:
    """Source run ids in consolidations with the given statuses for this PR."""
    if not statuses:
        return set()
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"""
        SELECT source_run_ids FROM consolidations
        WHERE pr_id = ? AND status IN ({placeholders})
        """,
        (pr_id, *statuses),
    ).fetchall()
    out: set[int] = set()
    for row in rows:
        try:
            ids = json.loads(row["source_run_ids"] or "[]")
        except json.JSONDecodeError:
            continue
        for rid in ids:
            out.add(int(rid))
    return out


def in_flight_consolidation_run_ids(conn: sqlite3.Connection, pr_id: int) -> set[int]:
    """Source run ids in a queued/running consolidation (not dormant/waiting)."""
    return active_consolidation_run_ids(conn, pr_id, statuses=("queued", "running"))


def dormant_consolidation_run_ids(conn: sqlite3.Connection, pr_id: int) -> set[int]:
    """Source run ids reserved by a dormant (waiting-for-runs) consolidation."""
    return active_consolidation_run_ids(conn, pr_id, statuses=("dormant",))


def _normalize_consolidation(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    try:
        out["source_run_ids"] = [int(x) for x in json.loads(out.get("source_run_ids") or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        out["source_run_ids"] = []
    if out.get("findings_json"):
        try:
            out["findings"] = json.loads(out["findings_json"])
        except json.JSONDecodeError:
            out["findings"] = []
    else:
        out["findings"] = []
    return out


def replace_findings(
    conn: sqlite3.Connection,
    run_id: int,
    findings: list[dict[str, Any]],
) -> None:
    conn.execute("DELETE FROM findings WHERE run_id = ?", (run_id,))
    for f in findings:
        conn.execute(
            """
            INSERT INTO findings (run_id, external_id, severity, title, body, path, line, disposition, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                f["external_id"],
                f["severity"],
                f["title"],
                f.get("body", ""),
                f.get("path"),
                f.get("line"),
                f.get("disposition"),
                json.dumps(f.get("raw") or f),
            ),
        )
    conn.commit()


def list_findings(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    return rows_to_list(
        conn.execute(
            "SELECT * FROM findings WHERE run_id = ? ORDER BY severity ASC, id ASC",
            (run_id,),
        ).fetchall()
    )


def create_feedback_action(
    conn: sqlite3.Connection,
    *,
    pr_id: int,
    action: str,
    source_run_ids: list[int],
    body: str,
    gh_comment_url: str | None = None,
    gh_review_id: str | None = None,
    dedup_summary: str | None = None,
) -> dict[str, Any]:
    cur = conn.execute(
        """
        INSERT INTO feedback_actions (pr_id, action, source_run_ids, body, gh_comment_url, gh_review_id, dedup_summary)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pr_id,
            action,
            json.dumps(source_run_ids),
            body,
            gh_comment_url,
            gh_review_id,
            dedup_summary,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM feedback_actions WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    assert row is not None
    return dict(row)


def list_feedback_actions(conn: sqlite3.Connection, pr_id: int) -> list[dict[str, Any]]:
    return rows_to_list(
        conn.execute(
            "SELECT * FROM feedback_actions WHERE pr_id = ? ORDER BY id DESC", (pr_id,)
        ).fetchall()
    )


def analytics_combos(
    conn: sqlite3.Connection,
    *,
    period: str = "all",
) -> list[dict[str, Any]]:
    where, params = _analytics_period_clause(period)
    rows = conn.execute(
        f"""
        SELECT
          agent,
          model,
          COALESCE(effort, '') AS effort,
          COUNT(*) AS runs,
          SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
          SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
          AVG(duration_ms) AS avg_duration_ms,
          AVG(total_tokens) AS avg_total_tokens,
          AVG(cost_usd) AS avg_cost_usd,
          SUM(cost_usd) AS total_cost_usd,
          AVG(findings_sev1) AS avg_sev1,
          AVG(findings_sev2) AS avg_sev2,
          AVG(findings_sev3) AS avg_sev3
        FROM review_runs
        WHERE status IN ('succeeded', 'failed')
          {where}
        GROUP BY agent, model, COALESCE(effort, '')
        ORDER BY runs DESC, agent, model
        """,
        params,
    ).fetchall()
    return rows_to_list(rows)


_ANALYTICS_PERIOD_DAYS = {
    "week": 7,
    "month": 30,
    "3months": 90,
    "6months": 180,
    "all": None,
}

def _analytics_period_clause(period: str) -> tuple[str, list[Any]]:
    from datetime import timedelta

    key = (period or "all").strip().lower()
    days = _ANALYTICS_PERIOD_DAYS.get(key)
    if days is None:
        return "", []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(
        microsecond=0
    ).isoformat()
    # Prefer finished_at; fall back to created_at for odd rows.
    return (
        "AND COALESCE(finished_at, created_at) >= ?",
        [cutoff],
    )


def _parse_run_day(iso: str | None) -> str | None:
    """Extract YYYY-MM-DD (UTC) from a run timestamp."""
    if not iso:
        return None
    text = str(iso).strip()
    if not text:
        return None
    # Prefer the date prefix when present (covers +00:00 / Z / naive).
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date().isoformat()


def analytics_activity(conn: sqlite3.Connection, *, period: str = "month") -> dict[str, Any]:
    """
    Histogram of completed agent review runs (status succeeded or failed).

    Uses the same period window as the rest of the analytics dashboard.
    Buckets are daily for short windows and weekly when the span exceeds ~2 months.
    Zero-filled so the chart always spans the full period.
    """
    return _analytics_activity_buckets(conn, period=period, repo=None)


def _analytics_activity_rows(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    return rows_to_list(
        conn.execute(
            """
            SELECT
              r.status AS status,
              COALESCE(r.finished_at, r.created_at) AS at,
              COALESCE(p.repo, '') AS repo,
              r.pr_id AS pr_id
            FROM review_runs r
            JOIN prs p ON p.id = r.pr_id
            WHERE r.status IN ('succeeded', 'failed')
            """
        ).fetchall()
    )


def _analytics_window(
    period: str, earliest: Any | None
) -> tuple[str, Any, Any, bool]:
    """Return (period_key, start_date, today, use_weekly)."""
    from datetime import date, timedelta

    key = (period or "month").strip().lower()
    if key not in _ANALYTICS_PERIOD_DAYS:
        key = "month"
    today = datetime.now(timezone.utc).date()
    days = _ANALYTICS_PERIOD_DAYS[key]
    if days is None:
        start = earliest or today
    else:
        start = today - timedelta(days=days - 1)
    span = (today - start).days + 1
    return key, start, today, span > 62


def _fill_activity_buckets(
    *,
    by_day: dict[str, dict[str, int]],
    start: Any,
    today: Any,
    use_weekly: bool,
    period_key: str,
) -> list[dict[str, Any]]:
    from datetime import timedelta

    buckets: list[dict[str, Any]] = []
    if use_weekly:
        cursor = start - timedelta(days=start.weekday())
        while cursor <= today:
            week_end = min(cursor + timedelta(days=6), today)
            succeeded = 0
            failed = 0
            d = cursor
            while d <= week_end:
                slot = by_day.get(d.isoformat())
                if slot:
                    succeeded += slot["succeeded"]
                    failed += slot["failed"]
                d += timedelta(days=1)
            buckets.append(
                {
                    "date": cursor.isoformat(),
                    "end_date": week_end.isoformat(),
                    "label": f"{cursor.strftime('%b')} {cursor.day}",
                    "succeeded": succeeded,
                    "failed": failed,
                    "total": succeeded + failed,
                }
            )
            cursor += timedelta(days=7)
    else:
        cursor = start
        while cursor <= today:
            day_s = cursor.isoformat()
            slot = by_day.get(day_s) or {"succeeded": 0, "failed": 0}
            succeeded = int(slot["succeeded"])
            failed = int(slot["failed"])
            if period_key == "week":
                label = f"{cursor.strftime('%a')} {cursor.day}"
            else:
                label = f"{cursor.strftime('%b')} {cursor.day}"
            buckets.append(
                {
                    "date": day_s,
                    "label": label,
                    "succeeded": succeeded,
                    "failed": failed,
                    "total": succeeded + failed,
                }
            )
            cursor += timedelta(days=1)
    return buckets


def _analytics_activity_buckets(
    conn: sqlite3.Connection,
    *,
    period: str = "month",
    repo: str | None = None,
) -> dict[str, Any]:
    from datetime import date

    rows = _analytics_activity_rows(conn)
    repo_key = (repo or "").strip() or None
    by_day: dict[str, dict[str, int]] = {}
    earliest: date | None = None
    for row in rows:
        if repo_key and (row.get("repo") or "") != repo_key:
            continue
        day_s = _parse_run_day(row.get("at"))
        if not day_s:
            continue
        day = date.fromisoformat(day_s)
        if earliest is None or day < earliest:
            earliest = day
        slot = by_day.get(day_s)
        if slot is None:
            slot = {"succeeded": 0, "failed": 0}
            by_day[day_s] = slot
        status = (row.get("status") or "").lower()
        if status == "succeeded":
            slot["succeeded"] += 1
        elif status == "failed":
            slot["failed"] += 1

    key, start, today, use_weekly = _analytics_window(period, earliest)
    buckets = _fill_activity_buckets(
        by_day=by_day,
        start=start,
        today=today,
        use_weekly=use_weekly,
        period_key=key,
    )
    return {
        "period": key,
        "granularity": "week" if use_weekly else "day",
        "repo": repo_key,
        "buckets": buckets,
    }


def _analytics_resolution_rows(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """
    Posted GitHub review actions (comment / approve / request_changes).

    Sourced from consolidations with feedback_provided=1 so external reviews
    synced into lazyreview are included. Timestamp prefers a matching
    feedback_actions.created_at, else finished_at / created_at.
    """
    return rows_to_list(
        conn.execute(
            """
            SELECT
              c.pr_id AS pr_id,
              COALESCE(p.repo, '') AS repo,
              CASE
                WHEN LOWER(TRIM(COALESCE(c.gh_action, ''))) IN
                  ('comment', 'approve', 'request_changes')
                THEN LOWER(TRIM(c.gh_action))
                ELSE 'comment'
              END AS gh_action,
              COALESCE(
                (
                  SELECT fa.created_at
                  FROM feedback_actions fa
                  WHERE fa.pr_id = c.pr_id
                    AND fa.action = CASE
                      WHEN LOWER(TRIM(COALESCE(c.gh_action, ''))) IN
                        ('comment', 'approve', 'request_changes')
                      THEN LOWER(TRIM(c.gh_action))
                      ELSE 'comment'
                    END
                    AND (
                      (c.gh_review_id IS NOT NULL AND fa.gh_review_id = c.gh_review_id)
                      OR (
                        c.gh_comment_url IS NOT NULL
                        AND fa.gh_comment_url = c.gh_comment_url
                      )
                    )
                  ORDER BY fa.id DESC
                  LIMIT 1
                ),
                c.finished_at,
                c.created_at
              ) AS at
            FROM consolidations c
            JOIN prs p ON p.id = c.pr_id
            WHERE c.feedback_provided = 1
            """
        ).fetchall()
    )


def _fill_resolution_buckets(
    *,
    by_day: dict[str, dict[str, int]],
    start: Any,
    today: Any,
    use_weekly: bool,
    period_key: str,
) -> list[dict[str, Any]]:
    from datetime import timedelta

    def empty_slot() -> dict[str, int]:
        return {"approve": 0, "request_changes": 0, "comment": 0}

    buckets: list[dict[str, Any]] = []
    if use_weekly:
        cursor = start - timedelta(days=start.weekday())
        while cursor <= today:
            week_end = min(cursor + timedelta(days=6), today)
            slot = empty_slot()
            d = cursor
            while d <= week_end:
                day_slot = by_day.get(d.isoformat())
                if day_slot:
                    for k in slot:
                        slot[k] += int(day_slot.get(k) or 0)
                d += timedelta(days=1)
            total = slot["approve"] + slot["request_changes"] + slot["comment"]
            buckets.append(
                {
                    "date": cursor.isoformat(),
                    "end_date": week_end.isoformat(),
                    "label": f"{cursor.strftime('%b')} {cursor.day}",
                    **slot,
                    "total": total,
                }
            )
            cursor += timedelta(days=7)
    else:
        cursor = start
        while cursor <= today:
            day_s = cursor.isoformat()
            slot = by_day.get(day_s) or empty_slot()
            approve = int(slot.get("approve") or 0)
            request_changes = int(slot.get("request_changes") or 0)
            comment = int(slot.get("comment") or 0)
            if period_key == "week":
                label = f"{cursor.strftime('%a')} {cursor.day}"
            else:
                label = f"{cursor.strftime('%b')} {cursor.day}"
            buckets.append(
                {
                    "date": day_s,
                    "label": label,
                    "approve": approve,
                    "request_changes": request_changes,
                    "comment": comment,
                    "total": approve + request_changes + comment,
                }
            )
            cursor += timedelta(days=1)
    return buckets


def analytics_prs_by_repo(conn: sqlite3.Connection, *, period: str = "month") -> list[dict[str, Any]]:
    """Distinct PRs resolved (comment / approve / request_changes) in the period, by repo."""
    from datetime import timedelta

    key = (period or "month").strip().lower()
    if key not in _ANALYTICS_PERIOD_DAYS:
        key = "month"
    days = _ANALYTICS_PERIOD_DAYS[key]
    cutoff: str | None = None
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days - 1)).replace(
            microsecond=0
        ).date().isoformat()

    by_repo: dict[str, set[int]] = {}
    for row in _analytics_resolution_rows(conn):
        day_s = _parse_run_day(row.get("at"))
        if not day_s:
            continue
        if cutoff is not None and day_s < cutoff:
            continue
        repo = (row.get("repo") or "").strip() or "unknown"
        pr_id = int(row.get("pr_id") or 0)
        if not pr_id:
            continue
        by_repo.setdefault(repo, set()).add(pr_id)

    out = [
        {"repo": repo, "pr_count": len(pr_ids)}
        for repo, pr_ids in by_repo.items()
    ]
    out.sort(key=lambda x: (-int(x["pr_count"]), str(x["repo"])))
    return out


def analytics_repo_activity(conn: sqlite3.Connection, *, period: str = "month") -> dict[str, Any]:
    """
    Resolved-PR time series for All + each repo (comment / approve / request_changes).

    Client filters which series to plot as a line chart.
    """
    from datetime import date

    rows = _analytics_resolution_rows(conn)
    repos = sorted({(r.get("repo") or "").strip() for r in rows if (r.get("repo") or "").strip()})
    earliest: date | None = None
    for row in rows:
        day_s = _parse_run_day(row.get("at"))
        if not day_s:
            continue
        day = date.fromisoformat(day_s)
        if earliest is None or day < earliest:
            earliest = day
    key, start, today, use_weekly = _analytics_window(period, earliest)

    def buckets_for(repo_filter: str | None) -> list[dict[str, Any]]:
        by_day: dict[str, dict[str, int]] = {}
        for row in rows:
            if repo_filter is not None and (row.get("repo") or "") != repo_filter:
                continue
            day_s = _parse_run_day(row.get("at"))
            if not day_s:
                continue
            day = date.fromisoformat(day_s)
            if day < start or day > today:
                continue
            slot = by_day.get(day_s)
            if slot is None:
                slot = {"approve": 0, "request_changes": 0, "comment": 0}
                by_day[day_s] = slot
            action = (row.get("gh_action") or "comment").strip().lower() or "comment"
            if action not in slot:
                action = "comment"
            slot[action] += 1
        return _fill_resolution_buckets(
            by_day=by_day,
            start=start,
            today=today,
            use_weekly=use_weekly,
            period_key=key,
        )

    series: dict[str, list[dict[str, Any]]] = {"all": buckets_for(None)}
    active_repos: list[str] = []
    for repo in repos:
        buckets = buckets_for(repo)
        if any(int(b.get("total") or 0) for b in buckets):
            series[repo] = buckets
            active_repos.append(repo)

    return {
        "period": key,
        "granularity": "week" if use_weekly else "day",
        "repos": active_repos,
        "series": series,
    }


def analytics_weekday_pattern(conn: sqlite3.Connection, *, period: str = "month") -> dict[str, Any]:
    """
    PRs resolved by weekday (Mon–Sun, UTC resolve day).

    Counts posted review actions (comment / approve / request_changes).
    """
    from datetime import date, timedelta

    key = (period or "month").strip().lower()
    if key not in _ANALYTICS_PERIOD_DAYS:
        key = "month"
    days = _ANALYTICS_PERIOD_DAYS[key]
    cutoff: str | None = None
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days - 1)).replace(
            microsecond=0
        ).date().isoformat()

    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    counts = [0, 0, 0, 0, 0, 0, 0]
    for row in _analytics_resolution_rows(conn):
        day_s = _parse_run_day(row.get("at"))
        if not day_s:
            continue
        if cutoff is not None and day_s < cutoff:
            continue
        weekday = date.fromisoformat(day_s).weekday()  # Mon=0
        counts[weekday] += 1

    return {
        "period": key,
        "labels": labels,
        "counts": counts,
        "total": sum(counts),
    }


def analytics_repo_pattern(conn: sqlite3.Connection, *, period: str = "month") -> dict[str, Any]:
    """
    PRs resolved by repo in the period.

    Counts posted review actions (comment / approve / request_changes).
    """
    from datetime import timedelta

    key = (period or "month").strip().lower()
    if key not in _ANALYTICS_PERIOD_DAYS:
        key = "month"
    days = _ANALYTICS_PERIOD_DAYS[key]
    cutoff: str | None = None
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days - 1)).replace(
            microsecond=0
        ).date().isoformat()

    by_repo: dict[str, int] = {}
    for row in _analytics_resolution_rows(conn):
        day_s = _parse_run_day(row.get("at"))
        if not day_s:
            continue
        if cutoff is not None and day_s < cutoff:
            continue
        repo = (row.get("repo") or "").strip() or "unknown"
        by_repo[repo] = by_repo.get(repo, 0) + 1

    ordered = sorted(by_repo.items(), key=lambda kv: (-kv[1], kv[0]))
    labels = [repo for repo, _ in ordered]
    counts = [n for _, n in ordered]
    return {
        "period": key,
        "labels": labels,
        "counts": counts,
        "total": sum(counts),
    }


def analytics_dashboard(conn: sqlite3.Connection, *, period: str = "all") -> dict[str, Any]:
    """Chart-ready aggregates for the analytics dashboard."""
    from datetime import timedelta

    key = (period or "all").strip().lower()
    if key not in _ANALYTICS_PERIOD_DAYS:
        key = "all"
    days = _ANALYTICS_PERIOD_DAYS[key]
    params: list[Any] = []
    where = ""
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(
            microsecond=0
        ).isoformat()
        where = "AND COALESCE(r.finished_at, r.created_at) >= ?"
        params = [cutoff]

    succeeded = rows_to_list(
        conn.execute(
            f"""
            SELECT
              r.id,
              r.agent,
              r.model,
              COALESCE(r.effort, '') AS effort,
              r.duration_ms,
              r.cost_usd,
              COALESCE(r.findings_sev1, 0) AS findings_sev1,
              COALESCE(r.findings_sev2, 0) AS findings_sev2,
              COALESCE(r.findings_sev3, 0) AS findings_sev3,
              COALESCE(r.total_tokens, 0) AS total_tokens,
              COALESCE(p.additions, 0) AS additions,
              COALESCE(p.deletions, 0) AS deletions,
              COALESCE(p.changed_files, 0) AS changed_files
            FROM review_runs r
            JOIN prs p ON p.id = r.pr_id
            WHERE r.status = 'succeeded'
              {where}
            ORDER BY r.id ASC
            """,
            params,
        ).fetchall()
    )

    def _cost_of(row: dict[str, Any]) -> float | None:
        raw = row.get("cost_usd")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    # Nested donut: outer = agent counts, inner = model (+effort) counts.
    agent_counts: dict[str, int] = {}
    model_counts: dict[str, dict[str, Any]] = {}
    agent_spend: dict[str, float] = {}
    model_spend: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    costed_runs = 0
    for row in succeeded:
        agent = row["agent"] or "unknown"
        agent_counts[agent] = agent_counts.get(agent, 0) + 1
        effort = row["effort"] or ""
        model = row["model"] or "unknown"
        label = f"{model}" + (f" · {effort}" if effort else "")
        key_m = f"{agent}::{label}"
        slot = model_counts.get(key_m)
        if slot is None:
            model_counts[key_m] = {
                "agent": agent,
                "label": label,
                "model": model,
                "effort": effort,
                "count": 1,
            }
        else:
            slot["count"] += 1

        cost = _cost_of(row)
        if cost is not None:
            total_cost += cost
            costed_runs += 1
            agent_spend[agent] = agent_spend.get(agent, 0.0) + cost
            spend_slot = model_spend.get(key_m)
            if spend_slot is None:
                model_spend[key_m] = {
                    "agent": agent,
                    "label": label,
                    "model": model,
                    "effort": effort,
                    "cost": cost,
                }
            else:
                spend_slot["cost"] += cost

    agents_sorted = sorted(agent_counts.items(), key=lambda x: (-x[1], x[0]))
    models_sorted = sorted(
        model_counts.values(), key=lambda x: (-int(x["count"]), x["agent"], x["label"])
    )
    spend_agents_sorted = sorted(agent_spend.items(), key=lambda x: (-x[1], x[0]))
    spend_models_sorted = sorted(
        model_spend.values(), key=lambda x: (-float(x["cost"]), x["agent"], x["label"])
    )

    # Combo rollups for bar charts (successful runs only).
    combo_map: dict[str, dict[str, Any]] = {}
    for row in succeeded:
        agent = row["agent"] or "unknown"
        model = row["model"] or "unknown"
        effort = row["effort"] or ""
        ck = f"{agent}|{model}|{effort}"
        slot = combo_map.get(ck)
        if slot is None:
            slot = {
                "agent": agent,
                "model": model,
                "effort": effort,
                "label": f"{agent} / {model}" + (f" / {effort}" if effort else ""),
                "runs": 0,
                "duration_sum": 0,
                "cost_sum": 0.0,
                "cost_n": 0,
                "sev1_sum": 0,
                "sev2_sum": 0,
                "sev3_sum": 0,
            }
            combo_map[ck] = slot
        slot["runs"] += 1
        slot["duration_sum"] += int(row["duration_ms"] or 0)
        slot["sev1_sum"] += int(row["findings_sev1"] or 0)
        slot["sev2_sum"] += int(row["findings_sev2"] or 0)
        slot["sev3_sum"] += int(row["findings_sev3"] or 0)
        cost = _cost_of(row)
        if cost is not None:
            slot["cost_sum"] += cost
            slot["cost_n"] += 1

    combos_ok = []
    for slot in combo_map.values():
        n = max(1, int(slot["runs"]))
        cost_n = int(slot["cost_n"])
        combos_ok.append(
            {
                "agent": slot["agent"],
                "model": slot["model"],
                "effort": slot["effort"],
                "label": slot["label"],
                "runs": slot["runs"],
                "avg_duration_ms": slot["duration_sum"] / n,
                "avg_cost_usd": (slot["cost_sum"] / cost_n) if cost_n else None,
                "total_cost_usd": slot["cost_sum"] if cost_n else None,
                "avg_sev1": slot["sev1_sum"] / n,
                "avg_sev2": slot["sev2_sum"] / n,
                "avg_sev3": slot["sev3_sum"] / n,
            }
        )
    combos_ok.sort(key=lambda x: (-x["runs"], x["label"]))

    # Bubble points: diff size × duration/cost × findings, colored by model.
    bubbles = []
    for row in succeeded:
        diff = int(row["additions"] or 0) + int(row["deletions"] or 0)
        findings = (
            int(row["findings_sev1"] or 0)
            + int(row["findings_sev2"] or 0)
            + int(row["findings_sev3"] or 0)
        )
        bubbles.append(
            {
                "id": row["id"],
                "agent": row["agent"],
                "model": row["model"],
                "effort": row["effort"],
                "diff_size": diff,
                "duration_ms": int(row["duration_ms"] or 0),
                "cost_usd": _cost_of(row),
                "findings": findings,
                "findings_sev1": int(row["findings_sev1"] or 0),
                "findings_sev2": int(row["findings_sev2"] or 0),
                "findings_sev3": int(row["findings_sev3"] or 0),
                "changed_files": int(row["changed_files"] or 0),
            }
        )

    failed_n = conn.execute(
        f"""
        SELECT COUNT(*) AS c FROM review_runs r
        WHERE r.status = 'failed' {where}
        """,
        params,
    ).fetchone()["c"]

    return {
        "period": key,
        "periods": [
            {"id": "week", "label": "Last week"},
            {"id": "month", "label": "Last month"},
            {"id": "3months", "label": "Last 3 months"},
            {"id": "6months", "label": "Last 6 months"},
            {"id": "all", "label": "All"},
        ],
        "summary": {
            "succeeded": len(succeeded),
            "failed": int(failed_n or 0),
            "combos": len(combos_ok),
            "total_cost_usd": total_cost if costed_runs else None,
            "costed_runs": costed_runs,
        },
        "usage": {
            "agents": [{"label": a, "count": c} for a, c in agents_sorted],
            "models": [
                {
                    "agent": m["agent"],
                    "label": m["label"],
                    "model": m["model"],
                    "effort": m["effort"],
                    "count": m["count"],
                }
                for m in models_sorted
            ],
        },
        "spend": {
            "agents": [{"label": a, "cost": c} for a, c in spend_agents_sorted],
            "models": [
                {
                    "agent": m["agent"],
                    "label": m["label"],
                    "model": m["model"],
                    "effort": m["effort"],
                    "cost": m["cost"],
                }
                for m in spend_models_sorted
            ],
        },
        "combos": combos_ok,
        "table": analytics_combos(conn, period=key),
        "bubbles": bubbles,
        "activity": analytics_activity(conn, period=key),
        "prs_by_repo": analytics_prs_by_repo(conn, period=key),
        "repo_activity": analytics_repo_activity(conn, period=key),
        "weekday_pattern": analytics_weekday_pattern(conn, period=key),
        "repo_pattern": analytics_repo_pattern(conn, period=key),
    }


def list_purgeable_runs(conn: sqlite3.Connection, cutoff_iso: str) -> list[dict[str, Any]]:
    return rows_to_list(
        conn.execute(
            """
            SELECT * FROM review_runs
            WHERE status IN ('succeeded', 'failed', 'cancelled')
              AND finished_at IS NOT NULL
              AND finished_at < ?
            """,
            (cutoff_iso,),
        ).fetchall()
    )


def delete_run(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute("DELETE FROM findings WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM review_runs WHERE id = ?", (run_id,))
    conn.commit()


# --- PR analyses -------------------------------------------------------------

def create_pr_analysis(
    conn: sqlite3.Connection,
    *,
    pr_id: int,
    head_sha: str,
    agent: str,
    model: str,
    effort: str | None,
    workdir_path: str = "",
) -> dict[str, Any]:
    """Insert or re-queue analysis for (pr_id, head_sha). Idempotent when already queued/running/succeeded."""
    existing = get_pr_analysis_for_head(conn, pr_id, head_sha)
    if existing and existing["status"] in ("queued", "running", "succeeded"):
        return existing
    if existing:
        conn.execute(
            """
            UPDATE pr_analyses
            SET status = 'queued',
                agent = ?,
                model = ?,
                effort = ?,
                workdir_path = COALESCE(NULLIF(?, ''), workdir_path),
                brief_markdown = NULL,
                brief_path = NULL,
                error_message = NULL,
                started_at = NULL,
                finished_at = NULL,
                duration_ms = NULL,
                cli_command = NULL
            WHERE id = ?
            """,
            (agent, model, effort, workdir_path, int(existing["id"])),
        )
        conn.commit()
        row = get_pr_analysis(conn, int(existing["id"]))
        assert row is not None
        return row
    cur = conn.execute(
        """
        INSERT INTO pr_analyses (pr_id, head_sha, status, agent, model, effort, workdir_path, created_at)
        VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
        """,
        (pr_id, head_sha, agent, model, effort, workdir_path, utcnow()),
    )
    conn.commit()
    row = get_pr_analysis(conn, int(cur.lastrowid))
    assert row is not None
    return row


def get_pr_analysis(conn: sqlite3.Connection, analysis_id: int) -> dict[str, Any] | None:
    return row_to_dict(
        conn.execute("SELECT * FROM pr_analyses WHERE id = ?", (analysis_id,)).fetchone()
    )


def get_pr_analysis_for_head(
    conn: sqlite3.Connection, pr_id: int, head_sha: str
) -> dict[str, Any] | None:
    return row_to_dict(
        conn.execute(
            "SELECT * FROM pr_analyses WHERE pr_id = ? AND head_sha = ?",
            (pr_id, head_sha),
        ).fetchone()
    )


def get_current_pr_analysis(conn: sqlite3.Connection, pr_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT head_sha FROM prs WHERE id = ?", (pr_id,)).fetchone()
    if row is None:
        return None
    head = (row["head_sha"] or "").strip()
    if not head:
        return None
    return get_pr_analysis_for_head(conn, pr_id, head)


def list_pr_analyses(conn: sqlite3.Connection, pr_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM pr_analyses WHERE pr_id = ? ORDER BY id DESC",
        (pr_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_pr_analysis(
    conn: sqlite3.Connection, analysis_id: int, **fields: Any
) -> dict[str, Any] | None:
    if not fields:
        return get_pr_analysis(conn, analysis_id)
    cols = []
    vals: list[Any] = []
    for key, value in fields.items():
        cols.append(f"{key} = ?")
        vals.append(value)
    vals.append(analysis_id)
    conn.execute(
        f"UPDATE pr_analyses SET {', '.join(cols)} WHERE id = ?",
        vals,
    )
    conn.commit()
    return get_pr_analysis(conn, analysis_id)


def cancel_pr_analyses_for_pr(conn: sqlite3.Connection, pr_id: int) -> list[int]:
    """Mark queued/running analyses for a PR as cancelled. Returns cancelled ids."""
    rows = conn.execute(
        """
        SELECT * FROM pr_analyses
        WHERE pr_id = ? AND status IN ('queued', 'running')
        """,
        (pr_id,),
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    if not ids:
        return []
    finished_at = utcnow()
    conn.execute(
        f"""
        UPDATE pr_analyses
        SET status = 'cancelled',
            finished_at = ?,
            error_message = 'Cancelled due to head change or user request'
        WHERE id IN ({','.join('?' * len(ids))})
        """,
        [finished_at, *ids],
    )
    for r in rows:
        record_analysis_finished_event(
            conn,
            {
                **dict(r),
                "status": "cancelled",
                "finished_at": finished_at,
            },
            commit=False,
        )
    conn.commit()
    return ids


def analysis_succeeded_for_current_head(conn: sqlite3.Connection, pr_id: int) -> bool:
    row = get_current_pr_analysis(conn, pr_id)
    return bool(row and row.get("status") == "succeeded")


# --- PR events (timeline) ----------------------------------------------------

def create_pr_event(
    conn: sqlite3.Connection,
    *,
    pr_id: int,
    kind: str,
    message: str = "",
    agent: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    analysis_id: int | None = None,
    meta: dict[str, Any] | None = None,
    created_at: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    meta_json = json.dumps(meta) if meta is not None else None
    when = created_at or utcnow()
    cur = conn.execute(
        """
        INSERT INTO pr_events (
          pr_id, kind, message, agent, model, effort, analysis_id, meta_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pr_id,
            kind,
            message or "",
            agent,
            model,
            effort,
            analysis_id,
            meta_json,
            when,
        ),
    )
    if commit:
        conn.commit()
    row = conn.execute(
        "SELECT * FROM pr_events WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    assert row is not None
    return _pr_event_dict(row)


def record_analysis_finished_event(
    conn: sqlite3.Connection,
    analysis: dict[str, Any],
    *,
    commit: bool = True,
) -> dict[str, Any] | None:
    """Record a timeline event when an analysis reaches a terminal status."""
    status = (analysis.get("status") or "").strip()
    if status not in ("succeeded", "failed", "cancelled"):
        return None
    messages = {
        "succeeded": "PR analysis finished",
        "failed": "PR analysis failed",
        "cancelled": "PR analysis cancelled",
    }
    return create_pr_event(
        conn,
        pr_id=int(analysis["pr_id"]),
        kind="analysis_finished",
        message=messages[status],
        agent=analysis.get("agent") or None,
        model=analysis.get("model") or None,
        effort=analysis.get("effort") or None,
        analysis_id=int(analysis["id"]),
        meta={"status": status},
        created_at=analysis.get("finished_at") or None,
        commit=commit,
    )


def list_pr_events(conn: sqlite3.Connection, pr_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM pr_events WHERE pr_id = ? ORDER BY created_at DESC, id DESC",
        (pr_id,),
    ).fetchall()
    return [_pr_event_dict(r) for r in rows]


def _pr_event_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    raw = d.pop("meta_json", None)
    meta: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                meta = parsed
        except (TypeError, json.JSONDecodeError):
            meta = {}
    d["meta"] = meta
    return d
