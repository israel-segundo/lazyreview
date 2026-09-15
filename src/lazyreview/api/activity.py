"""Global activity feed API."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from lazyreview.db import count_global_activity_running, list_global_activity

router = APIRouter(tags=["activity"])

_PAGE_SIZES = (25, 50, 100)


@router.get("/activity")
def get_activity(
    request: Request,
    running_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=500),
):
    if page_size not in _PAGE_SIZES:
        page_size = 25
    conn = request.app.state.db
    offset = (page - 1) * page_size
    result = list_global_activity(
        conn, limit=page_size, offset=offset, running_only=running_only
    )
    total = int(result["total"])
    pages = max(1, (total + page_size - 1) // page_size) if total else 1
    if page > pages:
        page = pages
        offset = (page - 1) * page_size
        result = list_global_activity(
            conn, limit=page_size, offset=offset, running_only=running_only
        )
    return {
        "items": result["items"],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "running_count": count_global_activity_running(conn),
        "running_only": running_only,
        "page_sizes": list(_PAGE_SIZES),
    }
