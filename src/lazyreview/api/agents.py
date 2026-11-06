from __future__ import annotations

from fastapi import APIRouter, Request

from lazyreview.services.agent_eligibility import list_agent_catalog

router = APIRouter(tags=["agents"])


@router.get("/agents")
def agents(request: Request):
    """List agents with PATH / auth / enabled / selectable flags for dropdowns."""
    catalog = list_agent_catalog(
        request.app.state.settings,
        request.app.state.db,
        probe_cli=True,
    )
    return {"agents": catalog}
