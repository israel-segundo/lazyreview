"""Registry of agent adapters."""

from __future__ import annotations

from lazyreview.agents.agy import AgyAdapter
from lazyreview.agents.claude import ClaudeAdapter
from lazyreview.agents.cursor import CursorAdapter

_ADAPTERS = {
    "cursor": CursorAdapter(),
    "claude": ClaudeAdapter(),
    "agy": AgyAdapter(),
}


def get_adapter(name: str):
    try:
        return _ADAPTERS[name]
    except KeyError as e:
        raise KeyError(f"Unknown agent: {name}") from e


def list_adapters():
    return list(_ADAPTERS.values())
