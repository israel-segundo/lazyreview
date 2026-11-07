"""Thread-local SQLite connections under concurrent API-style access."""

from __future__ import annotations

import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from lazyreview.db import get_run, upsert_pr
from lazyreview.db.schema import ThreadLocalConnection, connect, init_db


def _seed_run(db_path: Path, workdir: Path) -> int:
    bootstrap = connect(db_path)
    init_db(bootstrap)
    pr = upsert_pr(
        bootstrap,
        owner="o",
        repo="r",
        number=1,
        url="https://example.com/o/r/pull/1",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="abc",
    )
    cur = bootstrap.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path)
        VALUES (?, 'claude', 'opus', 'running', 'abc', ?)
        """,
        (pr["id"], str(workdir)),
    )
    bootstrap.commit()
    run_id = int(cur.lastrowid)
    bootstrap.close()
    return run_id


def test_thread_local_connection_survives_concurrent_reads(tmp_path: Path):
    db_path = tmp_path / "tl.db"
    run_id = _seed_run(db_path, tmp_path / "w")

    shared = ThreadLocalConnection(db_path)

    def _read(_i: int) -> str:
        row = get_run(shared, run_id)  # type: ignore[arg-type]
        assert row is not None
        return str(row["status"])

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(_read, i) for i in range(64)]
        results = [f.result() for f in as_completed(futures)]
    assert results == ["running"] * 64
    shared.close()


def test_thread_local_connection_releases_when_threads_die(tmp_path: Path):
    """Ephemeral threads must not leave SQLite FDs open after they exit."""
    db_path = tmp_path / "tl-leak.db"
    run_id = _seed_run(db_path, tmp_path / "w2")
    shared = ThreadLocalConnection(db_path)
    opened = threading.Barrier(9)
    finished = threading.Event()

    def _touch() -> None:
        row = get_run(shared, run_id)  # type: ignore[arg-type]
        assert row is not None
        opened.wait(timeout=5)
        finished.wait(timeout=5)

    threads = [
        threading.Thread(target=_touch, name=f"tl-leak-{i}", daemon=True)
        for i in range(8)
    ]
    for t in threads:
        t.start()
    opened.wait(timeout=5)
    assert shared.connection_count() == 8
    finished.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    # Thread-local finalizers run when the thread dict is cleared; nudge GC
    # in case a cycle delays __del__.
    deadline = time.monotonic() + 2.0
    while shared.connection_count() > 0 and time.monotonic() < deadline:
        gc.collect()
        time.sleep(0.05)
    assert shared.connection_count() == 0
    shared.close()
