"""PR analysis HTTP API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from lazyreview.db import (
    get_current_pr_analysis,
    get_pr,
    get_pr_analysis,
    list_pr_analyses,
    list_pr_events,
)
from lazyreview.services.analysis_enqueue import ensure_analysis_for_pr

router = APIRouter(tags=["analyses"])


@router.get("/prs/{pr_id}/analysis")
def get_pr_analysis_endpoint(pr_id: int, request: Request):
    conn = request.app.state.db
    pr = get_pr(conn, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    analysis = get_current_pr_analysis(conn, pr_id)
    return {
        "pr_id": pr_id,
        "head_sha": pr.get("head_sha"),
        "analysis": analysis,
        "history": list_pr_analyses(conn, pr_id),
        "events": list_pr_events(conn, pr_id),
    }


@router.post("/prs/{pr_id}/analysis")
def start_pr_analysis(pr_id: int, request: Request):
    """Enqueue analysis for the current head (manual trigger / backfill)."""
    conn = request.app.state.db
    settings = request.app.state.settings
    pr = get_pr(conn, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    if not (pr.get("head_sha") or "").strip():
        raise HTTPException(status_code=400, detail="PR has no head_sha")
    runner = getattr(request.app.state, "analyses", None)
    row = ensure_analysis_for_pr(conn, settings, runner, pr_id, force=False)
    if not row:
        raise HTTPException(status_code=400, detail="Could not enqueue analysis")
    return row


@router.post("/prs/{pr_id}/analysis/retry")
def retry_pr_analysis(pr_id: int, request: Request):
    conn = request.app.state.db
    settings = request.app.state.settings
    pr = get_pr(conn, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    runner = getattr(request.app.state, "analyses", None)
    row = ensure_analysis_for_pr(conn, settings, runner, pr_id, force=True)
    if not row:
        raise HTTPException(status_code=400, detail="Could not enqueue analysis")
    return row


@router.post("/analyses/{analysis_id}/cancel")
def cancel_analysis(analysis_id: int, request: Request):
    runner = getattr(request.app.state, "analyses", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="Analysis runner not available")
    try:
        return runner.cancel(analysis_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Analysis not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: int, request: Request):
    row = get_pr_analysis(request.app.state.db, analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return row
