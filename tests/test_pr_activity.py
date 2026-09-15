"""Homepage activity ball aggregates analysis, runs, and consolidations."""

from __future__ import annotations

from pathlib import Path

from lazyreview.db import (
    create_consolidation,
    create_pr_analysis,
    create_run,
    list_prs,
    pr_activity_summary,
    update_pr_analysis,
    update_run,
    upsert_pr,
)
from lazyreview.db.schema import connect, init_db


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "activity.db")
    init_db(conn)
    return conn


def _pr(conn, *, number: int = 1, head: str = "abc"):
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


def test_pr_activity_summary_priority():
    idle = pr_activity_summary(analysis_status=None)
    assert idle["activity_status"] == "idle"

    running = pr_activity_summary(analysis_status="queued", finished_runs=3)
    assert running["activity_status"] == "running"

    done = pr_activity_summary(analysis_status="succeeded", finished_runs=2)
    assert done["activity_status"] == "done"


def test_list_prs_activity_idle(tmp_path: Path):
    conn = _conn(tmp_path)
    _pr(conn)
    row = list_prs(conn)[0]
    assert row["activity_status"] == "idle"
    assert "No activity" in row["activity_label"]


def test_list_prs_activity_running_from_analysis(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn)
    create_pr_analysis(
        conn,
        pr_id=pr["id"],
        head_sha="abc",
        agent="claude",
        model="haiku",
        effort="low",
    )
    row = list_prs(conn)[0]
    assert row["activity_status"] == "running"
    assert "analysis" in row["activity_label"]


def test_list_prs_activity_running_from_run(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn)
    run = create_run(
        conn,
        pr_id=pr["id"],
        agent="claude",
        model="haiku",
        effort="low",
        skill="",
        head_sha="abc",
        workdir_path="/tmp/r1",
    )
    update_run(conn, int(run["id"]), status="running")
    row = list_prs(conn)[0]
    assert row["activity_status"] == "running"
    assert "review run" in row["activity_label"]


def test_list_prs_activity_done_after_analysis(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn)
    a = create_pr_analysis(
        conn,
        pr_id=pr["id"],
        head_sha="abc",
        agent="claude",
        model="haiku",
        effort="low",
    )
    update_pr_analysis(conn, int(a["id"]), status="succeeded", brief_markdown="# ok")
    row = list_prs(conn)[0]
    assert row["activity_status"] == "done"
    assert "finished" in row["activity_label"].lower()


def test_list_prs_activity_running_from_consolidation(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn)
    run = create_run(
        conn,
        pr_id=pr["id"],
        agent="claude",
        model="haiku",
        effort="low",
        skill="",
        head_sha="abc",
        workdir_path="/tmp/r1",
    )
    update_run(conn, int(run["id"]), status="succeeded")
    create_consolidation(
        conn,
        pr_id=pr["id"],
        head_sha="abc",
        source_run_ids=[int(run["id"])],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path="/tmp/c1",
        status="queued",
    )
    row = list_prs(conn)[0]
    assert row["activity_status"] == "running"
    assert "consolidation" in row["activity_label"]
