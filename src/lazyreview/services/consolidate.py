"""Consolidate findings across runs and dedup against PR comments."""

from __future__ import annotations

import re
from typing import Any

from lazyreview.services.gh import list_pr_comments

SEV_LABEL = {1: "High", 2: "Medium", 3: "Low"}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def consolidate_findings(
    findings_by_run: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    existing_comments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Dedup findings across runs and against existing PR feedback."""
    existing_bodies = [_normalize(c.get("body") or "") for c in existing_comments]
    seen_titles: set[str] = set()
    kept: list[dict[str, Any]] = []
    dropped_dup_runs = 0
    dropped_existing = 0

    # Prefer higher severity / earlier run order already provided
    flat: list[dict[str, Any]] = []
    for run, findings in findings_by_run:
        for f in findings:
            item = dict(f)
            item["_run_id"] = run["id"]
            item["_agent"] = run.get("agent")
            item["_model"] = run.get("model")
            flat.append(item)

    flat.sort(key=lambda x: (x.get("severity", 9), x.get("external_id") or ""))

    for f in flat:
        title_key = _normalize(f.get("title") or "")
        if title_key in seen_titles:
            dropped_dup_runs += 1
            continue
        # Overlap with existing PR comments: title substring or high token overlap
        matched_existing = False
        for body in existing_bodies:
            if title_key and title_key in body:
                matched_existing = True
                break
            if f.get("path") and str(f["path"]).lower() in body and title_key[:40] in body:
                matched_existing = True
                break
        if matched_existing:
            dropped_existing += 1
            f["_vs_existing"] = "duplicate"
            continue
        seen_titles.add(title_key)
        f["_vs_existing"] = "new"
        kept.append(f)

    body = _format_comment(kept)
    return {
        "findings": kept,
        "body": body,
        "dedup_summary": (
            f"Kept {len(kept)}; dropped {dropped_dup_runs} cross-run dupes; "
            f"{dropped_existing} already on PR."
        ),
        "dropped_cross_run": dropped_dup_runs,
        "dropped_existing": dropped_existing,
    }


def _format_comment(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return (
            "## lazyreview\n\nNo new findings after consolidating selected runs "
            "and deduping against existing PR feedback.\n"
        )
    lines = [
        "## lazyreview consolidated findings",
        "",
        "Human-confirmed post from selected agentic review runs.",
        "",
    ]
    for f in findings:
        sev = SEV_LABEL.get(int(f.get("severity") or 2), "Medium")
        eid = f.get("external_id") or "?"
        title = f.get("title") or "(untitled)"
        lines.append(f"### {eid} — [{sev}] {title}")
        if f.get("path"):
            loc = f["path"]
            if f.get("line"):
                loc = f"{loc}:{f['line']}"
            lines.append(f"- Location: `{loc}`")
        body = (f.get("body") or "").strip()
        if body:
            lines.append(body)
        lines.append("")
    lines.append("---")
    lines.append("_Posted via lazyreview_")
    return "\n".join(lines)


def preview_for_pr(
    owner: str,
    repo: str,
    number: int,
    runs_with_findings: list[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> dict[str, Any]:
    comments = list_pr_comments(owner, repo, number)
    return consolidate_findings(runs_with_findings, comments)
