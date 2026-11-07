"""Lifecycle: analysis on PR create; optional re-enqueue on head move."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from lazyreview.db import set_setting, upsert_pr
from lazyreview.db.schema import connect, init_db
from lazyreview.services import lifecycle
from lazyreview.services.settings import get_settings_view, update_app_settings


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_upsert_enqueues_analysis_on_create_and_head_when_enabled(tmp_path: Path):
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

            # New head with default setting on — cancel then re-enqueue.
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
            assert ensure.call_count == 2
            analyses.cancel_for_pr.assert_called()
    finally:
        lifecycle.unbind()
        conn.close()


def test_upsert_enqueues_analysis_on_head_even_without_review_requested(
    tmp_path: Path,
):
    """Stale-queue sync clears review_requested before reconcile; head move must still analyze."""
    conn = _conn(tmp_path)
    analyses = MagicMock()
    lifecycle.bind(
        settings=MagicMock(),
        jobs=MagicMock(),
        consolidations=MagicMock(),
        analyses=analyses,
    )

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

            # Mimic sync's stale path: new head + review_requested cleared.
            upsert_pr(
                conn,
                owner="o",
                repo="r",
                number=1,
                url="https://github.com/o/r/pull/1",
                title="t",
                base_ref="main",
                head_ref="feat",
                head_sha="bbb",
                review_requested=False,
            )
            assert ensure.call_count == 2
            analyses.cancel_for_pr.assert_called()
    finally:
        lifecycle.unbind()
        conn.close()


def test_upsert_skips_analysis_on_head_when_disabled(tmp_path: Path):
    conn = _conn(tmp_path)
    set_setting(conn, "pr_analysis_on_head_change", "false")
    conn.commit()
    analyses = MagicMock()
    lifecycle.bind(
        settings=MagicMock(),
        jobs=MagicMock(),
        consolidations=MagicMock(),
        analyses=analyses,
    )

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

            upsert_pr(
                conn,
                owner="o",
                repo="r",
                number=1,
                url="https://github.com/o/r/pull/1",
                title="t",
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


def test_head_change_cancels_without_enqueue_when_called_directly(tmp_path: Path):
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


def test_pr_analysis_on_head_change_settings_round_trip(tmp_path: Path):
    conn = _conn(tmp_path)
    view = get_settings_view(conn)
    assert view["pr_analysis_on_head_change"] is True

    updated = update_app_settings(conn, pr_analysis_on_head_change=False)
    assert updated["pr_analysis_on_head_change"] is False
    assert get_settings_view(conn)["pr_analysis_on_head_change"] is False

    updated = update_app_settings(conn, pr_analysis_on_head_change=True)
    assert updated["pr_analysis_on_head_change"] is True
    conn.close()
