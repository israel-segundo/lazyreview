"""PR analysis eligibility: skip merged / draft / merge-queue."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lazyreview.db import create_pr_analysis, get_pr_analysis, upsert_pr
from lazyreview.db.schema import connect, init_db
from lazyreview.services import lifecycle
from lazyreview.services.analysis_eligibility import analysis_ineligible_reason
from lazyreview.services.analysis_enqueue import AnalysisSkipped, ensure_analysis_for_pr


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_analysis_ineligible_reason_cases():
    assert analysis_ineligible_reason({"state": "open"}) is None
    assert analysis_ineligible_reason({"state": "merged"}) == "PR is merged"
    assert analysis_ineligible_reason({"state": "closed"}) == "PR is closed"
    assert analysis_ineligible_reason({"state": "open", "is_draft": 1}) == "PR is a draft"
    assert (
        analysis_ineligible_reason({"state": "open", "is_draft": 1}, allow_draft=True)
        is None
    )
    assert (
        analysis_ineligible_reason({"state": "open", "in_merge_queue": True})
        == "PR is in the merge queue"
    )
    assert (
        analysis_ineligible_reason(
            {"state": "merged", "in_merge_queue": True, "is_draft": 1}
        )
        == "PR is merged"
    )


def test_upsert_skips_analysis_for_draft(tmp_path: Path):
    conn = _conn(tmp_path)
    analyses = MagicMock()
    lifecycle.bind(
        settings=MagicMock(), jobs=MagicMock(), consolidations=MagicMock(), analyses=analyses
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
                is_draft=True,
            )
            ensure.assert_not_called()
            analyses.cancel_for_pr.assert_called()
            assert "draft" in analyses.cancel_for_pr.call_args.kwargs["reason"]
    finally:
        lifecycle.unbind()
        conn.close()


def test_upsert_skips_analysis_for_merge_queue(tmp_path: Path):
    conn = _conn(tmp_path)
    analyses = MagicMock()
    lifecycle.bind(
        settings=MagicMock(), jobs=MagicMock(), consolidations=MagicMock(), analyses=analyses
    )
    try:
        with patch(
            "lazyreview.services.analysis_enqueue.ensure_analysis_for_pr"
        ) as ensure:
            upsert_pr(
                conn,
                owner="o",
                repo="r",
                number=2,
                url="https://github.com/o/r/pull/2",
                title="t",
                base_ref="main",
                head_ref="feat",
                head_sha="bbb",
                review_requested=True,
                in_merge_queue=True,
            )
            ensure.assert_not_called()
            assert "merge queue" in analyses.cancel_for_pr.call_args.kwargs["reason"]
    finally:
        lifecycle.unbind()
        conn.close()


def test_upsert_cancels_when_pr_merges(tmp_path: Path):
    conn = _conn(tmp_path)
    analyses = MagicMock()
    lifecycle.bind(
        settings=MagicMock(), jobs=MagicMock(), consolidations=MagicMock(), analyses=analyses
    )
    try:
        with patch("lazyreview.services.analysis_enqueue.ensure_analysis_for_pr"):
            pr = upsert_pr(
                conn,
                owner="o",
                repo="r",
                number=3,
                url="https://github.com/o/r/pull/3",
                title="t",
                base_ref="main",
                head_ref="feat",
                head_sha="ccc",
                review_requested=True,
            )
        analyses.reset_mock()
        upsert_pr(
            conn,
            owner="o",
            repo="r",
            number=3,
            url="https://github.com/o/r/pull/3",
            title="t",
            base_ref="main",
            head_ref="feat",
            head_sha="ccc",
            state="merged",
            review_requested=False,
        )
        analyses.cancel_for_pr.assert_called()
        assert "merged" in analyses.cancel_for_pr.call_args.kwargs["reason"]
        assert pr["id"]
    finally:
        lifecycle.unbind()
        conn.close()


def test_ensure_analysis_raises_for_merged(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=4,
        url="https://github.com/o/r/pull/4",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="ddd",
        state="merged",
    )
    with pytest.raises(AnalysisSkipped, match="merged"):
        ensure_analysis_for_pr(conn, MagicMock(), None, int(pr["id"]), force=True)


def test_ensure_analysis_allows_draft_when_forced(tmp_path: Path):
    conn = _conn(tmp_path)
    settings = MagicMock()
    settings.data_dir = tmp_path / "data"
    settings.data_dir.mkdir()
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=5,
        url="https://github.com/o/r/pull/5",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="eee",
        is_draft=True,
        review_requested=True,
    )
    runner = MagicMock()
    row = ensure_analysis_for_pr(
        conn, settings, runner, int(pr["id"]), force=True, allow_draft=True
    )
    assert row is not None
    assert row["status"] == "queued"
    runner.enqueue.assert_called_once()


def test_worker_skips_merge_queue_before_agent(tmp_path: Path):
    from lazyreview.services.analysis_jobs import AnalysisRunner

    conn = _conn(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    settings = MagicMock()
    settings.data_dir = data
    settings.db_path = tmp_path / "t.db"
    settings.max_concurrent_analyses = 1

    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=6,
        url="https://github.com/o/r/pull/6",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="fff",
        in_merge_queue=True,
    )
    work = data / "analyses" / "1"
    work.mkdir(parents=True)
    row = create_pr_analysis(
        conn,
        pr_id=int(pr["id"]),
        head_sha="fff",
        agent="claude",
        model="sonnet",
        effort="low",
        workdir_path=str(work),
    )
    runner = AnalysisRunner(settings, conn)
    with patch(
        "lazyreview.services.analysis_eligibility.refresh_pr_analysis_flags",
        side_effect=lambda p: p,
    ):
        runner._execute_with_conn(int(row["id"]), conn)
    updated = get_pr_analysis(conn, int(row["id"]))
    assert updated["status"] == "cancelled"
    assert "merge queue" in (updated.get("error_message") or "")
