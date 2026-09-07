from __future__ import annotations

from fastapi import APIRouter, Request

from lazyreview import __version__
from lazyreview.services.gh import auth_status

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request):
    settings = request.app.state.settings
    gh = auth_status()
    return {
        "ok": True,
        "version": __version__,
        "config": settings.as_public_dict(),
        "gh": gh,
    }
