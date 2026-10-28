"""Helpers for posting consolidations as summary + inline GitHub review comments."""

from __future__ import annotations

import json
import re
from pathlib import Path
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


def _finding_severity_rank(finding: dict[str, Any]) -> int | None:
    """Normalize severity to 1 (High) / 2 (Medium) / 3 (Low) when possible."""
    raw = finding.get("severity")
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        n = int(raw)
        return n if n in (1, 2, 3) else None
    text = str(raw).strip().lower()
    if text in {"1", "high", "s1", "sev1", "severity 1"}:
        return 1
    if text in {"2", "medium", "s2", "sev2", "severity 2"}:
        return 2
    if text in {"3", "low", "s3", "sev3", "severity 3"}:
        return 3
    try:
        n = int(text)
    except ValueError:
        return None
    return n if n in (1, 2, 3) else None


def recommended_action_from_findings(findings: list[dict[str, Any]]) -> str:
    """Safety-net action when meta is missing/invalid. Prompts remain primary."""
    if any(finding_is_blocking(f) for f in findings):
        return "request_changes"
    if any(_finding_severity_rank(f) == 1 for f in findings):
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
    """Return (inline_findings, summary_only_findings) by anchor presence only."""
    inline: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for f in findings:
        if finding_has_inline_anchor(f):
            inline.append(f)
        else:
            summary.append(f)
    return inline, summary


def finding_display_id(finding: dict[str, Any], *, index: int = 0) -> str:
    fid = str(finding.get("id") or "").strip()
    return fid or f"C{index + 1}"


def _finding_posts_inline_on_diff(
    finding: dict[str, Any],
    *,
    changed_paths: set[str] | frozenset[str] | None,
    commentable_lines: dict[str, frozenset[int]] | None,
) -> bool:
    if not finding_has_inline_anchor(finding):
        return False
    path = finding_path(finding)
    try:
        line = int(finding.get("line"))
    except (TypeError, ValueError):
        return False
    use_paths = bool(changed_paths)
    use_lines = bool(commentable_lines)
    if use_paths and path not in (changed_paths or ()):
        return False
    if use_lines:
        from lazyreview.services.pr_diff import snap_line_to_patch

        available = (commentable_lines or {}).get(path)
        if available is None:
            return False
        if line not in available and snap_line_to_patch(line, available) is None:
            return False
    return True


def partition_findings_for_github_post(
    findings: list[dict[str, Any]],
    changed_paths: set[str] | frozenset[str] | None = None,
    commentable_lines: dict[str, frozenset[int]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split findings into (inline on PR diff, summary-only for review body)."""
    inline: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for f in findings:
        if _finding_posts_inline_on_diff(
            f,
            changed_paths=changed_paths,
            commentable_lines=commentable_lines,
        ):
            inline.append(f)
        else:
            summary.append(f)
    return inline, summary


def build_github_post_plan(
    findings: list[dict[str, Any]],
    changed_paths: set[str] | frozenset[str] | None = None,
    commentable_lines: dict[str, frozenset[int]] | None = None,
) -> dict[str, list[str]]:
    """Machine-readable inline vs summary split for publish transform + UI."""
    inline, summary = partition_findings_for_github_post(
        findings, changed_paths, commentable_lines
    )
    inline_ids = [finding_display_id(f, index=i) for i, f in enumerate(inline)]
    summary_ids = [finding_display_id(f, index=i) for i, f in enumerate(summary)]
    off_diff_ids: list[str] = []
    bad_line_ids: list[str] = []
    use_paths = bool(changed_paths)
    use_lines = bool(commentable_lines)
    for i, f in enumerate(findings):
        fid = finding_display_id(f, index=i)
        if not finding_has_inline_anchor(f):
            continue
        path = finding_path(f)
        try:
            int(f.get("line"))
        except (TypeError, ValueError):
            continue
        if use_paths and path not in (changed_paths or ()):
            off_diff_ids.append(fid)
            continue
        if use_lines:
            from lazyreview.services.pr_diff import snap_line_to_patch

            available = (commentable_lines or {}).get(path)
            if available is None:
                off_diff_ids.append(fid)
                continue
            line = int(f.get("line"))
            if line not in available and snap_line_to_patch(line, available) is None:
                bad_line_ids.append(fid)
    return {
        "inline_ids": inline_ids,
        "summary_only_ids": summary_ids,
        "off_diff_ids": off_diff_ids,
        "bad_line_ids": bad_line_ids,
    }


def write_github_post_plan_file(
    work_dir: Path,
    *,
    owner: str,
    repo: str,
    number: int,
    findings: list[dict[str, Any]],
) -> None:
    """Write ``post-plan.json`` in a consolidation workdir (best-effort diff fetch)."""
    from lazyreview.services.pr_diff import commentable_lines_by_path, list_pr_file_patches

    try:
        patches = list_pr_file_patches(owner, repo, number)
        changed = frozenset(patches.keys())
        lines = commentable_lines_by_path(patches)
        plan = build_github_post_plan(findings, changed, lines)
    except Exception:
        plan = build_github_post_plan(findings)
    (work_dir / "post-plan.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8"
    )


def _review_body_includes_finding(body: str, finding_id: str) -> bool:
    if not finding_id.strip():
        return False
    fid = re.escape(finding_id.strip())
    return bool(re.search(rf"\*\*{fid}(?:\s|[·—]|\*\*)", body))


def format_summary_only_findings_md(
    findings: list[dict[str, Any]],
    *,
    changed_paths: set[str] | frozenset[str] | None = None,
) -> str:
    """Markdown blocks for findings that cannot be posted as inline diff comments."""
    if not findings:
        return ""
    sev_name = {1: "High", 2: "Medium", 3: "Low"}
    lines = ["", "## Findings (included in review body)", ""]
    for i, f in enumerate(findings):
        sev = int(f.get("severity") or 3)
        claim = (f.get("title") or finding_display_id(f, index=i)).strip()
        body = (f.get("body") or "").strip()
        fid = finding_display_id(f, index=i)
        disposition = str(f.get("disposition") or "").strip()
        lines.append(
            f"**{fid} · {sev_name.get(sev, 'Low')} — {claim}.** {body}".strip()
        )
        if disposition and disposition not in body:
            lines.append(disposition)
        path = finding_path(f)
        line = f.get("line")
        if path and line is not None and line != "":
            cite = f"`{path}:{line}`"
            if changed_paths is not None and path not in changed_paths:
                lines.append(
                    f"_Evidence at {cite} — not on this PR's diff; included in the review body._"
                )
            else:
                lines.append(
                    f"_Evidence at {cite} — line not on the diff; included in the review body._"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def merge_summary_findings_into_review_body(
    body: str,
    summary_findings: list[dict[str, Any]],
    *,
    changed_paths: set[str] | frozenset[str] | None = None,
) -> tuple[str, int]:
    """Append summary-only findings not already in ``body``. Returns (text, appended_count)."""
    missing: list[dict[str, Any]] = []
    for i, f in enumerate(summary_findings):
        fid = finding_display_id(f, index=i)
        if not _review_body_includes_finding(body, fid):
            missing.append(f)
    if not missing:
        return body, 0
    appendix = format_summary_only_findings_md(missing, changed_paths=changed_paths)
    base = (body or "").rstrip()
    return f"{base}{appendix}", len(missing)


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
    commentable_lines: dict[str, frozenset[int]] | None = None,
) -> dict[str, int]:
    """Counts for UI: inline on diff, summary-only, and off-diff / bad-line anchors."""
    inline_list, summary_list = partition_findings_for_github_post(
        findings, changed_paths, commentable_lines
    )
    plan = build_github_post_plan(findings, changed_paths, commentable_lines)
    return {
        "inline": len(inline_list),
        "summary": len(summary_list),
        "off_diff": len(plan["off_diff_ids"]),
        "bad_line": len(plan["bad_line_ids"]),
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
