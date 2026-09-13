"""Batch review run creation and dormant auto-consolidation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from lazyreview.config import Settings
from lazyreview.db import (
    active_consolidation_run_ids,
    create_consolidation,
    get_consolidation,
    update_run,
    upsert_pr,
)
from lazyreview.db.schema import connect, init_db
from lazyreview.services.consolidation_jobs import (
    try_activate_dormant_consolidation,
)



def _seed_succeeded_analysis(conn, pr):
    """Satisfy the PR-analysis gate for API tests."""
    from lazyreview.db import create_pr_analysis, update_pr_analysis

    row = create_pr_analysis(
        conn,
        pr_id=int(pr["id"]),
        head_sha=pr["head_sha"],
        agent="claude",
        model="haiku",
        effort="low",
    )
    update_pr_analysis(
        conn,
        int(row["id"]),
        status="succeeded",
        brief_markdown="# ok",
        finished_at="2026-01-01T00:00:00+00:00",
    )
    return row

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
        max_concurrent_jobs=2,
        service_name="lazyreview",
        unit_name="lazyreview.service",
    )


def test_batch_creates_multiple_runs(tmp_path: Path):
    from lazyreview.main import create_app

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=7,
        url="https://example.com/o/r/pull/7",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="bbb",
    )
    _seed_succeeded_analysis(conn, pr)
    conn.close()

    fake = MagicMock()
    fake.available.return_value = True

    with patch("lazyreview.main.get_settings", return_value=settings):
        app = create_app()
        with TestClient(app) as client:
            with (
                patch("lazyreview.api.runs.get_adapter", return_value=fake),
                patch.object(app.state.jobs, "enqueue"),
            ):
                res = client.post(
                    "/api/runs/batch",
                    json={
                        "pr_id": pr["id"],
                        "runs": [
                            {"agent": "claude", "model": "opus", "effort": "high"},
                            {"agent": "claude", "model": "haiku", "effort": "low"},
                        ],
                    },
                )
            assert res.status_code == 200, res.text
            data = res.json()
            assert data["count"] == 2
            assert len(data["runs"]) == 2
            assert data["consolidation"] is not None
            assert data["consolidation"]["status"] == "dormant"
            assert {r["model"] for r in data["runs"]} == {"opus", "haiku"}
            assert all(r["status"] == "queued" for r in data["runs"])


def test_batch_creates_dormant_consolidation_by_default(tmp_path: Path):
    from lazyreview.main import create_app

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=8,
        url="https://example.com/o/r/pull/8",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="ccc",
    )
    _seed_succeeded_analysis(conn, pr)
    conn.close()

    fake = MagicMock()
    fake.available.return_value = True

    with patch("lazyreview.main.get_settings", return_value=settings):
        app = create_app()
        with TestClient(app) as client:
            with (
                patch("lazyreview.api.runs.get_adapter", return_value=fake),
                patch.object(app.state.jobs, "enqueue"),
                patch.object(app.state.consolidations, "enqueue") as consol_enqueue,
            ):
                res = client.post(
                    "/api/runs/batch",
                    json={
                        "pr_id": pr["id"],
                        "runs": [
                            {"agent": "claude", "model": "opus", "effort": "high"},
                            {"agent": "claude", "model": "haiku", "effort": "low"},
                        ],
                    },
                )
            assert res.status_code == 200, res.text
            data = res.json()
            assert data["count"] == 2
            consol = data["consolidation"]
            assert consol is not None
            assert consol["status"] == "dormant"
            assert set(consol["source_run_ids"]) == {r["id"] for r in data["runs"]}
            consol_enqueue.assert_not_called()

            active = active_consolidation_run_ids(app.state.db, pr["id"])
            assert active == set(consol["source_run_ids"])
            from lazyreview.db import in_flight_consolidation_run_ids

            assert in_flight_consolidation_run_ids(app.state.db, pr["id"]) == set()


def test_dormant_consolidation_queues_when_all_runs_succeed(tmp_path: Path):
    conn = connect(tmp_path / "wake.db")
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=9,
        url="https://example.com/o/r/pull/9",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="ddd",
    )
    _seed_succeeded_analysis(conn, pr)
    conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path)
        VALUES (?, 'claude', 'opus', 'running', 'ddd', ?),
               (?, 'claude', 'haiku', 'running', 'ddd', ?)
        """,
        (pr["id"], str(tmp_path / "a"), pr["id"], str(tmp_path / "b")),
    )
    # Fetch both ids.
    rows = conn.execute(
        "SELECT id FROM review_runs WHERE pr_id = ? ORDER BY id", (pr["id"],)
    ).fetchall()
    run_ids = [int(r["id"]) for r in rows]
    assert len(run_ids) == 2

    consol = create_consolidation(
        conn,
        pr_id=int(pr["id"]),
        head_sha="ddd",
        source_run_ids=run_ids,
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c"),
        status="dormant",
    )

    assert try_activate_dormant_consolidation(conn, int(consol["id"])) is None
    assert get_consolidation(conn, int(consol["id"]))["status"] == "dormant"

    update_run(conn, run_ids[0], status="succeeded")
    assert try_activate_dormant_consolidation(conn, int(consol["id"])) is None

    update_run(conn, run_ids[1], status="succeeded")
    updated = try_activate_dormant_consolidation(conn, int(consol["id"]))
    assert updated is not None
    assert updated["status"] == "queued"
    assert updated["source_run_ids"] == run_ids

    # Second observer loses the race.
    assert try_activate_dormant_consolidation(conn, int(consol["id"])) is None
    conn.close()


def test_dormant_consolidation_proceeds_when_some_fail_but_one_succeeds(tmp_path: Path):
    conn = connect(tmp_path / "mixed.db")
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=11,
        url="https://example.com/o/r/pull/11",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="fff",
    )
    _seed_succeeded_analysis(conn, pr)
    conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path)
        VALUES (?, 'claude', 'opus', 'succeeded', 'fff', ?),
               (?, 'claude', 'haiku', 'failed', 'fff', ?),
               (?, 'claude', 'sonnet', 'cancelled', 'fff', ?)
        """,
        (
            pr["id"],
            str(tmp_path / "a"),
            pr["id"],
            str(tmp_path / "b"),
            pr["id"],
            str(tmp_path / "c"),
        ),
    )
    run_ids = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM review_runs WHERE pr_id = ? ORDER BY id", (pr["id"],)
        ).fetchall()
    ]
    consol = create_consolidation(
        conn,
        pr_id=int(pr["id"]),
        head_sha="fff",
        source_run_ids=run_ids,
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c"),
        status="dormant",
    )
    updated = try_activate_dormant_consolidation(conn, int(consol["id"]))
    assert updated is not None
    assert updated["status"] == "queued"
    assert updated["source_run_ids"] == [run_ids[0]]
    conn.close()


def test_dormant_consolidation_cancels_when_no_successes(tmp_path: Path):
    conn = connect(tmp_path / "fail.db")
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=10,
        url="https://example.com/o/r/pull/10",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="eee",
    )
    _seed_succeeded_analysis(conn, pr)
    conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path)
        VALUES (?, 'claude', 'opus', 'failed', 'eee', ?),
               (?, 'claude', 'haiku', 'cancelled', 'eee', ?)
        """,
        (pr["id"], str(tmp_path / "a"), pr["id"], str(tmp_path / "b")),
    )
    run_ids = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM review_runs WHERE pr_id = ? ORDER BY id", (pr["id"],)
        ).fetchall()
    ]
    consol = create_consolidation(
        conn,
        pr_id=int(pr["id"]),
        head_sha="eee",
        source_run_ids=run_ids,
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c"),
        status="dormant",
    )
    updated = try_activate_dormant_consolidation(conn, int(consol["id"]))
    assert updated is not None
    assert updated["status"] == "cancelled"
    assert "No successful runs" in (updated.get("error_message") or "")
    conn.close()
