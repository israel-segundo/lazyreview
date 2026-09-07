from __future__ import annotations

from fastapi import APIRouter, Request

from lazyreview.db import analytics_combos

router = APIRouter(tags=["analytics"])


@router.get("/analytics/combos")
def combos(request: Request):
    return {"combos": analytics_combos(request.app.state.db)}
