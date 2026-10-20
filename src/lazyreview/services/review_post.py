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


def finding_has_inline_anchor(finding: dict[str, Any]) -> bool:
    path = str(finding.get("path") or "").strip()
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


def build_review_comments(
    inline_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for f in inline_findings:
        path = str(f.get("path") or "").strip()
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
