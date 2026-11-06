"""Cursor adapter model catalog filtering."""

from __future__ import annotations

import subprocess

from lazyreview.agents.cursor import CursorAdapter, _is_allowed_cursor_model


def test_is_allowed_cursor_model_composer_and_grok_only():
    assert _is_allowed_cursor_model("composer-2.5")
    assert _is_allowed_cursor_model("composer-2.5-fast")
    assert _is_allowed_cursor_model("grok-4.7-high")
    assert _is_allowed_cursor_model("cursor-grok-4.6-high-fast")
    assert not _is_allowed_cursor_model("auto")
    assert not _is_allowed_cursor_model("gpt-5.3-codex")
    assert not _is_allowed_cursor_model("claude-opus-5-5-high")
    assert not _is_allowed_cursor_model("gemini-3.7-flash-high")


def test_cursor_list_models_keeps_composer_and_grok_only(monkeypatch):
    stdout = """Available models

auto - Auto
gpt-5.3-codex - Codex 5.3
composer-2.5 - Composer 2.5
claude-opus-5-5-high - Claude Opus 5.5
cursor-grok-4.6-high - Grok 4.6
grok-4.7-high - Grok 4.7 High
composer-2.5-fast - Composer 2.5 Fast
gemini-3.7-flash-high - Gemini 3.7 Flash
"""
    monkeypatch.setattr(
        "lazyreview.agents.cursor.which_binary", lambda _name: "/usr/bin/agent"
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout=stdout, stderr=""
        ),
    )
    models = CursorAdapter().list_models()
    assert [(m.id, m.label, m.efforts) for m in models] == [
        ("composer-2.5", "Composer 2.5", []),
        ("cursor-grok-4.6-high", "Grok 4.6", []),
        ("grok-4.7-high", "Grok 4.7 High", []),
        ("composer-2.5-fast", "Composer 2.5 Fast", []),
    ]
