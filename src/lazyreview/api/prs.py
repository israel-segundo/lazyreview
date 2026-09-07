from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from lazyreview.db import get_pr, list_prs, list_runs_for_pr, upsert_pr
from lazyreview.services.gh import GhError, view_pr
from lazyreview.services.pr_sync import sync_pending_review_prs

router = APIRouter(tags=["prs"])


class ResolvePrBody(BaseModel):
    url: str = Field(..., min_length=3)


@router.post("/prs/resolve")
def resolve_pr(body: ResolvePrBody, request: Request):
    try:
        data = view_pr(body.url)
    except GhError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    pr = upsert_pr(
        request.app.state.db,
        owner=data["owner"],
        repo=data["repo"],
        number=data["number"],
        url=data["url"],
        title=data["title"],
        author=data.get("author") or "",
        base_ref=data["base_ref"],
        head_ref=data["head_ref"],
        head_sha=data["head_sha"],
        state=data.get("state") or "open",
        is_draft=bool(data.get("is_draft")),
        additions=data.get("additions"),
        deletions=data.get("deletions"),
        changed_files=data.get("changed_files"),
    )
    return pr


@router.post("/prs/sync-pending")
def sync_pending(request: Request):
    try:
        result = sync_pending_review_prs(request.app.state.db)
    except GhError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


@router.get("/prs")
def prs(request: Request):
    return {"prs": list_prs(request.app.state.db)}


@router.get("/prs/{pr_id}")
def pr_detail(pr_id: int, request: Request):
    pr = get_pr(request.app.state.db, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    runs = list_runs_for_pr(request.app.state.db, pr_id)
    return {"pr": pr, "runs": runs}
