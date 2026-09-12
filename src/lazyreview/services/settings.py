"""Settings helpers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from lazyreview.db import get_all_settings, get_setting, set_setting
from lazyreview.db.schema import (
    DEFAULT_BATCH_REVIEW_DEFAULTS,
    _default_consolidator_prompt,
)
from lazyreview.services.pr_sync import (
    DEFAULT_SYNC_INTERVAL_MINUTES,
    get_pr_sync_interval_minutes,
)

ALLOWED_UNITS = {"hours", "days", "weeks"}
ALLOWED_CONSOLIDATOR_AGENTS = {"claude", "cursor", "agy"}
ALLOWED_BATCH_AGENTS = {"claude", "cursor", "agy"}
MAX_BATCH_DEFAULTS = 20


def normalize_batch_review_defaults(raw: Any) -> list[dict[str, str]]:
    """Validate and normalize a list of {agent, model, effort} run configs."""
    if raw is None:
        return [dict(x) for x in DEFAULT_BATCH_REVIEW_DEFAULTS]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"batch_review_defaults must be valid JSON: {e}") from e
    if not isinstance(raw, list):
        raise ValueError("batch_review_defaults must be a list")
    if len(raw) > MAX_BATCH_DEFAULTS:
        raise ValueError(f"At most {MAX_BATCH_DEFAULTS} batch review defaults allowed")
    out: list[dict[str, str]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"batch_review_defaults[{i}] must be an object")
        agent = str(item.get("agent") or "").strip().lower()
        model = str(item.get("model") or "").strip()
        effort_raw = item.get("effort")
        effort = "" if effort_raw is None else str(effort_raw).strip()
        if not agent:
            raise ValueError(f"batch_review_defaults[{i}].agent is required")
        if agent not in ALLOWED_BATCH_AGENTS:
            raise ValueError(
                f"batch_review_defaults[{i}].agent must be one of "
                f"{sorted(ALLOWED_BATCH_AGENTS)}"
            )
        if not model:
            raise ValueError(f"batch_review_defaults[{i}].model is required")
        out.append({"agent": agent, "model": model, "effort": effort})
    return out


def parse_batch_review_defaults(raw: str | None) -> list[dict[str, str]]:
    if raw is None:
        return [dict(x) for x in DEFAULT_BATCH_REVIEW_DEFAULTS]
    try:
        return normalize_batch_review_defaults(raw)
    except ValueError:
        return [dict(x) for x in DEFAULT_BATCH_REVIEW_DEFAULTS]


def get_settings_view(conn: sqlite3.Connection) -> dict[str, Any]:
    raw = get_all_settings(conn)
    value = int(raw.get("run_retention_value", "1") or "1")
    unit = raw.get("run_retention_unit", "weeks") or "weeks"
    sync_minutes = get_pr_sync_interval_minutes(conn)
    prompt = raw.get("consolidator_prompt") or _default_consolidator_prompt() or ""
    batch_defaults = parse_batch_review_defaults(raw.get("batch_review_defaults"))
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
        "batch_review_defaults": batch_defaults,
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
    batch_review_defaults: list[dict[str, Any]] | None = None,
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
    if batch_review_defaults is not None:
        normalized = normalize_batch_review_defaults(batch_review_defaults)
        set_setting(conn, "batch_review_defaults", json.dumps(normalized))
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
