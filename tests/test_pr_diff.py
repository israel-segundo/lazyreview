"""PR diff patch parsing for inline review comment anchors."""

from __future__ import annotations

from lazyreview.services.gh import _post_inline_comments_one_review
from lazyreview.services.pr_diff import (
    filter_comments_for_diff_lines,
    new_file_lines_in_patch,
    snap_line_to_patch,
)
from unittest.mock import patch


def test_new_file_lines_in_patch():
    patch_text = """@@ -10,3 +10,4 @@ context
 unchanged
-old
+added line
 still here
"""
    lines = new_file_lines_in_patch(patch_text)
    assert lines == frozenset({10, 11, 12})


def test_snap_line_to_patch():
    available = frozenset({100, 105, 110})
    assert snap_line_to_patch(105, available) == 105
    assert snap_line_to_patch(349, available) == 110


def test_filter_comments_snaps_line():
    patch_text = """@@ -450,5 +450,10 @@
     const x = 1;
+    const breakdown = {};
"""
    lines = new_file_lines_in_patch(patch_text)
    comments = [{"path": "a.ts", "line": 349, "body": "c"}]
    lines_by_path = {"a.ts": lines}
    kept, skipped = filter_comments_for_diff_lines(comments, lines_by_path, snap=True)
    assert len(kept) == 1
    assert kept[0]["line"] != 349
    assert kept[0]["line"] in lines


def test_post_inline_comments_one_review_single_batch():
    captured: list[str] = []

    def _fake_post(owner, repo, number, payload):
        captured.append(payload)
        return {"ok": True, "id": "1", "html_url": "u"}

    comments = [
        {"path": "a.py", "line": 1, "side": "RIGHT", "body": "one"},
        {"path": "b.py", "line": 2, "side": "RIGHT", "body": "two"},
    ]
    with patch("lazyreview.services.gh._post_review", side_effect=_fake_post):
        posted, dropped = _post_inline_comments_one_review(
            "o", "r", 1, commit_id="abc", comments=comments
        )
    assert posted == comments
    assert dropped == []
    assert len(captured) == 1
    import json

    data = json.loads(captured[0])
    assert len(data["comments"]) == 2
    assert data["event"] == "COMMENT"
