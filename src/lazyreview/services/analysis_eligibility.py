"""Decide whether a PR should burn tokens on orientation analysis."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def in_merge_queue(pr: dict[str, Any]) -> bool:
    """True when the stored PR flag says GitHub has this PR in a merge queue."""
    return _flag(pr.get("in_merge_queue"))


def analysis_ineligible_reason(
    pr: dict[str, Any],
    *,
    allow_draft: bool = False,
) -> str | None:
    """
    Return a human reason to skip/cancel analysis, or None if eligible.

    Auto-kick (allow_draft=False) also skips drafts. Manual run/retry may pass
    allow_draft=True but still refuses merged/closed/merge-queue PRs.
    """
    state = (pr.get("state") or "").lower()
    if state in ("merged", "closed"):
        return f"PR is {state}"
    if not allow_draft and _flag(pr.get("is_draft")):
        return "PR is a draft"
    if in_merge_queue(pr):
        return "PR is in the merge queue"
    return None


def review_blocked_reason(pr: dict[str, Any]) -> str | None:
    """
    Reason to refuse review runs, consolidations, or GitHub submit, or None.

    Drafts may still be reviewed; merge-queue / closed / merged may not.
    """
    return analysis_ineligible_reason(pr, allow_draft=True)


def refresh_pr_analysis_flags(pr: dict[str, Any]) -> dict[str, Any]:
    """
    Best-effort live refresh of state / draft / merge flags onto a PR dict.

    On GitHub errors, returns the original ``pr`` unchanged.
    """
    url = (pr.get("url") or "").strip()
    if not url:
        return pr
    try:
        from lazyreview.services.gh import view_pr

        live = view_pr(url)
    except Exception as e:
        logger.warning("Could not refresh PR flags for analysis gate: %s", e)
        return pr
    out = dict(pr)
    if live.get("state"):
        out["state"] = live["state"]
    if "is_draft" in live:
        out["is_draft"] = live["is_draft"]
    if "in_merge_queue" in live:
        out["in_merge_queue"] = live["in_merge_queue"]
    if "has_merge_conflicts" in live:
        out["has_merge_conflicts"] = live["has_merge_conflicts"]
    return out
