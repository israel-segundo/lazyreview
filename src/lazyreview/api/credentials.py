from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from lazyreview.services.cli_login import LOGIN_PROVIDERS, check_provider
from lazyreview.services.credentials import (
    CREDENTIAL_KEYS,
    clear_credential,
    credential_status,
    set_credential,
)

router = APIRouter(tags=["credentials"])

PROVIDERS = (
    {"id": "github", "label": "GitHub"},
    {"id": "claude", "label": "Claude"},
    {"id": "cursor", "label": "Cursor"},
    {"id": "agy", "label": "Agy"},
    {"id": "codex", "label": "Codex"},
)


class CredentialBody(BaseModel):
    value: str = Field(..., min_length=1)


class LoginCodeBody(BaseModel):
    code: str = Field(..., min_length=1)


@router.get("/credentials")
def read_credentials(request: Request):
    return {
        "credentials": credential_status(request.app.state.settings),
        "providers": [{**p, "login": p["id"] in LOGIN_PROVIDERS} for p in PROVIDERS],
    }


@router.put("/credentials/{key}")
def save_credential(key: str, body: CredentialBody, request: Request):
    if key not in CREDENTIAL_KEYS:
        raise HTTPException(404, f"unknown credential {key}")
    try:
        set_credential(request.app.state.settings, key, body.value)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return read_credentials(request)


@router.delete("/credentials/{key}")
def delete_credential(key: str, request: Request):
    if key not in CREDENTIAL_KEYS:
        raise HTTPException(404, f"unknown credential {key}")
    clear_credential(request.app.state.settings, key)
    return read_credentials(request)


@router.get("/credentials/status/{provider}")
async def provider_status(provider: str):
    if provider not in {p["id"] for p in PROVIDERS}:
        raise HTTPException(404, f"unknown provider {provider}")
    return await run_in_threadpool(check_provider, provider)


@router.post("/credentials/login/{provider}")
def start_login(provider: str, request: Request):
    try:
        session = request.app.state.logins.start(provider)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    return session.view()


@router.get("/credentials/login/session/{session_id}")
def login_session(session_id: str, request: Request):
    session = request.app.state.logins.get(session_id)
    if session is None:
        raise HTTPException(404, "login session not found")
    return session.view()


@router.post("/credentials/login/session/{session_id}/code")
def login_code(session_id: str, body: LoginCodeBody, request: Request):
    try:
        request.app.state.logins.submit_code(session_id, body.code)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return login_session(session_id, request)


@router.delete("/credentials/login/session/{session_id}")
def cancel_login(session_id: str, request: Request):
    request.app.state.logins.cancel(session_id)
    return login_session(session_id, request)
