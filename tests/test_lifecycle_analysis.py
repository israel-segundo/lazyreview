"""Lifecycle: analysis only on PR create; head moves cancel but do not re-enqueue."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from lazyreview.db import upsert_pr
from lazyreview.db.schema import connect, init_db
from lazyreview.services import lifecycle


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_upsert_enqueues_analysis_only_on_create(tmp_path: Path):
    conn = _conn(tmp_path)
    analyses = MagicMock()
    settings = MagicMock()
    lifecycle.bind(settings=settings, jobs=MagicMock(), consolidations=MagicMock(), analyses=analyses)

    try:
        with patch(
            "lazyreview.services.analysis_enqueue.ensure_analysis_for_pr"
        ) as ensure:
            upsert_pr(
                conn,
                owner="o",
                repo="r",
                number=1,
                url="https://github.com/o/r/pull/1",
                title="t",
                base_ref="main",
                head_ref="feat",
                head_sha="aaa",
                review_requested=True,
            )
            assert ensure.call_count == 1
            assert ensure.call_args.kwargs.get("force") is False

            # Same PR, same head (sync refresh) — no re-enqueue.
            upsert_pr(
                conn,
                owner="o",
                repo="r",
                number=1,
                url="https://github.com/o/r/pull/1",
                title="t updated",
                base_ref="main",
                head_ref="feat",
                head_sha="aaa",
                review_requested=True,
            )
            assert ensure.call_count == 1

            # New head — cancel in-flight, but do not start analysis again.
            upsert_pr(
                conn,
                owner="o",
                repo="r",
                number=1,
                url="https://github.com/o/r/pull/1",
                title="t updated",
                base_ref="main",
                head_ref="feat",
                head_sha="bbb",
                review_requested=True,
            )
            assert ensure.call_count == 1
            analyses.cancel_for_pr.assert_called()
    finally:
        lifecycle.unbind()
        conn.close()


def test_head_change_cancels_without_enqueue(tmp_path: Path):
    conn = _conn(tmp_path)
    jobs = MagicMock()
    consolidations = MagicMock()
    analyses = MagicMock()
    lifecycle.bind(settings=MagicMock(), jobs=jobs, consolidations=consolidations, analyses=analyses)

    try:
        with patch(
            "lazyreview.services.analysis_enqueue.ensure_analysis_for_pr"
        ) as ensure:
            lifecycle.on_pr_head_changed(conn, 42)
            jobs.cancel_for_pr.assert_called_once_with(42)
            consolidations.cancel_for_pr.assert_called_once_with(42)
            analyses.cancel_for_pr.assert_called_once_with(42)
            ensure.assert_not_called()
    finally:
        lifecycle.unbind()
        conn.close()
