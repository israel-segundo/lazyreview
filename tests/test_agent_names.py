"""Canonical agent / effort / Claude-alias identifiers."""

from __future__ import annotations

from lazyreview.agents.names import (
    ALL_AGENT_NAMES,
    AgentName,
    ClaudeModelAlias,
    EffortLevel,
    agent_key,
    normalize_agent_name,
)


def test_agent_name_values():
    assert set(ALL_AGENT_NAMES) == {"claude", "cursor", "agy"}
    assert AgentName.CLAUDE == "claude"
    assert AgentName.CURSOR.value == "cursor"


def test_normalize_agent_name_aliases():
    assert normalize_agent_name("Claude") is AgentName.CLAUDE
    assert normalize_agent_name("agent") is AgentName.CURSOR
    assert normalize_agent_name("nope") is None
    assert agent_key("agent") == AgentName.CURSOR.value
    assert agent_key("agy") == AgentName.AGY.value


def test_effort_and_claude_aliases():
    assert EffortLevel.HIGH.value == "high"
    assert ClaudeModelAlias.OPUS.value == "opus"
    assert list(EffortLevel)[0] == EffortLevel.LOW
