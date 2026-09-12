from __future__ import annotations

from fastapi import APIRouter, Query, Request

from lazyreview.db import analytics_combos, analytics_dashboard

router = APIRouter(tags=["analytics"])


@router.get("/analytics/combos")
def combos(
    request: Request,
    period: str = Query(default="all"),
):
    return {"combos": analytics_combos(request.app.state.db, period=period)}


@router.get("/analytics/dashboard")
def dashboard(
    request: Request,
    period: str = Query(default="month"),
):
    return analytics_dashboard(request.app.state.db, period=period)
