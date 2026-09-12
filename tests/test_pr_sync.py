"""PR sync: pending queue refresh and external review detection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from lazyreview.db import get_pr, pr_internal_status, record_external_pr_review, upsert_pr
from lazyreview.db.schema import connect, init_db
from lazyreview.services.gh import parse_user_review_for_head
from lazyreview.services.pr_sync import sync_pending_review_prs


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _pr(conn, *, head="abc123"):
    return upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=1,
        url="https://github.com/o/r/pull/1",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha=head,
        review_requested=True,
    )


def test_parse_user_review_for_head_picks_latest_on_head():
    reviews = [
        {
            "author": {"login": "me"},
            "state": "COMMENTED",
            "commitOID": "abc123",
            "id": "1",
            "submittedAt": "2026-01-01T00:00:00Z",
        },
        {
            "author": {"login": "me"},
            "state": "APPROVED",
            "commitOID": "abc123",
            "id": "2",
            "submittedAt": "2026-01-02T00:00:00Z",
        },
        {
            "author": {"login": "me"},
            "state": "APPROVED",
            "commitOID": "oldsha",
            "id": "3",
            "submittedAt": "2026-01-03T00:00:00Z",
        },
        {
            "author": {"login": "other"},
            "state": "APPROVED",
            "commitOID": "abc123",
            "id": "4",
            "submittedAt": "2026-01-04T00:00:00Z",
        },
    ]
    review = parse_user_review_for_head(reviews, head_sha="abc123", login="me")
    assert review is not None
    assert review["gh_action"] == "approve"
    assert review["gh_review_id"] == "2"


def test_parse_user_review_for_head_request_changes():
    reviews = [
        {
            "author": {"login": "me"},
            "state": "CHANGES_REQUESTED",
            "commitOID": "deadbeef",
            "id": "9",
            "submittedAt": "2026-01-01T00:00:00Z",
        },
    ]
    review = parse_user_review_for_head(reviews, head_sha="deadbeef", login="me")
    assert review is not None
    assert review["gh_action"] == "request_changes"


def test_parse_user_review_for_head_gh_pr_view_commit_shape():
    """gh pr view --json reviews nests commit as { oid: ... }."""
    reviews = [
        {
            "author": {"login": "me"},
            "state": "APPROVED",
            "commit": {"oid": "924a23efe112e6c5b524bc5ef16279536d9a566a"},
            "id": "PRR_kwDOQCT1P88AAAABMp2xyA",
            "submittedAt": "2026-09-08T16:08:58Z",
        },
        {
            "author": {"login": "me"},
            "state": "CHANGES_REQUESTED",
            "commit": {"oid": "395787615a020b772ce8aee39cc4a747f557798d"},
            "id": "PRR_kwDOQCT1P88AAAABMggReQ",
            "submittedAt": "2026-09-07T17:38:33Z",
        },
    ]
    review = parse_user_review_for_head(
        reviews,
        head_sha="924a23efe112e6c5b524bc5ef16279536d9a566a",
        login="me",
    )
    assert review is not None
    assert review["gh_action"] == "approve"
    assert review["gh_review_id"] == "PRR_kwDOQCT1P88AAAABMp2xyA"


def test_record_external_pr_review_updates_internal_status(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn, head="abc123")
    assert pr_internal_status(conn, pr)["status"] == "needs_review"

    recorded = record_external_pr_review(
        conn,
        pr,
        gh_action="approve",
        gh_review_id="42",
        body="LGTM",
    )
    assert recorded is True
    refreshed = get_pr(conn, pr["id"])
    assert pr_internal_status(conn, refreshed)["status"] == "approved"

    # Idempotent for the same GitHub review id.
    assert (
        record_external_pr_review(
            conn,
            refreshed,
            gh_action="approve",
            gh_review_id="42",
        )
        is False
    )


def test_record_external_pr_review_skips_when_lazyreview_already_submitted(tmp_path: Path):
    from lazyreview.db import create_consolidation, update_consolidation

    conn = _conn(tmp_path)
    pr = _pr(conn, head="abc123")
    create_consolidation(
        conn,
        pr_id=pr["id"],
        head_sha="abc123",
        source_run_ids=[],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c"),
        status="queued",
    )
    update_consolidation(
        conn,
        1,
        status="succeeded",
        feedback_provided=1,
        gh_action="comment",
        body="from lazyreview",
    )
    assert (
        record_external_pr_review(
            conn,
            pr,
            gh_action="approve",
            gh_review_id="99",
        )
        is False
    )


def test_sync_pending_review_prs_records_external_approval(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn, head="head1")
    view_data = {
        "owner": "o",
        "repo": "r",
        "number": 1,
        "url": pr["url"],
        "title": "t",
        "author": "author",
        "base_ref": "main",
        "head_ref": "feat",
        "head_sha": "head1",
        "head_updated_at": "2026-01-01T00:00:00Z",
        "state": "open",
        "is_draft": False,
        "reviews": [
            {
                "author": {"login": "me"},
                "state": "APPROVED",
                "commit": {"oid": "head1"},
                "id": "77",
                "submittedAt": "2026-01-02T00:00:00Z",
            }
        ],
    }

    with (
        patch(
            "lazyreview.services.pr_sync.search_pending_review_prs",
            return_value=[{"owner": "o", "repo": "r", "number": 1, "url": pr["url"], "title": "t"}],
        ),
        patch("lazyreview.services.pr_sync.view_pr", return_value=view_data),
        patch("lazyreview.services.pr_sync.gh_user_login", return_value="me"),
    ):
        result = sync_pending_review_prs(conn)

    assert result["external_reviews_synced"] == 1
    refreshed = get_pr(conn, pr["id"])
    assert pr_internal_status(conn, refreshed)["status"] == "approved"


def test_sync_pending_review_prs_open_needs_review_not_in_queue(tmp_path: Path):
    """Approved-on-GitHub PRs leave the pending queue but stay in the local DB."""
    conn = _conn(tmp_path)
    pr = _pr(conn, head="head1")
    conn.execute("UPDATE prs SET review_requested = 0 WHERE id = ?", (pr["id"],))
    conn.commit()
    view_data = {
        "owner": "o",
        "repo": "r",
        "number": 1,
        "url": pr["url"],
        "title": "t",
        "author": "author",
        "base_ref": "main",
        "head_ref": "feat",
        "head_sha": "head1",
        "head_updated_at": "2026-01-01T00:00:00Z",
        "state": "open",
        "is_draft": False,
        "reviews": [
            {
                "author": {"login": "me"},
                "state": "APPROVED",
                "commit": {"oid": "head1"},
                "id": "77",
                "submittedAt": "2026-01-02T00:00:00Z",
            }
        ],
    }

    with (
        patch("lazyreview.services.pr_sync.search_pending_review_prs", return_value=[]),
        patch("lazyreview.services.pr_sync.view_pr", return_value=view_data),
        patch("lazyreview.services.pr_sync.gh_user_login", return_value="me"),
    ):
        result = sync_pending_review_prs(conn)

    assert result["external_reviews_synced"] == 1
    refreshed = get_pr(conn, pr["id"])
    assert pr_internal_status(conn, refreshed)["status"] == "approved"


def test_sync_pending_review_prs_stale_pr_gets_external_review(tmp_path: Path):
    conn = _conn(tmp_path)
    pr = _pr(conn, head="head2")
    view_data = {
        "owner": "o",
        "repo": "r",
        "number": 1,
        "url": pr["url"],
        "title": "t",
        "author": "author",
        "base_ref": "main",
        "head_ref": "feat",
        "head_sha": "head2",
        "head_updated_at": "2026-01-01T00:00:00Z",
        "state": "open",
        "is_draft": False,
        "reviews": [
            {
                "author": {"login": "me"},
                "state": "CHANGES_REQUESTED",
                "commit": {"oid": "head2"},
                "id": "88",
                "submittedAt": "2026-01-02T00:00:00Z",
            }
        ],
    }

    with (
        patch("lazyreview.services.pr_sync.search_pending_review_prs", return_value=[]),
        patch("lazyreview.services.pr_sync.view_pr", return_value=view_data),
        patch("lazyreview.services.pr_sync.gh_user_login", return_value="me"),
    ):
        result = sync_pending_review_prs(conn)

    assert result["external_reviews_synced"] == 1
    refreshed = get_pr(conn, pr["id"])
    assert pr_internal_status(conn, refreshed)["status"] == "changes_requested"
    assert int(refreshed["review_requested"]) == 0
