"""Helpers for posting consolidations as summary + inline GitHub review comments."""

from __future__ import annotations

import json
import re
from typing import Any

_BLOCKING_DISPOSITION = re.compile(
    r"fix\s+before\s+merge|blocker|blocks?\s+merge|"
    r"compromises?\s+(the\s+)?(build|ac|acceptance)|"
    r"must\s+fix\s+before|"
    r"build\s+is\s+red|ci\s+is\s+red",
    re.I,
)
_NONBLOCKING_DISPOSITION = re.compile(
    r"fast[- ]follow|author'?s\s+call|out\s+of\s+scope|follow[_ ]?up",
    re.I,
)


def parse_findings_list(raw: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [f for f in raw if isinstance(f, dict)]
    text = str(raw).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [f for f in data if isinstance(f, dict)]


def finding_is_blocking(finding: dict[str, Any]) -> bool:
    """True when disposition / body says the finding must block merge."""
    disposition = str(finding.get("disposition") or "").strip()
    body = str(finding.get("body") or "")
    title = str(finding.get("title") or "")
    blob = f"{disposition}\n{title}\n{body}"
    if _NONBLOCKING_DISPOSITION.search(disposition) and not _BLOCKING_DISPOSITION.search(
        disposition
    ):
        return False
    if _BLOCKING_DISPOSITION.search(blob):
        return True
    # Explicit verify class if present on the object
    klass = str(finding.get("class") or finding.get("verify_class") or "").strip().lower()
    if klass == "blocker":
        return True
    if klass in {"follow_up", "follow-up", "false_alarm", "unverified"}:
        return False
    return False


def recommended_action_from_findings(findings: list[dict[str, Any]]) -> str:
    """Safety-net action when meta is missing/invalid. Prompts remain primary."""
    if any(finding_is_blocking(f) for f in findings):
        return "request_changes"
    return "approve"


def finding_path(finding: dict[str, Any]) -> str:
    """Repo-relative path for an inline anchor (``path`` or legacy ``file``)."""
    return str(finding.get("path") or finding.get("file") or "").strip()


def finding_has_inline_anchor(finding: dict[str, Any]) -> bool:
    path = finding_path(finding)
    line = finding.get("line")
    if not path or line is None or line == "":
        return False
    try:
        int(line)
    except (TypeError, ValueError):
        return False
    return True


def split_findings_for_post(
    findings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (inline_findings, summary_only_findings)."""
    inline: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for f in findings:
        if finding_has_inline_anchor(f):
            inline.append(f)
        else:
            summary.append(f)
    return inline, summary


def inline_comment_body(finding: dict[str, Any]) -> str:
    """Short inline review comment body (1–3 sentences), led by finding id."""
    fid = str(finding.get("id") or "").strip() or "?"
    title = str(finding.get("title") or "").strip()
    body = str(finding.get("body") or "").strip()
    disposition = str(finding.get("disposition") or "").strip()
    # Prefer title + first sentences of body; avoid dumping the whole cell.
    detail = body or title
    if detail and title and not body.lower().startswith(title.lower()):
        detail = f"{title}. {body}".strip() if body else title
    # Collapse whitespace; keep ~3 sentences max.
    detail = re.sub(r"\s+", " ", detail).strip()
    sentences = re.split(r"(?<=[.!?])\s+", detail) if detail else []
    detail = " ".join(sentences[:3]).strip()
    if disposition and disposition not in detail:
        if not disposition.startswith("_"):
            disposition = f"_{disposition}_"
        detail = f"{detail} {disposition}".strip()
    if not detail:
        detail = "See consolidation finding."
    return f"**{fid}** — {detail}"


def filter_comments_for_changed_paths(
    comments: list[dict[str, Any]],
    changed_paths: set[str] | frozenset[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop review comments whose ``path`` is not in the PR diff file list."""
    if not changed_paths:
        return list(comments), []
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for c in comments:
        path = str(c.get("path") or "").strip()
        if path and path in changed_paths:
            kept.append(c)
        else:
            skipped.append(c)
    return kept, skipped


def count_inline_post_plan(
    findings: list[dict[str, Any]],
    changed_paths: set[str] | frozenset[str] | None = None,
) -> dict[str, int]:
    """Counts for UI: inline on diff, summary-only, and off-diff anchors."""
    inline, summary, off_diff = 0, 0, 0
    use_diff = bool(changed_paths)
    for f in findings:
        if not finding_has_inline_anchor(f):
            summary += 1
            continue
        path = finding_path(f)
        if use_diff and path not in changed_paths:
            off_diff += 1
            summary += 1
            continue
        inline += 1
    return {
        "inline": inline,
        "summary": summary,
        "off_diff": off_diff,
        "total": len(findings),
    }


def build_review_comments(
    inline_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for f in inline_findings:
        path = finding_path(f)
        try:
            line = int(f.get("line"))
        except (TypeError, ValueError):
            continue
        if not path or line < 1:
            continue
        comments.append(
            {
                "path": path,
                "line": line,
                "side": "RIGHT",
                "body": inline_comment_body(f),
            }
        )
    return comments
