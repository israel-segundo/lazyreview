"""PR diff patch parsing for inline comment anchors."""

from __future__ import annotations

from lazyreview.services.pr_diff import (
    filter_comments_for_diff_lines,
    new_file_lines_in_patch,
    snap_line_to_patch,
)


def test_new_file_lines_in_patch():
    patch = """@@ -10,3 +10,4 @@ context
 unchanged
-old
+added line
 still here
"""
    lines = new_file_lines_in_patch(patch)
    assert lines == frozenset({10, 11, 12})


def test_snap_line_to_patch():
    available = frozenset({100, 105, 110})
    assert snap_line_to_patch(105, available) == 105
    assert snap_line_to_patch(349, available) == 110


def test_filter_comments_snaps_line():
    patch = """@@ -450,5 +450,10 @@
     const x = 1;
+    const breakdown = {};
"""
    lines = new_file_lines_in_patch(patch)
    comments = [{"path": "a.ts", "line": 349, "body": "c"}]
    lines_by_path = {"a.ts": lines}
    kept, skipped = filter_comments_for_diff_lines(comments, lines_by_path, snap=True)
    assert len(kept) == 1
    assert kept[0]["line"] != 349
    assert kept[0]["line"] in lines
