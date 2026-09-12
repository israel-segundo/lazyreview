"""Canonical agent / model-alias / effort identifiers.

Adapters, settings validation, seed defaults, and the UI all import from here
so agent (and known effort / Claude alias) strings are not scattered as literals.
Dynamic model ids from CLIs (agy / cursor catalogs) stay free-form.
"""

from __future__ import annotations

from enum import StrEnum


class AgentName(StrEnum):
    CLAUDE = "claude"
    CURSOR = "cursor"
    AGY = "agy"


# CLI / legacy names that should canonicalize to an AgentName for UI keys.
AGENT_ALIASES: dict[str, AgentName] = {
    "agent": AgentName.CURSOR,  # Cursor ships as the `agent` binary
}


class EffortLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    ULTRA = "ultra"


class ClaudeModelAlias(StrEnum):
    """Built-in Claude Code model aliases (not the full live catalog)."""

    SONNET = "sonnet"
    OPUS = "opus"
    HAIKU = "haiku"


# Seed / fallback model ids used when settings are first initialized.
# Cursor and Agy catalogs are dynamic; these are only defaults.
DEFAULT_AGY_MODEL = "gemini-3.6-flash-high"
DEFAULT_CURSOR_MODEL = "composer-2.5"

DEFAULT_EFFORTS: tuple[str, ...] = tuple(e.value for e in EffortLevel)

ALL_AGENT_NAMES: frozenset[str] = frozenset(a.value for a in AgentName)

AGENT_COLORS: dict[AgentName, str] = {
    AgentName.CLAUDE: "#be123c",
    AgentName.CURSOR: "#0369a1",
    AgentName.AGY: "#15803d",
}


def normalize_agent_name(raw: str | None) -> AgentName | None:
    """Return the canonical AgentName, or None if unknown."""
    key = str(raw or "").strip().lower()
    if not key:
        return None
    try:
        return AgentName(key)
    except ValueError:
        return AGENT_ALIASES.get(key)


def agent_key(raw: str | None) -> str:
    """Stable key for icons / colors (alias-aware); falls back to lowercased raw."""
    name = normalize_agent_name(raw)
    if name is not None:
        return name.value
    return str(raw or "").strip().lower()


def agents_js_dict() -> dict[str, str]:
    """Uppercase member → value map for embedding in browser JS."""
    return {a.name: a.value for a in AgentName}


def efforts_js_dict() -> dict[str, str]:
    return {e.name: e.value for e in EffortLevel}


def claude_models_js_dict() -> dict[str, str]:
    return {m.name: m.value for m in ClaudeModelAlias}


def agent_colors_js_dict() -> dict[str, str]:
    return {a.value: color for a, color in AGENT_COLORS.items()}
