"""Global /activity feed across analyses, runs, and consolidations."""

from __future__ import annotations

from pathlib import Path

from lazyreview.db import (
    count_global_activity_running,
    create_consolidation,
    create_pr_analysis,
    create_run,
    list_global_activity,
    update_consolidation,
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


def test_list_global_activity_includes_publish_regen(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn)
    c = create_consolidation(
        conn,
        pr_id=pr["id"],
        head_sha="abc",
        source_run_ids=[],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path="/tmp/c1",
        status="queued",
    )
    update_consolidation(
        conn,
        int(c["id"]),
        status="succeeded",
        publish_status="queued",
        publish_started_at="2026-11-03T15:00:00+00:00",
    )

    running = list_global_activity(conn, running_only=True)
    assert running["total"] == 1
    item = running["items"][0]
    assert item["kind"] == "publish"
    assert item["id"] == int(c["id"])
    assert item["status"] == "queued"
    assert count_global_activity_running(conn) == 1

    # Completed publish regenerations leave the in-flight feed but stay in all/completed.
    update_consolidation(
        conn,
        int(c["id"]),
        publish_status="succeeded",
        publish_finished_at="2026-11-03T15:01:00+00:00",
    )
    assert list_global_activity(conn, running_only=True)["total"] == 0
    assert count_global_activity_running(conn) == 0
    completed = list_global_activity(conn, status_filter="completed")
    assert any(i["kind"] == "publish" and i["status"] == "succeeded" for i in completed["items"])
    assert any(i["kind"] == "consolidation" and i["status"] == "succeeded" for i in completed["items"])


def test_list_global_activity_status_filters(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn)
    a_ok = create_pr_analysis(
        conn, pr_id=pr["id"], head_sha="abc", agent="claude", model="haiku", effort="low"
    )
    update_pr_analysis(conn, int(a_ok["id"]), status="succeeded", brief_markdown="# ok")
    # Separate head so create_pr_analysis inserts a new row (one analysis per head).
    a_cancel = create_pr_analysis(
        conn, pr_id=pr["id"], head_sha="def", agent="claude", model="haiku", effort="low"
    )
    update_pr_analysis(conn, int(a_cancel["id"]), status="cancelled")
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

    all_items = list_global_activity(conn, status_filter="all")
    assert all_items["total"] == 4
    assert any(i["status"] == "cancelled" for i in all_items["items"])

    running = list_global_activity(conn, status_filter="running")
    assert running["total"] == 1
    assert running["items"][0]["kind"] == "run"
    assert running["items"][0]["status"] == "running"

    queued = list_global_activity(conn, status_filter="queued")
    assert queued["total"] == 1
    assert queued["items"][0]["kind"] == "consolidation"
    assert queued["items"][0]["status"] == "queued"

    completed = list_global_activity(conn, status_filter="completed")
    assert completed["total"] == 2
    assert {i["status"] for i in completed["items"]} == {"succeeded", "cancelled"}
