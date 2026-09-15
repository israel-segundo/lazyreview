"""Global /activity feed across analyses, runs, and consolidations."""

from __future__ import annotations

from pathlib import Path

from lazyreview.db import (
    count_global_activity_running,
    create_consolidation,
    create_pr_analysis,
    create_run,
    list_global_activity,
    update_pr_analysis,
    update_run,
    upsert_pr,
)
from lazyreview.db.schema import connect, init_db


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "activity_feed.db")
    init_db(conn)
    return conn


def _pr(conn, *, number: int = 1, head: str = "abc", repo: str = "r"):
    return upsert_pr(
        conn,
        owner="o",
        repo=repo,
        number=number,
        url=f"https://example.com/{number}",
        title=f"PR {number}",
        base_ref="main",
        head_ref="feat",
        head_sha=head,
        head_updated_at="2026-09-01T00:00:00+00:00",
    )


def test_list_global_activity_merges_kinds(tmp_path: Path):
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

    result = list_global_activity(conn, limit=50)
    items = result["items"]
    assert result["total"] == 3
    assert result["limit"] == 50
    assert result["offset"] == 0
    kinds = {i["kind"] for i in items}
    assert kinds == {"analysis", "run", "consolidation"}
    assert all(i["pr_label"] == "r#1" for i in items)
    assert all("when_at" in i for i in items)

    running = list_global_activity(conn, running_only=True)
    assert running["total"] == 2
    assert {i["kind"] for i in running["items"]} == {"run", "consolidation"}
    assert all(i["status"] in ("queued", "running") for i in running["items"])

    assert count_global_activity_running(conn) == 2


def test_list_global_activity_pagination(tmp_path: Path):
    conn = _conn(tmp_path)
    for n in range(1, 6):
        pr = _pr(conn, number=n, head=f"sha{n}")
        create_pr_analysis(
            conn,
            pr_id=pr["id"],
            head_sha=f"sha{n}",
            agent="claude",
            model="haiku",
            effort="low",
        )

    page1 = list_global_activity(conn, limit=2, offset=0)
    assert page1["total"] == 5
    assert page1["limit"] == 2
    assert page1["offset"] == 0
    assert len(page1["items"]) == 2

    page2 = list_global_activity(conn, limit=2, offset=2)
    assert len(page2["items"]) == 2
    assert page1["items"][0]["id"] != page2["items"][0]["id"]

    page3 = list_global_activity(conn, limit=2, offset=4)
    assert len(page3["items"]) == 1

    empty = list_global_activity(conn, limit=2, offset=50)
    assert empty["items"] == []
    assert empty["total"] == 5


def test_list_global_activity_empty(tmp_path: Path):
    conn = _conn(tmp_path)
    result = list_global_activity(conn)
    assert result["items"] == []
    assert result["total"] == 0
    assert result["limit"] == 25
    assert result["offset"] == 0
    assert count_global_activity_running(conn) == 0
