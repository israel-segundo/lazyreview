"""Settings helpers."""

from __future__ import annotations

import sqlite3
from typing import Any

from lazyreview.db import get_all_settings, get_setting, set_setting
from lazyreview.db.schema import _default_consolidator_prompt
from lazyreview.services.pr_sync import (
    DEFAULT_SYNC_INTERVAL_MINUTES,
    get_pr_sync_interval_minutes,
)

ALLOWED_UNITS = {"hours", "days", "weeks"}
ALLOWED_CONSOLIDATOR_AGENTS = {"claude", "cursor", "agy"}


def get_settings_view(conn: sqlite3.Connection) -> dict[str, Any]:
    raw = get_all_settings(conn)
    value = int(raw.get("run_retention_value", "1") or "1")
    unit = raw.get("run_retention_unit", "weeks") or "weeks"
    sync_minutes = get_pr_sync_interval_minutes(conn)
    prompt = raw.get("consolidator_prompt") or _default_consolidator_prompt() or ""
    return {
        "run_retention_value": value,
        "run_retention_unit": unit,
        "policy_summary": f"keep runs for {value} {unit}, then delete",
        "default_summary": "Default is 1 week.",
        "pr_sync_interval_minutes": sync_minutes,
        "pr_sync_interval_default": DEFAULT_SYNC_INTERVAL_MINUTES,
        "pr_sync_last_at": get_setting(conn, "pr_sync_last_at"),
        "pr_sync_summary": (
            f"sync pending review PRs every {sync_minutes} minutes "
            f"(default {DEFAULT_SYNC_INTERVAL_MINUTES})"
        ),
        "consolidator_agent": raw.get("consolidator_agent") or "claude",
        "consolidator_model": raw.get("consolidator_model") or "haiku",
        "consolidator_effort": raw.get("consolidator_effort") or "low",
        "consolidator_prompt": prompt,
        "consolidator_prompt_default": _default_consolidator_prompt() or "",
    }


def update_app_settings(
    conn: sqlite3.Connection,
    *,
    run_retention_value: int | None = None,
    run_retention_unit: str | None = None,
    pr_sync_interval_minutes: int | None = None,
    consolidator_agent: str | None = None,
    consolidator_model: str | None = None,
    consolidator_effort: str | None = None,
    consolidator_prompt: str | None = None,
) -> dict[str, Any]:
    if run_retention_value is not None:
        if run_retention_value < 1:
            raise ValueError("Keep for must be >= 1")
        set_setting(conn, "run_retention_value", str(run_retention_value))
    if run_retention_unit is not None:
        unit = run_retention_unit.lower()
        if unit not in ALLOWED_UNITS:
            raise ValueError(f"Unit must be one of {sorted(ALLOWED_UNITS)}")
        set_setting(conn, "run_retention_unit", unit)
    if pr_sync_interval_minutes is not None:
        if pr_sync_interval_minutes < 1:
            raise ValueError("PR sync interval must be >= 1 minute")
        set_setting(conn, "pr_sync_interval_minutes", str(pr_sync_interval_minutes))
    if consolidator_agent is not None:
        agent = consolidator_agent.strip().lower()
        if agent not in ALLOWED_CONSOLIDATOR_AGENTS:
            raise ValueError(
                f"Consolidator agent must be one of {sorted(ALLOWED_CONSOLIDATOR_AGENTS)}"
            )
        set_setting(conn, "consolidator_agent", agent)
    if consolidator_model is not None:
        model = consolidator_model.strip()
        if not model:
            raise ValueError("Consolidator model is required")
        set_setting(conn, "consolidator_model", model)
    if consolidator_effort is not None:
        set_setting(conn, "consolidator_effort", consolidator_effort.strip())
    if consolidator_prompt is not None:
        set_setting(conn, "consolidator_prompt", consolidator_prompt)
    conn.commit()
    return get_settings_view(conn)


def update_retention(
    conn: sqlite3.Connection, *, value: int, unit: str
) -> dict[str, Any]:
    return update_app_settings(
        conn, run_retention_value=value, run_retention_unit=unit
    )


def get_consolidator_config(conn: sqlite3.Connection) -> dict[str, str]:
    view = get_settings_view(conn)
    return {
        "agent": view["consolidator_agent"],
        "model": view["consolidator_model"],
        "effort": view["consolidator_effort"] or "",
        "prompt": view["consolidator_prompt"],
    }
