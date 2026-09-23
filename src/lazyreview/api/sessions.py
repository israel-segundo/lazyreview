"""Interactive chat session API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from lazyreview.db import (
    get_chat_session,
    get_consolidation,
    get_pr,
    list_chat_turns,
)

router = APIRouter(tags=["sessions"])


class CreateExploreBody(BaseModel):
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    force_new: bool = False


class CreateChallengeBody(BaseModel):
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    force_new: bool = False


class PatchSessionBody(BaseModel):
    agent: str | None = None
    model: str | None = None
    effort: str | None = None


class MessageBody(BaseModel):
    text: str = Field(min_length=1)


def _runner(request: Request) -> Any:
    runner = getattr(request.app.state, "session_runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="Session runner not available")
    return runner


@router.post("/prs/{pr_id}/sessions")
def create_explore_session(pr_id: int, body: CreateExploreBody, request: Request):
    conn = request.app.state.db
    if not get_pr(conn, pr_id):
        raise HTTPException(status_code=404, detail="PR not found")
    try:
        row = _runner(request).create_explore_session(
            pr_id,
            agent=body.agent,
            model=body.model,
            effort=body.effort,
            force_new=body.force_new,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return row


@router.post("/consolidations/{consolidation_id}/sessions")
def create_challenge_session(
    consolidation_id: int, body: CreateChallengeBody, request: Request
):
    conn = request.app.state.db
    if not get_consolidation(conn, consolidation_id):
        raise HTTPException(status_code=404, detail="Consolidation not found")
    try:
        row = _runner(request).create_challenge_session(
            consolidation_id,
            agent=body.agent,
            model=body.model,
            effort=body.effort,
            force_new=body.force_new,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return row


@router.get("/sessions/{session_id}")
def get_session(session_id: int, request: Request):
    row = get_chat_session(request.app.state.db, session_id)
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


@router.patch("/sessions/{session_id}")
def patch_session(session_id: int, body: PatchSessionBody, request: Request):
    try:
        return _runner(request).patch_session(
            session_id, agent=body.agent, model=body.model, effort=body.effort
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/sessions/{session_id}/turns")
def get_turns(session_id: int, request: Request, after_id: int = 0):
    if not get_chat_session(request.app.state.db, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"turns": list_chat_turns(request.app.state.db, session_id, after_id=after_id)}


@router.post("/sessions/{session_id}/messages")
def post_message(session_id: int, body: MessageBody, request: Request):
    try:
        return _runner(request).send_message(session_id, body.text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post("/sessions/{session_id}/done")
def finish_session(session_id: int, request: Request):
    try:
        return _runner(request).finish_session(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
