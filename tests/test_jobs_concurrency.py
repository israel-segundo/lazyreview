"""JobRunner respects max_concurrent_jobs for parallel reviews."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from lazyreview.config import Settings
from lazyreview.db.schema import connect, init_db
from lazyreview.services.jobs import JobRunner


def _settings(tmp_path: Path, max_jobs: int) -> Settings:
    data = tmp_path / "data"
    data.mkdir()
    return Settings(
        host="127.0.0.1",
        port=3334,
        data_dir=data,
        runs_dir=data / "runs",
        db_path=data / "lazyreview.db",
        config_path=tmp_path / "config.toml",
        job_timeout_seconds=60,
        retention_sweep_seconds=3600,
        max_concurrent_jobs=max_jobs,
        service_name="lazyreview",
        unit_name="lazyreview.service",
    )


def test_parallel_jobs_when_max_concurrent_gt_one(tmp_path: Path):
    settings = _settings(tmp_path, max_jobs=2)
    conn = connect(settings.db_path)
    init_db(conn)

    runner = JobRunner(settings, conn)
    barrier = threading.Barrier(2, timeout=5)
    seen: list[int] = []
    lock = threading.Lock()

    def fake_execute(run_id: int) -> None:
        with lock:
            seen.append(run_id)
        barrier.wait()
        time.sleep(0.05)

    runner._execute = fake_execute  # type: ignore[method-assign]
    runner.start()
    try:
        runner.enqueue(101)
        runner.enqueue(102)
        # Both must enter fake_execute before either finishes (Barrier).
        deadline = time.time() + 5
        while time.time() < deadline:
            with lock:
                if len(seen) >= 2:
                    break
            time.sleep(0.02)
        assert sorted(seen) == [101, 102]
        # Wait for both to finish and clear inflight.
        deadline = time.time() + 5
        while time.time() < deadline:
            with runner._lock:
                if not runner._inflight:
                    break
            time.sleep(0.02)
        with runner._lock:
            assert runner._inflight == set()
    finally:
        runner.stop()
        conn.close()


def test_serial_when_max_concurrent_is_one(tmp_path: Path):
    settings = _settings(tmp_path, max_jobs=1)
    conn = connect(settings.db_path)
    init_db(conn)

    runner = JobRunner(settings, conn)
    overlapping = threading.Event()
    active = 0
    lock = threading.Lock()

    def fake_execute(run_id: int) -> None:
        nonlocal active
        with lock:
            active += 1
            if active > 1:
                overlapping.set()
        time.sleep(0.1)
        with lock:
            active -= 1

    runner._execute = fake_execute  # type: ignore[method-assign]
    runner.start()
    try:
        runner.enqueue(1)
        runner.enqueue(2)
        time.sleep(0.35)
        assert not overlapping.is_set()
    finally:
        runner.stop()
        conn.close()
