"""Tests for interactive chat sessions."""

from __future__ import annotations

from pathlib import Path

from lazyreview.db.schema import SCHEMA_VERSION, connect, init_db
from lazyreview.services.session_jobs import findings_to_body_md, parse_structured_trailer
from lazyreview.services.session_safety import preflight_user_message


def test_schema_has_chat_tables(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = connect(db)
    init_db(conn)
    ver = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert ver == SCHEMA_VERSION == 23
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "chat_sessions" in tables
    assert "chat_turns" in tables
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()
    }
    assert "working_findings_json" in cols
    assert "working_recommended_action" in cols
    conn.close()


def test_preflight_blocks_destructive():
    block, _ = preflight_user_message("please rm -rf ~/")
    assert block is not None
    block2, warn = preflight_user_message("approve PR #98 please")
    assert block2 is None
    assert warn is not None


def test_parse_structured_trailer():
    text = (
        "Dropped C2.\n\n"
        "```json\n"
        '{"working_findings":[{"id":"C1","severity":1,"title":"x","body":"y"}],'
        '"recommended_action":"request_changes","recommended_action_rationale":"kept S1"}\n'
        "```"
    )
    body, data = parse_structured_trailer(text)
    assert "Dropped C2" in body
    assert data is not None
    assert data["recommended_action"] == "request_changes"
    assert len(data["working_findings"]) == 1


def test_findings_to_body_md():
    md = findings_to_body_md(
        pr={"title": "Add widget", "number": 1},
        findings=[{"id": "C1", "severity": 1, "title": "Broken", "body": "Because."}],
        recommended_action="request_changes",
        rationale="S1 remains",
    )
    assert "Add widget" in md
    assert "Broken" in md
    assert "request_changes" in md
