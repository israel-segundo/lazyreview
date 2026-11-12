"""Head-move eviction cancels reviews, consolidations, publish-only, and chat."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from lazyreview.config import Settings
from lazyreview.db import (
    create_consolidation,
    create_run,
    get_consolidation,
    get_run,
    update_consolidation,
    update_run,
    upsert_pr,
)
from lazyreview.db.schema import connect, init_db
from lazyreview.services.consolidation_jobs import ConsolidationRunner
from lazyreview.services.jobs import JobRunner
from lazyreview.services.lifecycle import HEAD_MOVED_CANCEL_REASON, on_pr_head_changed
from lazyreview.services import lifecycle


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
    )


def test_cancel_for_pr_stops_publish_only(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
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
        head_sha="old",
    )
    consol = create_consolidation(
        conn,
        pr_id=int(pr["id"]),
        head_sha="old",
        source_run_ids=[],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c"),
        status="queued",
    )
    update_consolidation(
        conn,
        int(consol["id"]),
        status="succeeded",
        publish_status="queued",
        ready_to_post=0,
        body="## internal",
        finished_at="2026-01-01T00:00:00+00:00",
    )
    runner = ConsolidationRunner(settings, conn)
    with runner._lock:
        runner._publish_only.add(int(consol["id"]))

    cancelled = runner.cancel_for_pr(
        int(pr["id"]), reason=HEAD_MOVED_CANCEL_REASON
    )
    assert int(consol["id"]) in cancelled
    updated = get_consolidation(conn, int(consol["id"]))
    assert updated["status"] == "succeeded"
    assert updated["publish_status"] == "cancelled"
    assert HEAD_MOVED_CANCEL_REASON in (updated.get("error_message") or "")
    with runner._lock:
        assert int(consol["id"]) not in runner._publish_only
        assert int(consol["id"]) in runner._cancel_requested
    conn.close()


def test_cancel_for_pr_reviews_uses_reason(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
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
        head_sha="old",
    )
    run = create_run(
        conn,
        pr_id=int(pr["id"]),
        agent="claude",
        model="opus",
        effort="high",
        skill="pr-code-review-batch",
        head_sha="old",
        workdir_path=str(tmp_path / "r"),
    )
    update_run(conn, int(run["id"]), status="queued")
    runner = JobRunner(settings, conn)
    cancelled = runner.cancel_for_pr(int(pr["id"]), reason=HEAD_MOVED_CANCEL_REASON)
    assert cancelled == [int(run["id"])]
    updated = get_run(conn, int(run["id"]))
    assert updated["status"] == "cancelled"
    assert updated["error_message"] == HEAD_MOVED_CANCEL_REASON
    conn.close()


def test_on_pr_head_changed_cancels_sessions(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    jobs = MagicMock()
    consolidations = MagicMock()
    analyses = MagicMock()
    sessions = MagicMock()
    lifecycle.bind(
        settings=MagicMock(),
        jobs=jobs,
        consolidations=consolidations,
        analyses=analyses,
        sessions=sessions,
    )
    try:
        on_pr_head_changed(conn, 7)
        reason = HEAD_MOVED_CANCEL_REASON
        jobs.cancel_for_pr.assert_called_once_with(7, reason=reason)
        consolidations.cancel_for_pr.assert_called_once_with(7, reason=reason)
        analyses.cancel_for_pr.assert_called_once_with(7, reason=reason)
        sessions.cancel_for_pr.assert_called_once_with(7, reason=reason)
    finally:
        lifecycle.unbind()
        conn.close()
