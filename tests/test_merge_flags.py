"""GitHub merge-queue / merge-conflict flags on PR records."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from lazyreview.db import get_pr, upsert_pr
from lazyreview.db.schema import connect, init_db
from lazyreview.services.consolidation_jobs import (
    _MERGE_CONFLICT_FINDING_ID,
    apply_merge_conflict_constraints,
)
from lazyreview.services.gh import _pr_github_flags, view_pr


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_pr_github_flags_parses_mergeable_and_queue():
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "isInMergeQueue": True,
                    "mergeable": "CONFLICTING",
                }
            }
        }
    }
    with patch(
        "lazyreview.services.gh.run_gh",
        return_value=json.dumps(payload),
    ):
        flags = _pr_github_flags("o", "r", 1)
    assert flags == {"in_merge_queue": True, "has_merge_conflicts": True}


def test_pr_github_flags_unknown_mergeable_is_not_conflicts():
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "isInMergeQueue": False,
                    "mergeable": "UNKNOWN",
                }
            }
        }
    }
    with patch(
        "lazyreview.services.gh.run_gh",
        return_value=json.dumps(payload),
    ):
        flags = _pr_github_flags("o", "r", 2)
    assert flags == {"in_merge_queue": False, "has_merge_conflicts": False}


def test_view_pr_sets_conflict_flag_for_open_prs():
    pr_json = {
        "number": 9,
        "title": "t",
        "url": "https://github.com/o/r/pull/9",
        "baseRefName": "main",
        "headRefName": "feat",
        "headRefOid": "abc",
        "state": "OPEN",
        "author": {"login": "u"},
        "isDraft": False,
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
        "commits": [],
        "reviews": [],
    }

    def fake_run(args):
        if args and args[0] == "pr":
            return json.dumps(pr_json)
        raise AssertionError(f"unexpected gh args: {args}")

    with (
        patch("lazyreview.services.gh.run_gh", side_effect=fake_run),
        patch(
            "lazyreview.services.gh._pr_github_flags",
            return_value={"in_merge_queue": False, "has_merge_conflicts": True},
        ),
    ):
        data = view_pr("https://github.com/o/r/pull/9")
    assert data["has_merge_conflicts"] is True
    assert data["in_merge_queue"] is False


def test_upsert_persists_and_clears_merge_conflicts(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        pr = upsert_pr(
            conn,
            owner="o",
            repo="r",
            number=3,
            url="https://github.com/o/r/pull/3",
            title="t",
            base_ref="main",
            head_ref="feat",
            head_sha="aaa",
            has_merge_conflicts=True,
        )
        assert int(pr["has_merge_conflicts"] or 0) == 1
        cleared = upsert_pr(
            conn,
            owner="o",
            repo="r",
            number=3,
            url="https://github.com/o/r/pull/3",
            title="t",
            base_ref="main",
            head_ref="feat",
            head_sha="aaa",
            has_merge_conflicts=False,
        )
        assert int(cleared["has_merge_conflicts"] or 0) == 0
        row = get_pr(conn, int(cleared["id"]))
        assert int(row["has_merge_conflicts"] or 0) == 0
    finally:
        conn.close()


def test_apply_merge_conflict_constraints_injects_finding_and_forces_verdict():
    findings, action, rationale, changed = apply_merge_conflict_constraints(
        has_merge_conflicts=True,
        findings=[{"id": "C1", "severity": 3, "title": "nit"}],
        recommended_action="approve",
        rationale="Looks fine",
    )
    assert changed is True
    assert action == "request_changes"
    assert "conflict" in rationale.lower()
    assert findings[0]["id"] == _MERGE_CONFLICT_FINDING_ID
    assert int(findings[0]["severity"]) == 1
    assert len(findings) == 2


def test_apply_merge_conflict_constraints_skips_duplicate_conflict_finding():
    existing = {
        "id": "C2",
        "severity": 1,
        "title": "Fix merge conflicts before landing",
        "body": "Branch is unmergeable",
    }
    findings, action, rationale, changed = apply_merge_conflict_constraints(
        has_merge_conflicts=True,
        findings=[existing],
        recommended_action="request_changes",
        rationale="Merge conflicts with the base branch.",
    )
    assert action == "request_changes"
    assert len(findings) == 1
    assert findings[0]["id"] == "C2"
    assert changed is False


def test_apply_merge_conflict_constraints_noop_without_conflicts():
    findings, action, rationale, changed = apply_merge_conflict_constraints(
        has_merge_conflicts=False,
        findings=[{"id": "C1", "severity": 2, "title": "x"}],
        recommended_action="approve",
        rationale="ok",
    )
    assert changed is False
    assert action == "approve"
    assert len(findings) == 1
