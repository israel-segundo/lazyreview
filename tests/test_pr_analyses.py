"""PR analysis queue, concurrency cap, persistence, and review gate."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from lazyreview.config import Settings
from lazyreview.db import (
    create_pr_analysis,
    create_run,
    get_pr_analysis,
    update_pr_analysis,
    upsert_pr,
)
from lazyreview.db.schema import connect, init_db
from lazyreview.services.analysis_gate import require_succeeded_analysis
from lazyreview.services.analysis_jobs import AnalysisRunner


def _settings(tmp_path: Path, max_analyses: int = 2) -> Settings:
    data = tmp_path / "data"
    data.mkdir()
    return Settings(
        host="127.0.0.1",
        port=3334,
        data_dir=data,
        db_path=data / "lazyreview.db",
        runs_dir=data / "runs",
        config_path=tmp_path / "config.toml",
        job_timeout_seconds=60,
        retention_sweep_seconds=3600,
        max_concurrent_jobs=2,
        service_name="lazyreview",
        unit_name="lazyreview.service",
        max_concurrent_analyses=max_analyses,
    )


def _pr(conn, n: int = 1, head: str = "abc"):
    return upsert_pr(
        conn,
        owner="acme",
        repo="demo",
        number=n,
        url=f"https://github.com/acme/demo/pull/{n}",
        title=f"PR {n}",
        base_ref="main",
        head_ref="feat",
        head_sha=head,
        state="open",
        review_requested=True,
    )


def test_analysis_persisted_and_gate(tmp_path: Path):
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    init_db(conn)
    pr = _pr(conn)

    with pytest.raises(HTTPException) as ei:
        require_succeeded_analysis(conn, int(pr["id"]))
    assert ei.value.status_code == 409

    row = create_pr_analysis(
        conn,
        pr_id=int(pr["id"]),
        head_sha=pr["head_sha"],
        agent="claude",
        model="haiku",
        effort="low",
    )
    assert row["status"] == "queued"
    update_pr_analysis(
        conn,
        int(row["id"]),
        status="succeeded",
        brief_markdown="# Brief\n\nHello",
        finished_at="2026-01-01T00:00:00+00:00",
    )
    got = require_succeeded_analysis(conn, int(pr["id"]))
    assert got["brief_markdown"].startswith("# Brief")

    # Gate still fails for a different head.
    upsert_pr(
        conn,
        owner="acme",
        repo="demo",
        number=1,
        url=pr["url"],
        title=pr["title"],
        base_ref="main",
        head_ref="feat",
        head_sha="def456",
        state="open",
        review_requested=True,
    )
    with pytest.raises(HTTPException) as ei2:
        require_succeeded_analysis(conn, int(pr["id"]))
    assert ei2.value.status_code == 409

    # Overview still surfaces the previous brief as stale for the new head.
    from lazyreview.db import get_pr

    shown = get_pr(conn, int(pr["id"]))
    assert shown is not None
    assert shown["analysis_status"] is None
    assert shown["analysis_stale"] is True
    assert shown["analysis_brief_head_sha"] == "abc"
    assert (shown["analysis_brief"] or "").startswith("# Brief")
    conn.close()


def test_analysis_runner_caps_at_two(tmp_path: Path):
    settings = _settings(tmp_path, max_analyses=2)
    conn = connect(settings.db_path)
    init_db(conn)

    runner = AnalysisRunner(settings, conn)
    barrier = threading.Barrier(2, timeout=5)
    seen: list[int] = []
    lock = threading.Lock()
    max_active = 0
    active = 0

    def fake_execute(analysis_id: int) -> None:
        nonlocal active, max_active
        with lock:
            seen.append(analysis_id)
            active += 1
            max_active = max(max_active, active)
        barrier.wait()
        time.sleep(0.05)
        with lock:
            active -= 1

    runner._execute_with_conn = lambda analysis_id, conn: fake_execute(analysis_id)  # type: ignore[method-assign]
    runner.start()
    try:
        ids = []
        for i in range(5):
            pr = _pr(conn, n=i + 1, head=f"sha{i}")
            row = create_pr_analysis(
                conn,
                pr_id=int(pr["id"]),
                head_sha=pr["head_sha"],
                agent="claude",
                model="haiku",
                effort="low",
            )
            ids.append(int(row["id"]))
            runner.enqueue(int(row["id"]))
        deadline = time.time() + 5
        while time.time() < deadline:
            with lock:
                if len(seen) >= 2:
                    break
            time.sleep(0.02)
        assert len(seen) >= 2
        # Wait for all to drain
        deadline = time.time() + 8
        while time.time() < deadline:
            with runner._lock:
                if not runner._inflight and len(seen) >= 5:
                    break
            time.sleep(0.05)
        assert sorted(seen) == sorted(ids)
        assert max_active <= 2
    finally:
        runner.stop()
        conn.close()
