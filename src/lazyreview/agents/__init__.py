"""Registry of agent adapters."""

from __future__ import annotations

from lazyreview.agents.agy import AgyAdapter
from lazyreview.agents.claude import ClaudeAdapter
from lazyreview.agents.codex import CodexAdapter
from lazyreview.agents.cursor import CursorAdapter
from lazyreview.agents.names import (
    AGENT_COLORS,
    ALL_AGENT_NAMES,
    AgentName,
    ClaudeModelAlias,
    EffortLevel,
    agent_key,
    normalize_agent_name,
)

_ADAPTERS: dict[str, object] = {
    AgentName.CURSOR.value: CursorAdapter(),
    AgentName.CLAUDE.value: ClaudeAdapter(),
    AgentName.AGY.value: AgyAdapter(),
    AgentName.CODEX.value: CodexAdapter(),
}


def get_adapter(name: str):
    key = normalize_agent_name(name)
    lookup = key.value if key is not None else str(name or "").strip().lower()
    try:
        return _ADAPTERS[lookup]
    except KeyError as e:
        raise KeyError(f"Unknown agent: {name}") from e


def list_adapters():
    return list(_ADAPTERS.values())


__all__ = [
    "AGENT_COLORS",
    "ALL_AGENT_NAMES",
    "AgentName",
    "ClaudeModelAlias",
    "EffortLevel",
    "agent_key",
    "get_adapter",
    "list_adapters",
    "normalize_agent_name",
]
