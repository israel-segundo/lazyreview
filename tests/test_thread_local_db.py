"""Thread-local SQLite connections under concurrent API-style access."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from lazyreview.db import get_run, upsert_pr
from lazyreview.db.schema import ThreadLocalConnection, connect, init_db


def test_thread_local_connection_survives_concurrent_reads(tmp_path: Path):
    db_path = tmp_path / "tl.db"
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
        (pr["id"], str(tmp_path / "w")),
    )
    bootstrap.commit()
    run_id = int(cur.lastrowid)
    bootstrap.close()

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
