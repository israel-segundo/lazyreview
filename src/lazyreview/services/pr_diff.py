"""Parse PR file patches to know which lines accept inline review comments."""

from __future__ import annotations

import json
import re
from typing import Any

from lazyreview.services.gh import GhError, run_gh

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def new_file_lines_in_patch(patch: str | None) -> frozenset[int]:
    """Line numbers on the RIGHT (new) side that appear in a unified diff patch."""
    if not patch:
        return frozenset()
    out: set[int] = set()
    new_line: int | None = None
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            new_line = int(m.group(1)) if m else None
            continue
        if new_line is None:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        out.add(new_line)
        new_line += 1
    return frozenset(out)


def snap_line_to_patch(line: int, available: frozenset[int]) -> int | None:
    """If ``line`` is not commentable, pick the nearest line that is (same file)."""
    if line in available:
        return line
    if not available:
        return None
    return min(available, key=lambda n: abs(n - line))


def list_pr_file_patches(owner: str, repo: str, number: int) -> dict[str, str]:
    """Fetch unified diff patches keyed by repo-relative path."""
    raw = run_gh(
        [
            "api",
            f"repos/{owner}/{repo}/pulls/{number}/files",
            "--paginate",
        ]
    )
    data = json.loads(raw or "[]")
    out: dict[str, str] = {}
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        path = str(item.get("filename") or "").strip()
        if path:
            out[path] = str(item.get("patch") or "")
    return out


def commentable_lines_by_path(patches: dict[str, str]) -> dict[str, frozenset[int]]:
    return {path: new_file_lines_in_patch(patch) for path, patch in patches.items()}


def filter_comments_for_diff_lines(
    comments: list[dict[str, Any]],
    lines_by_path: dict[str, frozenset[int]],
    *,
    snap: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Keep comments whose path/line GitHub can anchor on the PR diff.

    Returns (kept, skipped). When ``snap`` is true, nudges line to the nearest
    commentable line in the same file.
    """
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for c in comments:
        path = str(c.get("path") or "").strip()
        try:
            line = int(c.get("line"))
        except (TypeError, ValueError):
            skipped.append(c)
            continue
        available = lines_by_path.get(path)
        if available is None:
            skipped.append(c)
            continue
        use_line = line
        if use_line not in available:
            if snap:
                snapped = snap_line_to_patch(use_line, available)
                if snapped is not None:
                    use_line = snapped
                else:
                    skipped.append(c)
                    continue
            else:
                skipped.append(c)
                continue
        item = dict(c)
        item["line"] = use_line
        kept.append(item)
    return kept, skipped
