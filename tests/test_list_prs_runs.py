"""Homepage list_prs run_count and runs_stale (obsolete head) flags."""

from __future__ import annotations

from pathlib import Path

from lazyreview.db import create_run, list_prs, upsert_pr
from lazyreview.db.schema import connect, init_db


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "runs_list.db")
    init_db(conn)
    return conn


def _pr(conn, *, number: int = 1, head: str = "head1"):
    return upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=number,
        url=f"https://example.com/{number}",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha=head,
        head_updated_at="2026-09-01T00:00:00+00:00",
    )


def test_list_prs_run_count_and_not_stale_when_head_reviewed(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn, head="abc")
    create_run(
        conn,
        pr_id=pr["id"],
        agent="claude",
        model="haiku",
        effort="low",
        skill="",
        head_sha="abc",
        workdir_path="/tmp/r1",
    )
    create_run(
        conn,
        pr_id=pr["id"],
        agent="agy",
        model="flash",
        effort=None,
        skill="",
        head_sha="abc",
        workdir_path="/tmp/r2",
    )
    row = list_prs(conn)[0]
    assert row["run_count"] == 2
    assert row["runs_stale"] is False


def test_list_prs_runs_stale_when_head_moved_without_new_runs(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn, head="old")
    create_run(
        conn,
        pr_id=pr["id"],
        agent="claude",
        model="haiku",
        effort="low",
        skill="",
        head_sha="old",
        workdir_path="/tmp/r1",
    )
    upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=1,
        url="https://example.com/1",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="new",
        head_updated_at="2026-09-07T00:00:00+00:00",
    )
    row = list_prs(conn)[0]
    assert row["run_count"] == 1
    assert row["runs_stale"] is True
    assert row["head_sha"] == "new"


def test_list_prs_not_stale_with_zero_runs(tmp_path: Path):
    conn = _conn(tmp_path)
    _pr(conn, head="only")
    row = list_prs(conn)[0]
    assert row["run_count"] == 0
    assert row["runs_stale"] is False
