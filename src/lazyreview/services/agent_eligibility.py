"""Which agents may appear in dropdowns and be used for new work.

An agent is **selectable** when all of:
- CLI binary is on PATH (``available``)
- authenticated (stored/env credential for its provider, or CLI login OK)
- enabled in Settings (``agent_enabled`` JSON; default true)
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from lazyreview.agents import get_adapter, list_adapters
from lazyreview.agents.names import ALL_AGENT_NAMES, normalize_agent_name
from lazyreview.config import Settings
from lazyreview.db import get_setting, set_setting
from lazyreview.services.cli_login import check_provider
from lazyreview.services.credentials import credential_status

# Provider id used by credentials / cli_login ↔ AgentName value.
AGENT_PROVIDERS = frozenset({"claude", "cursor", "agy", "codex"})

DEFAULT_AGENT_ENABLED: dict[str, bool] = {name: True for name in sorted(ALL_AGENT_NAMES)}


def normalize_agent_enabled(raw: Any) -> dict[str, bool]:
    """Return a full map for every known agent; unknown keys dropped; missing → True."""
    out = dict(DEFAULT_AGENT_ENABLED)
    if raw is None:
        return out
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return out
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"agent_enabled must be valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("agent_enabled must be an object")
    for key, value in data.items():
        name = str(key or "").strip().lower()
        if name not in ALL_AGENT_NAMES:
            continue
        if isinstance(value, bool):
            out[name] = value
        elif isinstance(value, (int, float)):
            out[name] = bool(value)
        elif isinstance(value, str):
            out[name] = value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            out[name] = bool(value)
    return out


def get_agent_enabled_map(conn: sqlite3.Connection) -> dict[str, bool]:
    raw = get_setting(conn, "agent_enabled")
    try:
        return normalize_agent_enabled(raw)
    except ValueError:
        return dict(DEFAULT_AGENT_ENABLED)


def set_agent_enabled_map(
    conn: sqlite3.Connection, updates: dict[str, Any]
) -> dict[str, bool]:
    current = get_agent_enabled_map(conn)
    merged = dict(current)
    for key, value in (updates or {}).items():
        name = str(key or "").strip().lower()
        if name not in ALL_AGENT_NAMES:
            raise ValueError(f"Unknown agent {key!r}")
        if isinstance(value, bool):
            merged[name] = value
        elif isinstance(value, (int, float)):
            merged[name] = bool(value)
        elif isinstance(value, str):
            merged[name] = value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            merged[name] = bool(value)
    set_setting(conn, "agent_enabled", json.dumps(merged, sort_keys=True))
    conn.commit()
    return merged


def provider_has_credential(settings: Settings, provider: str) -> bool:
    for row in credential_status(settings):
        if row.get("provider") == provider and row.get("source"):
            return True
    return False


def provider_authenticated(
    settings: Settings,
    provider: str,
    *,
    probe_cli: bool = True,
) -> tuple[bool, str | None]:
    """Return (ok, detail). Prefer stored/env credentials; optionally probe the CLI."""
    if provider not in AGENT_PROVIDERS and provider != "github":
        return False, f"unknown provider {provider}"
    if provider_has_credential(settings, provider):
        return True, "credential configured"
    if not probe_cli:
        return False, "no credential configured"
    try:
        result = check_provider(provider)
    except ValueError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001 — surface probe failure as not auth
        return False, str(e)
    return bool(result.get("ok")), str(result.get("detail") or "") or None


def agent_eligibility(
    settings: Settings,
    conn: sqlite3.Connection,
    agent: str,
    *,
    probe_cli: bool = True,
) -> dict[str, Any]:
    """Compute availability / auth / enabled / selectable for one agent."""
    canonical = normalize_agent_name(agent)
    name = canonical.value if canonical is not None else str(agent or "").strip().lower()
    if name not in ALL_AGENT_NAMES:
        return {
            "name": name,
            "available": False,
            "authenticated": False,
            "enabled": False,
            "selectable": False,
            "reason": f"unknown agent {agent!r}",
        }

    try:
        adapter = get_adapter(name)
        available = bool(adapter.available())
        display_name = adapter.display_name
        binary = adapter.binary
    except Exception:
        available = False
        display_name = name
        binary = name

    enabled_map = get_agent_enabled_map(conn)
    enabled = bool(enabled_map.get(name, True))
    authenticated, auth_detail = provider_authenticated(
        settings, name, probe_cli=probe_cli
    )

    reasons: list[str] = []
    if not available:
        reasons.append(f"{binary} not found on PATH")
    if not authenticated:
        reasons.append(auth_detail or "not authenticated")
    if not enabled:
        reasons.append("disabled in Settings → Credentials")

    selectable = available and authenticated and enabled
    return {
        "name": name,
        "display_name": display_name,
        "binary": binary,
        "available": available,
        "authenticated": authenticated,
        "enabled": enabled,
        "selectable": selectable,
        "auth_detail": auth_detail,
        "reason": None if selectable else "; ".join(reasons) or "not selectable",
    }


def list_agent_catalog(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    probe_cli: bool = True,
) -> list[dict[str, Any]]:
    """Full agent catalog for ``GET /api/agents`` (includes models when available)."""
    enabled_map = get_agent_enabled_map(conn)
    out: list[dict[str, Any]] = []
    for adapter in list_adapters():
        name = adapter.name
        available = bool(adapter.available())
        enabled = bool(enabled_map.get(name, True))
        authenticated, auth_detail = provider_authenticated(
            settings, name, probe_cli=probe_cli
        )
        models: list[dict[str, Any]] = []
        err: str | None = None
        if available:
            try:
                models = [
                    {"id": m.id, "label": m.label, "efforts": m.efforts}
                    for m in adapter.list_models()
                ]
            except Exception as e:
                models = []
                err = str(e)
        else:
            err = f"{adapter.binary} not found on PATH"

        reasons: list[str] = []
        if not available:
            reasons.append(err or f"{adapter.binary} missing")
        if not authenticated:
            reasons.append(auth_detail or "not authenticated")
        if not enabled:
            reasons.append("disabled")
        selectable = available and authenticated and enabled

        out.append(
            {
                "name": name,
                "display_name": adapter.display_name,
                "binary": adapter.binary,
                "available": available,
                "authenticated": authenticated,
                "enabled": enabled,
                "selectable": selectable,
                "auth_detail": auth_detail,
                "reason": None if selectable else "; ".join(reasons),
                "efforts": adapter.effort_options(),
                "models": models,
                "error": err if not available else None,
            }
        )
    return out


def require_selectable_agent(
    settings: Settings,
    conn: sqlite3.Connection,
    agent: str,
    *,
    probe_cli: bool = True,
) -> dict[str, Any]:
    """Return eligibility or raise ``ValueError`` with a UI-ready message."""
    info = agent_eligibility(settings, conn, agent, probe_cli=probe_cli)
    if info.get("selectable"):
        return info
    reason = info.get("reason") or "not selectable"
    raise ValueError(f"Agent {info.get('name') or agent} is not selectable: {reason}")
