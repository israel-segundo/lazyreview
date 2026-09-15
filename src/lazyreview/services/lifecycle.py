"""Process-global handles so DB upserts can cancel runners / enqueue analyses."""

from __future__ import annotations

from typing import Any

_jobs: Any | None = None
_consolidations: Any | None = None
_analyses: Any | None = None
_settings: Any | None = None


def bind(
    *,
    settings: Any,
    jobs: Any,
    consolidations: Any,
    analyses: Any,
) -> None:
    global _jobs, _consolidations, _analyses, _settings
    _settings = settings
    _jobs = jobs
    _consolidations = consolidations
    _analyses = analyses


def unbind() -> None:
    global _jobs, _consolidations, _analyses, _settings
    _jobs = None
    _consolidations = None
    _analyses = None
    _settings = None


def on_pr_head_changed(conn: Any, pr_id: int) -> None:
    """Cancel in-flight work for a new head. Does not auto-start PR analysis."""
    if _jobs is not None and hasattr(_jobs, "cancel_for_pr"):
        try:
            _jobs.cancel_for_pr(pr_id)
        except Exception:
            pass
    if _consolidations is not None and hasattr(_consolidations, "cancel_for_pr"):
        try:
            _consolidations.cancel_for_pr(pr_id)
        except Exception:
            pass
    if _analyses is not None:
        try:
            _analyses.cancel_for_pr(pr_id)
        except Exception:
            pass


def on_pr_upserted(conn: Any, pr: dict[str, Any], *, is_new: bool = False) -> None:
    """Enqueue analysis only when a PR is first added; prune workspace when terminal."""
    from lazyreview.services.pr_workspace import queue_workspace_deletion

    state = (pr.get("state") or "").lower()
    if state in ("closed", "merged"):
        try:
            queue_workspace_deletion(conn, int(pr["id"]))
        except Exception:
            pass
        return

    # Analysis is kicked off once at create time. Later head moves do not re-run it
    # (manual Run analysis / Retry still available on the PR page).
    if not is_new:
        return
    if not int(pr.get("review_requested") or 0):
        return
    if _settings is None:
        return
    from lazyreview.services.analysis_enqueue import ensure_analysis_for_pr

    try:
        ensure_analysis_for_pr(conn, _settings, _analyses, int(pr["id"]), force=False)
    except Exception:
        pass
