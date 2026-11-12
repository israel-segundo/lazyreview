"""Global activity feed API."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from lazyreview.db import (
    ACTIVITY_STATUS_FILTERS,
    count_global_activity_running,
    list_global_activity,
)

router = APIRouter(tags=["activity"])

_PAGE_SIZES = (25, 50, 100)


@router.get("/activity")
def get_activity(
    request: Request,
    status: str = Query(default="all"),
    running_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=500),
):
    if page_size not in _PAGE_SIZES:
        page_size = 25
    raw_status = (status or "all").strip().lower()
    if raw_status not in ACTIVITY_STATUS_FILTERS:
        raw_status = "all"
    # Legacy running_only only applies when status is the default "all".
    use_running_only = bool(running_only) and raw_status == "all"
    status_filter: str | None = None if use_running_only else raw_status
    conn = request.app.state.db
    offset = (page - 1) * page_size
    result = list_global_activity(
        conn,
        limit=page_size,
        offset=offset,
        status_filter=status_filter,
        running_only=use_running_only,
    )
    total = int(result["total"])
    pages = max(1, (total + page_size - 1) // page_size) if total else 1
    if page > pages:
        page = pages
        offset = (page - 1) * page_size
        result = list_global_activity(
            conn,
            limit=page_size,
            offset=offset,
            status_filter=status_filter,
            running_only=use_running_only,
        )
        total = int(result["total"])
        pages = max(1, (total + page_size - 1) // page_size) if total else 1
    return {
        "items": result["items"],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "running_count": count_global_activity_running(conn),
        "status": raw_status,
        "page_sizes": list(_PAGE_SIZES),
    }
