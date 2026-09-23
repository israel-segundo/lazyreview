"""Settings helpers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from lazyreview.agents.names import (
    ALL_AGENT_NAMES,
    AgentName,
    ClaudeModelAlias,
    EffortLevel,
)
from lazyreview.db import get_all_settings, get_setting, set_setting
from lazyreview.db.schema import (
    DEFAULT_BATCH_REVIEW_DEFAULTS,
    _default_consolidator_prompt,
    _default_pr_analysis_prompt,
    _default_publish_prompt,
)
from lazyreview.services.pr_sync import (
    DEFAULT_SYNC_INTERVAL_MINUTES,
    get_pr_sync_interval_minutes,
)
from lazyreview.services.timezones import (
    DEFAULT_DISPLAY_TIMEZONE,
    DISPLAY_TIMEZONE_CHOICES,
    normalize_display_timezone,
)

ALLOWED_UNITS = {"hours", "days", "weeks"}
ALLOWED_CONSOLIDATOR_AGENTS = ALL_AGENT_NAMES
ALLOWED_PR_ANALYSIS_AGENTS = ALL_AGENT_NAMES
ALLOWED_SESSION_AGENTS = ALL_AGENT_NAMES
ALLOWED_BATCH_AGENTS = ALL_AGENT_NAMES
MAX_BATCH_DEFAULTS = 20

_DEFAULT_CONSOLIDATOR_AGENT = AgentName.CLAUDE.value
_DEFAULT_CONSOLIDATOR_MODEL = ClaudeModelAlias.HAIKU.value
_DEFAULT_CONSOLIDATOR_EFFORT = EffortLevel.LOW.value
_DEFAULT_PR_ANALYSIS_AGENT = AgentName.CLAUDE.value
_DEFAULT_PR_ANALYSIS_MODEL = ClaudeModelAlias.OPUS.value
_DEFAULT_PR_ANALYSIS_EFFORT = EffortLevel.HIGH.value
_DEFAULT_SESSION_AGENT = AgentName.CLAUDE.value
_DEFAULT_SESSION_MODEL = ClaudeModelAlias.OPUS.value
_DEFAULT_SESSION_EFFORT = EffortLevel.HIGH.value


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
    analysis_prompt = raw.get("pr_analysis_prompt") or _default_pr_analysis_prompt() or ""
    publish_prompt = raw.get("publish_prompt") or _default_publish_prompt() or ""
    batch_defaults = parse_batch_review_defaults(raw.get("batch_review_defaults"))
    display_timezone = normalize_display_timezone(raw.get("display_timezone"))
    return {
        "run_retention_value": value,
        "run_retention_unit": unit,
        "policy_summary": (
            f"keep runs and chat sessions for {value} {unit}, then delete"
        ),
        "default_summary": "Default is 1 week.",
        "pr_sync_interval_minutes": sync_minutes,
        "pr_sync_interval_default": DEFAULT_SYNC_INTERVAL_MINUTES,
        "pr_sync_last_at": get_setting(conn, "pr_sync_last_at"),
        "pr_sync_summary": (
            f"sync pending review PRs every {sync_minutes} minutes "
            f"(default {DEFAULT_SYNC_INTERVAL_MINUTES})"
        ),
        "display_timezone": display_timezone,
        "display_timezone_choices": [
            {"id": tz, "label": label} for tz, label in DISPLAY_TIMEZONE_CHOICES
        ],
        "consolidator_agent": raw.get("consolidator_agent") or _DEFAULT_CONSOLIDATOR_AGENT,
        "consolidator_model": raw.get("consolidator_model") or _DEFAULT_CONSOLIDATOR_MODEL,
        "consolidator_effort": raw.get("consolidator_effort") or _DEFAULT_CONSOLIDATOR_EFFORT,
        "consolidator_prompt": prompt,
        "consolidator_prompt_default": _default_consolidator_prompt() or "",
        "pr_analysis_agent": raw.get("pr_analysis_agent") or _DEFAULT_PR_ANALYSIS_AGENT,
        "pr_analysis_model": raw.get("pr_analysis_model") or _DEFAULT_PR_ANALYSIS_MODEL,
        "pr_analysis_effort": raw.get("pr_analysis_effort") or _DEFAULT_PR_ANALYSIS_EFFORT,
        "pr_analysis_prompt": analysis_prompt,
        "pr_analysis_prompt_default": _default_pr_analysis_prompt() or "",
        "publish_prompt": publish_prompt,
        "publish_prompt_default": _default_publish_prompt() or "",
        "batch_review_defaults": batch_defaults,
        "session_agent": raw.get("session_agent") or _DEFAULT_SESSION_AGENT,
        "session_model": raw.get("session_model") or _DEFAULT_SESSION_MODEL,
        "session_effort": raw.get("session_effort") or _DEFAULT_SESSION_EFFORT,
    }


def update_app_settings(
    conn: sqlite3.Connection,
    *,
    run_retention_value: int | None = None,
    run_retention_unit: str | None = None,
    pr_sync_interval_minutes: int | None = None,
    display_timezone: str | None = None,
    consolidator_agent: str | None = None,
    consolidator_model: str | None = None,
    consolidator_effort: str | None = None,
    consolidator_prompt: str | None = None,
    pr_analysis_agent: str | None = None,
    pr_analysis_model: str | None = None,
    pr_analysis_effort: str | None = None,
    pr_analysis_prompt: str | None = None,
    publish_prompt: str | None = None,
    batch_review_defaults: list[dict[str, Any]] | None = None,
    session_agent: str | None = None,
    session_model: str | None = None,
    session_effort: str | None = None,
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
    if display_timezone is not None:
        set_setting(conn, "display_timezone", normalize_display_timezone(display_timezone))
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
    if pr_analysis_agent is not None:
        agent = pr_analysis_agent.strip().lower()
        if agent not in ALLOWED_PR_ANALYSIS_AGENTS:
            raise ValueError(
                f"PR analysis agent must be one of {sorted(ALLOWED_PR_ANALYSIS_AGENTS)}"
            )
        set_setting(conn, "pr_analysis_agent", agent)
    if pr_analysis_model is not None:
        model = pr_analysis_model.strip()
        if not model:
            raise ValueError("PR analysis model is required")
        set_setting(conn, "pr_analysis_model", model)
    if pr_analysis_effort is not None:
        set_setting(conn, "pr_analysis_effort", pr_analysis_effort.strip())
    if pr_analysis_prompt is not None:
        set_setting(conn, "pr_analysis_prompt", pr_analysis_prompt)
    if publish_prompt is not None:
        set_setting(conn, "publish_prompt", publish_prompt)
    if batch_review_defaults is not None:
        normalized = normalize_batch_review_defaults(batch_review_defaults)
        set_setting(conn, "batch_review_defaults", json.dumps(normalized))
    if session_agent is not None:
        agent = session_agent.strip().lower()
        if agent not in ALLOWED_SESSION_AGENTS:
            raise ValueError(
                f"Session agent must be one of {sorted(ALLOWED_SESSION_AGENTS)}"
            )
        set_setting(conn, "session_agent", agent)
    if session_model is not None:
        model = session_model.strip()
        if not model:
            raise ValueError("Session model is required")
        set_setting(conn, "session_model", model)
    if session_effort is not None:
        set_setting(conn, "session_effort", session_effort.strip())
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


def get_pr_analysis_config(conn: sqlite3.Connection) -> dict[str, str]:
    view = get_settings_view(conn)
    return {
        "agent": view["pr_analysis_agent"],
        "model": view["pr_analysis_model"],
        "effort": view["pr_analysis_effort"] or "",
        "prompt": view["pr_analysis_prompt"],
    }


def get_publish_config(conn: sqlite3.Connection) -> dict[str, str]:
    view = get_settings_view(conn)
    return {
        "prompt": view["publish_prompt"],
    }


def get_display_timezone(conn: sqlite3.Connection) -> str:
    raw = get_setting(conn, "display_timezone") or DEFAULT_DISPLAY_TIMEZONE
    return normalize_display_timezone(raw)
