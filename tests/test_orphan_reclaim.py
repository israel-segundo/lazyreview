"""Orphaned running jobs are reclaimed on runner start."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from lazyreview.config import Settings
from lazyreview.db import create_consolidation, get_consolidation, get_run, upsert_pr
from lazyreview.db.schema import connect, init_db
from lazyreview.services.consolidation_jobs import ConsolidationRunner
from lazyreview.services.jobs import JobRunner


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


def test_job_runner_reclaims_orphaned_running_and_wakes_dormant(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=1,
        url="https://example.com/o/r/pull/1",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="abc",
    )
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path)
        VALUES (?, 'claude', 'opus', 'succeeded', 'abc', ?),
               (?, 'claude', 'haiku', 'running', 'abc', ?)
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
        head_sha="abc",
        source_run_ids=run_ids,
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c"),
        status="dormant",
    )

    consol_runner = ConsolidationRunner(settings, conn)
    consol_runner.enqueue = MagicMock()  # type: ignore[method-assign]
    jobs = JobRunner(settings, conn)
    jobs.on_run_finished = consol_runner.notify_run_finished
    jobs.start()
    try:
        orphan = get_run(conn, run_ids[1])
        assert orphan is not None
        assert orphan["status"] == "failed"
        assert "daemon restart" in (orphan.get("error_message") or "")
        updated = get_consolidation(conn, int(consol["id"]))
        assert updated is not None
        assert updated["status"] == "queued"
        assert updated["source_run_ids"] == [run_ids[0]]
        consol_runner.enqueue.assert_called_with(int(consol["id"]))
    finally:
        jobs.stop()
        conn.close()
