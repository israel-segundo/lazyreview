"""In-app notifications API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from lazyreview.db import (
    NOTIFICATION_KIND_ANALYSIS,
    NOTIFICATION_KIND_CONSOLIDATION,
    NOTIFICATION_KIND_RUN,
    count_unread_notifications,
    get_notification,
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    mark_notification_read_for_consolidation,
    mark_notifications_read_for_pr,
)

router = APIRouter(tags=["notifications"])

_PAGE_SIZES = (15, 25, 50, 100)
_DROPDOWN_LIMIT = 15

_KIND_ALIASES = {
    "analysis": NOTIFICATION_KIND_ANALYSIS,
    "analysis_finished": NOTIFICATION_KIND_ANALYSIS,
    "run": NOTIFICATION_KIND_RUN,
    "run_finished": NOTIFICATION_KIND_RUN,
    "consolidation": NOTIFICATION_KIND_CONSOLIDATION,
    "consolidation_finished": NOTIFICATION_KIND_CONSOLIDATION,
    # PR page tabs → notification kinds
    "overview": NOTIFICATION_KIND_ANALYSIS,
    "runs": NOTIFICATION_KIND_RUN,
    "consolidations": NOTIFICATION_KIND_CONSOLIDATION,
    "findings": NOTIFICATION_KIND_CONSOLIDATION,
}


class MarkPrReadBody(BaseModel):
    kind: str | None = None


def _normalize_kind(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    key = str(raw).strip().lower()
    kind = _KIND_ALIASES.get(key)
    if kind is None:
        raise HTTPException(status_code=400, detail=f"Unknown notification kind: {raw}")
    return kind


@router.get("/notifications")
def get_notifications(
    request: Request,
    limit: int = Query(default=_DROPDOWN_LIMIT, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    unread_only: bool = Query(default=False),
):
    conn = request.app.state.db
    result = list_notifications(
        conn, limit=limit, offset=offset, unread_only=unread_only
    )
    return {
        **result,
        "unread_count": count_unread_notifications(conn),
    }


@router.get("/notifications/unread-count")
def get_unread_count(request: Request):
    return {"unread_count": count_unread_notifications(request.app.state.db)}


@router.post("/notifications/{notification_id}/read")
def post_mark_read(request: Request, notification_id: int):
    row = mark_notification_read(request.app.state.db, notification_id)
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {
        "notification": row,
        "unread_count": count_unread_notifications(request.app.state.db),
    }


@router.post("/notifications/read-all")
def post_mark_all_read(request: Request):
    updated = mark_all_notifications_read(request.app.state.db)
    return {
        "updated": updated,
        "unread_count": count_unread_notifications(request.app.state.db),
    }


@router.post("/notifications/read-pr/{pr_id}")
def post_mark_pr_read(request: Request, pr_id: int, body: MarkPrReadBody | None = None):
    kind = _normalize_kind(body.kind if body else None)
    updated = mark_notifications_read_for_pr(request.app.state.db, pr_id, kind=kind)
    return {
        "updated": updated,
        "unread_count": count_unread_notifications(request.app.state.db),
    }


@router.post("/notifications/read-consolidation/{consolidation_id}")
def post_mark_consolidation_read(request: Request, consolidation_id: int):
    updated = mark_notification_read_for_consolidation(
        request.app.state.db, consolidation_id
    )
    return {
        "updated": updated,
        "unread_count": count_unread_notifications(request.app.state.db),
    }


@router.get("/notifications/{notification_id}")
def get_one(request: Request, notification_id: int):
    row = get_notification(request.app.state.db, notification_id)
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    return row
