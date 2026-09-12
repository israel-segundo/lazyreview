from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from lazyreview.services.retention import purge_old_runs
from lazyreview.services.settings import get_settings_view, update_app_settings

router = APIRouter(tags=["settings"])


class BatchReviewDefault(BaseModel):
    agent: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    effort: str | None = ""


class SettingsBody(BaseModel):
    run_retention_value: int | None = Field(default=None, ge=1)
    run_retention_unit: str | None = None
    pr_sync_interval_minutes: int | None = Field(default=None, ge=1)
    consolidator_agent: str | None = None
    consolidator_model: str | None = None
    consolidator_effort: str | None = None
    consolidator_prompt: str | None = None
    batch_review_defaults: list[BatchReviewDefault] | None = None


@router.get("/settings")
def read_settings(request: Request):
    return get_settings_view(request.app.state.db)


@router.put("/settings")
def write_settings(body: SettingsBody, request: Request):
    try:
        batch = None
        if body.batch_review_defaults is not None:
            batch = [row.model_dump() for row in body.batch_review_defaults]
        view = update_app_settings(
            request.app.state.db,
            run_retention_value=body.run_retention_value,
            run_retention_unit=body.run_retention_unit,
            pr_sync_interval_minutes=body.pr_sync_interval_minutes,
            consolidator_agent=body.consolidator_agent,
            consolidator_model=body.consolidator_model,
            consolidator_effort=body.consolidator_effort,
            consolidator_prompt=body.consolidator_prompt,
            batch_review_defaults=batch,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Wake syncer so a shorter interval takes effect without waiting
    syncer = getattr(request.app.state, "pr_syncer", None)
    if syncer is not None and body.pr_sync_interval_minutes is not None:
        syncer.wake()
    return view


@router.post("/settings/purge-runs")
def purge_runs(request: Request):
    result = purge_old_runs(request.app.state.db, request.app.state.settings)
    return result
