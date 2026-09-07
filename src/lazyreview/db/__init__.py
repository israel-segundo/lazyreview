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

    conn.execute(
        """
        INSERT INTO prs (
          owner, repo, number, url, title, author, base_ref, head_ref, head_sha,
          state, is_draft, additions, deletions, changed_files,
          review_requested, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM prs WHERE owner = ? AND repo = ? AND number = ?",
        (owner, repo, number),
    ).fetchone()
    assert row is not None
    # New head → prior review runs are stale for consolidation.
    if head_sha and old_head and head_sha != old_head:
        deprecate_runs_for_pr(conn, int(row["id"]))
    conn.commit()
    return dict(row)


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


def list_prs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.*,
          (SELECT COUNT(*) FROM review_runs r WHERE r.pr_id = p.id) AS run_count,
          (SELECT r.status FROM review_runs r WHERE r.pr_id = p.id ORDER BY r.id DESC LIMIT 1) AS latest_status
        FROM prs p
        ORDER BY
          p.review_requested DESC,
          CASE WHEN p.state = 'open' THEN 0 ELSE 1 END,
          p.updated_at DESC
        """
    ).fetchall()
    return rows_to_list(rows)


def get_pr(conn: sqlite3.Connection, pr_id: int) -> dict[str, Any] | None:
    return row_to_dict(conn.execute("SELECT * FROM prs WHERE id = ?", (pr_id,)).fetchone())


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
        INSERT INTO review_runs (pr_id, agent, model, effort, skill, head_sha, status, workdir_path)
        VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
        """,
        (pr_id, agent, model, effort, skill, head_sha, workdir_path),
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
          pr_id, head_sha, source_run_ids, agent, model, effort, status, workdir_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
    """Source run ids in a queued/running consolidation (not dormant)."""
    return active_consolidation_run_ids(conn, pr_id, statuses=("queued", "running"))


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


def analytics_combos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          agent,
          model,
          COALESCE(effort, '') AS effort,
          COUNT(*) AS runs,
          SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
          SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
          AVG(duration_ms) AS avg_duration_ms,
          AVG(total_tokens) AS avg_total_tokens,
          AVG(findings_sev1) AS avg_sev1,
          AVG(findings_sev2) AS avg_sev2,
          AVG(findings_sev3) AS avg_sev3
        FROM review_runs
        WHERE status IN ('succeeded', 'failed')
        GROUP BY agent, model, COALESCE(effort, '')
        ORDER BY runs DESC, agent, model
        """
    ).fetchall()
    return rows_to_list(rows)


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
