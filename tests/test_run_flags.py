"""Run consolidated / deprecated flags."""

from __future__ import annotations

from pathlib import Path

from lazyreview.db import (
    deprecate_runs_for_pr,
    mark_runs_consolidated,
    run_is_selectable,
    upsert_pr,
)
from lazyreview.db.schema import connect, init_db


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_upsert_new_head_deprecates_runs(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=1,
        url="https://example.com",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="aaa",
    )
    conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path)
        VALUES (?, 'claude', 'opus', 'succeeded', 'aaa', ?)
        """,
        (pr["id"], str(tmp_path / "w")),
    )
    conn.commit()
    upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=1,
        url="https://example.com",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="bbb",
    )
    row = conn.execute("SELECT deprecated FROM review_runs").fetchone()
    assert int(row["deprecated"]) == 1


def test_mark_consolidated_and_selectable(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=2,
        url="https://example.com",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="ccc",
    )
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path)
        VALUES (?, 'claude', 'opus', 'succeeded', 'ccc', ?)
        """,
        (pr["id"], str(tmp_path / "w2")),
    )
    conn.commit()
    run_id = int(cur.lastrowid)
    run = dict(conn.execute("SELECT * FROM review_runs WHERE id = ?", (run_id,)).fetchone())
    assert run_is_selectable(run)
    mark_runs_consolidated(conn, [run_id])
    run = dict(conn.execute("SELECT * FROM review_runs WHERE id = ?", (run_id,)).fetchone())
    assert int(run["consolidated"]) == 1
    assert not run_is_selectable(run)
    deprecate_runs_for_pr(conn, pr["id"])
    run = dict(conn.execute("SELECT * FROM review_runs WHERE id = ?", (run_id,)).fetchone())
    assert int(run["deprecated"]) == 1
    assert not run_is_selectable(run)
