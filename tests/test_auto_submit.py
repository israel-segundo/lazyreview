"""Label-gated auto-submit of consolidation verdicts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from lazyreview.db import (
    create_consolidation,
    get_consolidation,
    list_feedback_actions,
    update_consolidation,
    upsert_pr,
)
from lazyreview.db.schema import connect, init_db
from lazyreview.services.consolidation_submit import maybe_auto_submit_consolidation
from lazyreview.services.settings import (
    get_auto_submit_labels,
    parse_label_list_setting,
    update_app_settings,
)


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _ready_consolidation(conn, *, labels=None):
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=42,
        url="https://github.com/o/r/pull/42",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="abc123",
        labels=labels or [{"name": "agent-verdict"}],
    )
    row = create_consolidation(
        conn,
        pr_id=int(pr["id"]),
        head_sha="abc123",
        source_run_ids=[],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path="",
    )
    update_consolidation(
        conn,
        int(row["id"]),
        status="succeeded",
        ready_to_post=1,
        publish_body="## Review\n\nLooks good.\n",
        recommended_action="approve",
        recommended_action_rationale="No blockers",
        findings_json="[]",
    )
    return pr, get_consolidation(conn, int(row["id"]))


def test_parse_label_list_setting():
    assert parse_label_list_setting("agent-verdict, wip") == ["agent-verdict", "wip"]
    assert parse_label_list_setting("") == []
    assert parse_label_list_setting(None, default=["agent-verdict"]) == ["agent-verdict"]


def test_default_auto_submit_labels(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        assert get_auto_submit_labels(conn) == ["agent-verdict"]
        update_app_settings(conn, auto_submit_labels="")
        assert get_auto_submit_labels(conn) == []
        update_app_settings(conn, auto_submit_labels="auto-bot, Agent-Verdict")
        assert get_auto_submit_labels(conn) == ["auto-bot", "Agent-Verdict"]
    finally:
        conn.close()


def test_maybe_auto_submit_posts_when_label_matches(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        _pr, row = _ready_consolidation(conn)
        fake = {
            "html_url": "https://github.com/o/r/pull/42#pullrequestreview-1",
            "id": "1",
            "dropped_comments": [],
            "skipped_off_diff_count": 0,
            "skipped_bad_line_count": 0,
            "posted_inline_count": 0,
        }
        with (
            patch(
                "lazyreview.services.consolidation_submit._refresh_pr_labels",
                side_effect=lambda c, pr: pr,
            ),
            patch(
                "lazyreview.services.consolidation_submit.submit_pr_review",
                return_value=fake,
            ) as submit,
            patch(
                "lazyreview.services.consolidation_submit.list_pr_file_patches",
                return_value={},
            ),
        ):
            result = maybe_auto_submit_consolidation(conn, int(row["id"]))
        assert result is not None
        assert result["auto_submitted"] is True
        submit.assert_called_once()
        updated = get_consolidation(conn, int(row["id"]))
        assert int(updated["feedback_provided"] or 0) == 1
        assert int(updated["auto_submitted"] or 0) == 1
        assert updated["gh_action"] == "approve"
        actions = list_feedback_actions(conn, int(row["pr_id"]))
        assert len(actions) == 1
        assert int(actions[0]["auto_submitted"] or 0) == 1
    finally:
        conn.close()


def test_maybe_auto_submit_skips_without_label(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        _pr, row = _ready_consolidation(conn, labels=[{"name": "docs"}])
        with (
            patch(
                "lazyreview.services.consolidation_submit._refresh_pr_labels",
                side_effect=lambda c, pr: pr,
            ),
            patch(
                "lazyreview.services.consolidation_submit.submit_pr_review"
            ) as submit,
        ):
            result = maybe_auto_submit_consolidation(conn, int(row["id"]))
        assert result is None
        submit.assert_not_called()
        updated = get_consolidation(conn, int(row["id"]))
        assert int(updated["feedback_provided"] or 0) == 0
    finally:
        conn.close()


def test_maybe_auto_submit_skips_when_disabled(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        update_app_settings(conn, auto_submit_labels="")
        _pr, row = _ready_consolidation(conn)
        with patch(
            "lazyreview.services.consolidation_submit.submit_pr_review"
        ) as submit:
            result = maybe_auto_submit_consolidation(conn, int(row["id"]))
        assert result is None
        submit.assert_not_called()
    finally:
        conn.close()
