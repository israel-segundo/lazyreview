"""PR GitHub labels: normalize, persist, match helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from lazyreview.db import get_pr, upsert_pr
from lazyreview.db.schema import connect, init_db
from lazyreview.services.gh import view_pr
from lazyreview.services.pr_labels import (
    normalize_pr_labels,
    pr_has_any_label,
    pr_has_label,
)


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_normalize_pr_labels_from_gh_shape():
    labels = normalize_pr_labels(
        [
            {"name": "bug", "color": "d73a4a"},
            {"name": "Docs", "color": "0075ca"},
            {"name": "bug", "color": "ffffff"},
            {"name": "  "},
        ]
    )
    assert [x["name"] for x in labels] == ["bug", "Docs"]
    assert labels[0]["color"] == "ffffff"


def test_pr_has_label_case_insensitive():
    pr = {"labels": [{"name": "do-not-review"}]}
    assert pr_has_label(pr, "Do-Not-Review")
    assert not pr_has_label(pr, "wip")
    assert pr_has_any_label(pr, ["wip", "do-not-review"])


def test_upsert_persists_and_clears_labels(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        pr = upsert_pr(
            conn,
            owner="o",
            repo="r",
            number=11,
            url="https://github.com/o/r/pull/11",
            title="t",
            base_ref="main",
            head_ref="feat",
            head_sha="abc",
            labels=[{"name": "bug", "color": "d73a4a"}, "wip"],
        )
        assert [x["name"] for x in pr["labels"]] == ["bug", "wip"]
        row = get_pr(conn, int(pr["id"]))
        assert [x["name"] for x in row["labels"]] == ["bug", "wip"]

        cleared = upsert_pr(
            conn,
            owner="o",
            repo="r",
            number=11,
            url="https://github.com/o/r/pull/11",
            title="t",
            base_ref="main",
            head_ref="feat",
            head_sha="abc",
            labels=[],
        )
        assert cleared["labels"] == []
        assert json.loads(cleared["labels_json"]) == []
    finally:
        conn.close()


def test_view_pr_includes_normalized_labels():
    pr_json = {
        "number": 4,
        "title": "t",
        "url": "https://github.com/o/r/pull/4",
        "baseRefName": "main",
        "headRefName": "feat",
        "headRefOid": "deadbeef",
        "state": "OPEN",
        "author": {"login": "u"},
        "isDraft": False,
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
        "commits": [],
        "reviews": [],
        "labels": [{"name": "enhancement", "color": "a2eeef", "description": ""}],
    }

    def fake_run(args):
        if args and args[0] == "pr":
            return json.dumps(pr_json)
        raise AssertionError(f"unexpected gh args: {args}")

    with (
        patch("lazyreview.services.gh.run_gh", side_effect=fake_run),
        patch(
            "lazyreview.services.gh._pr_github_flags",
            return_value={"in_merge_queue": False, "has_merge_conflicts": False},
        ),
    ):
        data = view_pr("https://github.com/o/r/pull/4")
    assert data["labels"] == [{"name": "enhancement", "color": "a2eeef"}]
