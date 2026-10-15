"""In-app notifications: create on job finish, read/unread, API."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from lazyreview.config import Settings
from lazyreview.db import (
    NOTIFICATION_KIND_ANALYSIS,
    NOTIFICATION_KIND_CONSOLIDATION,
    NOTIFICATION_KIND_RUN,
    count_unread_notifications,
    create_consolidation,
    create_notification_for_analysis,
    create_pr_analysis,
    get_notification,
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    mark_notifications_read_for_pr,
    record_analysis_finished_event,
    update_consolidation,
    update_pr_analysis,
    update_run,
    upsert_pr,
    utcnow,
)
from lazyreview.db.schema import SCHEMA_VERSION, connect, init_db


def _settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Settings(
        host="127.0.0.1",
        port=3334,
        data_dir=data,
        runs_dir=data / "runs",
        db_path=data / "lazyreview.db",
        config_path=tmp_path / "config.toml",
        job_timeout_seconds=60,
        retention_sweep_seconds=3600,
        max_concurrent_jobs=1,
    )


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "notif.db")
    init_db(conn)
    return conn


def _pr(conn):
    return upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=42,
        url="https://example.com/o/r/pull/42",
        title="Ship notifications",
        base_ref="main",
        head_ref="feat",
        head_sha="abc123",
    )


def test_schema_has_notifications(tmp_path: Path):
    conn = _conn(tmp_path)
    ver = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert ver == SCHEMA_VERSION == 23
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(notifications)").fetchall()
    }
    assert "kind" in cols
    assert "read_at" in cols
    assert "run_id" in cols
    conn.close()


def test_run_finish_creates_unread_notification(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn)
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, status, agent, model, head_sha, workdir_path)
        VALUES (?, 'running', 'claude', 'opus', 'abc123', ?)
        """,
        (pr["id"], str(tmp_path / "w")),
    )
    conn.commit()
    run_id = int(cur.lastrowid)

    updated = update_run(conn, run_id, status="succeeded", finished_at=utcnow())
    assert updated is not None

    listed = list_notifications(conn, limit=10)
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["kind"] == NOTIFICATION_KIND_RUN
    assert item["unread"] is True
    assert item["status"] == "succeeded"
    assert "claude" in item["title"]
    assert "r#42" in item["body"]
    assert item["href"] == f"/prs/{pr['id']}?tab=activity"
    assert count_unread_notifications(conn) == 1

    # Second terminal update must not duplicate.
    update_run(conn, run_id, error_message="noop")
    assert list_notifications(conn)["total"] == 1
    conn.close()


def test_analysis_and_consolidation_notifications(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn)
    analysis = create_pr_analysis(
        conn,
        pr_id=pr["id"],
        head_sha="abc123",
        agent="claude",
        model="opus",
        effort="high",
        workdir_path=str(tmp_path / "a"),
    )
    analysis = update_pr_analysis(
        conn, int(analysis["id"]), status="succeeded", finished_at=utcnow()
    )
    assert analysis is not None
    record_analysis_finished_event(conn, analysis)

    assert count_unread_notifications(conn) == 1
    a_item = list_notifications(conn)["items"][0]
    assert a_item["kind"] == NOTIFICATION_KIND_ANALYSIS
    assert a_item["href"].endswith("?tab=overview")

    # Deduped if recorded again.
    record_analysis_finished_event(conn, analysis)
    assert count_unread_notifications(conn) == 1

    run_cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, status, agent, model, head_sha, workdir_path)
        VALUES (?, 'succeeded', 'claude', 'opus', 'abc123', ?)
        """,
        (pr["id"], str(tmp_path / "rw")),
    )
    conn.commit()
    run_id = int(run_cur.lastrowid)
    consol = create_consolidation(
        conn,
        pr_id=pr["id"],
        head_sha="abc123",
        source_run_ids=[run_id],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c"),
    )
    update_consolidation(
        conn, int(consol["id"]), status="succeeded", finished_at=utcnow()
    )
    assert count_unread_notifications(conn) == 2
    kinds = {n["kind"] for n in list_notifications(conn)["items"]}
    assert NOTIFICATION_KIND_CONSOLIDATION in kinds
    conn.close()


def test_mark_read_by_id_pr_and_all(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn)
    analysis = create_pr_analysis(
        conn,
        pr_id=pr["id"],
        head_sha="abc123",
        agent="claude",
        model="opus",
        effort="high",
        workdir_path=str(tmp_path / "a"),
    )
    create_notification_for_analysis(
        conn,
        {
            **analysis,
            "status": "succeeded",
            "finished_at": utcnow(),
        },
    )
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, status, agent, model, head_sha, workdir_path)
        VALUES (?, 'running', 'agy', 'flash', 'abc123', ?)
        """,
        (pr["id"], str(tmp_path / "w")),
    )
    conn.commit()
    update_run(conn, int(cur.lastrowid), status="failed", finished_at=utcnow())

    assert count_unread_notifications(conn) == 2
    items = list_notifications(conn)["items"]
    run_n = next(n for n in items if n["kind"] == NOTIFICATION_KIND_RUN)
    mark_notification_read(conn, int(run_n["id"]))
    assert count_unread_notifications(conn) == 1
    assert get_notification(conn, int(run_n["id"]))["unread"] is False

    mark_notifications_read_for_pr(
        conn, int(pr["id"]), kind=NOTIFICATION_KIND_ANALYSIS
    )
    assert count_unread_notifications(conn) == 0

    # Create another and mark all.
    cur2 = conn.execute(
        """
        INSERT INTO review_runs (pr_id, status, agent, model, head_sha, workdir_path)
        VALUES (?, 'running', 'cursor', 'composer', 'abc123', ?)
        """,
        (pr["id"], str(tmp_path / "w2")),
    )
    conn.commit()
    update_run(conn, int(cur2.lastrowid), status="cancelled", finished_at=utcnow())
    assert count_unread_notifications(conn) == 1
    assert mark_all_notifications_read(conn) == 1
    assert count_unread_notifications(conn) == 0
    conn.close()


def test_notifications_api(tmp_path: Path):
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=7,
        url="https://example.com/o/r/pull/7",
        title="api",
        base_ref="main",
        head_ref="feat",
        head_sha="dead",
    )
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, status, agent, model, head_sha, workdir_path)
        VALUES (?, 'running', 'claude', 'opus', 'dead', ?)
        """,
        (pr["id"], str(tmp_path / "w")),
    )
    conn.commit()
    update_run(conn, int(cur.lastrowid), status="succeeded", finished_at=utcnow())
    conn.close()

    with patch("lazyreview.main.get_settings", return_value=settings):
        from lazyreview.main import create_app

        app = create_app()
        with TestClient(app) as client:
            res = client.get("/api/notifications/unread-count")
            assert res.status_code == 200
            assert res.json()["unread_count"] == 1

            res = client.get("/api/notifications?limit=15")
            assert res.status_code == 200
            data = res.json()
            assert data["total"] == 1
            assert data["items"][0]["unread"] is True
            nid = data["items"][0]["id"]

            res = client.post(f"/api/notifications/{nid}/read")
            assert res.status_code == 200
            assert res.json()["unread_count"] == 0

            # Re-create unread via another run, then mark by PR tab kind.
            conn2 = connect(settings.db_path)
            cur2 = conn2.execute(
                """
                INSERT INTO review_runs
                  (pr_id, status, agent, model, head_sha, workdir_path)
                VALUES (?, 'running', 'agy', 'flash', 'dead', ?)
                """,
                (pr["id"], str(tmp_path / "w2")),
            )
            conn2.commit()
            update_run(
                conn2, int(cur2.lastrowid), status="succeeded", finished_at=utcnow()
            )
            conn2.close()

            res = client.post(
                f"/api/notifications/read-pr/{pr['id']}",
                json={"kind": "runs"},
            )
            assert res.status_code == 200
            assert res.json()["unread_count"] == 0

            page = client.get("/notifications")
            assert page.status_code == 200
            assert b"Notifications" in page.content
