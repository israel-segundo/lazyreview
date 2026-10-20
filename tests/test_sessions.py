"""Tests for interactive chat sessions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from lazyreview.config import Settings
from lazyreview.db import (
    create_consolidation,
    get_chat_session,
    get_consolidation,
    update_chat_session,
    update_consolidation,
    upsert_pr,
)
from lazyreview.db.schema import SCHEMA_VERSION, connect, init_db
from lazyreview.services.session_jobs import (
    SessionRunner,
    findings_to_body_md,
    parse_structured_trailer,
)
from lazyreview.services.session_safety import preflight_user_message


def _settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Settings(
        host="127.0.0.1",
        port=3334,
        data_dir=data,
        runs_dir=data / "runs",
        db_path=data / "lazyreview.db",
        config_path=tmp_path / "config.toml",
        job_timeout_seconds=60,
        retention_sweep_seconds=3600,
        max_concurrent_jobs=1,
    )


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _pr_and_consol(conn, tmp_path: Path):
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=1,
        url="https://example.com/o/r/pull/1",
        title="Add widget",
        base_ref="main",
        head_ref="feat",
        head_sha="abc123",
    )
    work = tmp_path / "consol" / "1"
    work.mkdir(parents=True)
    row = create_consolidation(
        conn,
        pr_id=pr["id"],
        head_sha="abc123",
        source_run_ids=[],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(work),
    )
    update_consolidation(
        conn,
        int(row["id"]),
        status="succeeded",
        body="## Review: rich report\n\nKeep this tone.\n",
        findings_json='[{"id":"C1","severity":1,"title":"Broken","body":"Because."}]',
        recommended_action="request_changes",
        recommended_action_rationale="S1 remains",
        publish_body="## Review\n\nOld draft.\n",
        ready_to_post=1,
    )
    return pr, get_consolidation(conn, int(row["id"])), work


def test_schema_has_chat_tables(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = connect(db)
    init_db(conn)
    ver = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert ver == SCHEMA_VERSION == 26
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
    assert "working_body_md" in cols
    assert "working_publish_guidance" in cols
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


def test_parse_structured_trailer_body_and_guidance():
    text = (
        "Softer tone.\n\n"
        "```json\n"
        '{"working_findings":[{"id":"C1","severity":2,"title":"x","body":"y"}],'
        '"recommended_action":"comment",'
        '"recommended_action_rationale":"downgraded",'
        '"working_body_md":"## Soft report\\n\\nHello.\\n",'
        '"publish_guidance":"Softer colleague tone; mention flaky CI."}\n'
        "```"
    )
    body, data = parse_structured_trailer(text)
    assert "Softer tone" in body
    assert data is not None
    assert "Soft report" in data["working_body_md"]
    assert "flaky CI" in data["publish_guidance"]


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


def test_create_challenge_seeds_body_and_clears_publish(tmp_path: Path):
    conn = _conn(tmp_path)
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    _, consol, work = _pr_and_consol(conn, tmp_path)
    (work / "publish-body.md").write_text("stale\n", encoding="utf-8")

    runner = SessionRunner(settings, conn)
    session = runner.create_challenge_session(int(consol["id"]))

    assert session["mode"] == "challenge"
    assert "rich report" in (session.get("working_body_md") or "")
    assert session.get("working_recommended_action") == "request_changes"
    assert len(session.get("working_findings") or []) == 1

    refreshed = get_consolidation(conn, int(consol["id"]))
    assert int(refreshed["ready_to_post"] or 0) == 0
    assert not (refreshed.get("publish_body") or "").strip()
    assert not (work / "publish-body.md").exists()


def test_finish_session_preserves_body_and_writes_guidance(tmp_path: Path):
    conn = _conn(tmp_path)
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    _, consol, work = _pr_and_consol(conn, tmp_path)

    runner = SessionRunner(settings, conn)
    enqueued: list[int] = []

    class _ConsolRunner:
        def enqueue_publish(self, cid: int) -> None:
            enqueued.append(int(cid))

    runner.bind_consolidation_runner(_ConsolRunner())
    session = runner.create_challenge_session(int(consol["id"]))
    sid = int(session["id"])

    edited_body = "## Review: challenged\n\nSofter stance. Keep C1.\n"
    update_chat_session(
        conn,
        sid,
        working_findings_json=(
            '[{"id":"C1","severity":2,"title":"Broken","body":"Still real."}]'
        ),
        working_recommended_action="comment",
        working_recommended_action_rationale="downgraded after challenge",
        working_body_md=edited_body,
        working_publish_guidance="Softer tone; mention flaky CI in summary.",
    )

    result = runner.finish_session(sid)
    assert result["consolidation_id"] == int(consol["id"])
    assert enqueued == [int(consol["id"])]

    done = get_chat_session(conn, sid)
    assert done["status"] == "done"

    refreshed = get_consolidation(conn, int(consol["id"]))
    assert "challenged" in (refreshed.get("body") or "")
    assert "Softer stance" in (refreshed.get("body") or "")
    assert refreshed.get("recommended_action") == "comment"
    assert int(refreshed["ready_to_post"] or 0) == 0
    assert not (refreshed.get("publish_body") or "").strip()
    findings = refreshed.get("findings") or []
    assert len(findings) == 1
    assert int(findings[0].get("severity") or 0) == 2

    assert (work / "body.md").read_text(encoding="utf-8") == edited_body
    guidance = (work / "publish-guidance.md").read_text(encoding="utf-8")
    assert "flaky CI" in guidance
    assert not (work / "publish-body.md").exists()


def test_finish_session_falls_back_to_findings_template(tmp_path: Path):
    conn = _conn(tmp_path)
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    _, consol, work = _pr_and_consol(conn, tmp_path)

    runner = SessionRunner(settings, conn)
    runner.bind_consolidation_runner(MagicMock())
    session = runner.create_challenge_session(int(consol["id"]))
    sid = int(session["id"])
    update_chat_session(
        conn,
        sid,
        working_body_md="",
        working_publish_guidance="",
        working_findings_json=(
            '[{"id":"C1","severity":1,"title":"Broken","body":"Because."}]'
        ),
        working_recommended_action="request_changes",
        working_recommended_action_rationale="still blocked",
    )
    # Stale guidance from a prior Done should be removed.
    (work / "publish-guidance.md").write_text("old\n", encoding="utf-8")

    runner.finish_session(sid)
    body = (work / "body.md").read_text(encoding="utf-8")
    assert "Challenge" in body or "request_changes" in body
    assert "Broken" in body
    assert not (work / "publish-guidance.md").exists()


def test_round_persists_body_and_guidance_from_trailer(tmp_path: Path):
    conn = _conn(tmp_path)
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    _, consol, _work = _pr_and_consol(conn, tmp_path)
    runner = SessionRunner(settings, conn)
    session = runner.create_challenge_session(int(consol["id"]))
    sid = int(session["id"])

    assistant = (
        "Updated.\n\n"
        "```json\n"
        '{"working_findings":[{"id":"C1","severity":3,"title":"Nit","body":"ok"}],'
        '"recommended_action":"approve",'
        '"recommended_action_rationale":"nit only",'
        '"working_body_md":"## Edited body\\n",'
        '"publish_guidance":"Keep it short."}\n'
        "```"
    )
    wt = tmp_path / "wt"
    wt.mkdir()
    cm = MagicMock()
    cm.__enter__.return_value = wt
    cm.__exit__.return_value = None
    with (
        patch.object(runner, "_run_proc", return_value=0),
        patch.object(runner, "_build_command", return_value=(["true"], None)),
        patch("lazyreview.services.session_jobs.hold_worktree", return_value=cm),
        patch(
            "lazyreview.services.session_jobs._extract_assistant_text",
            return_value=assistant,
        ),
    ):
        result = runner.send_message(sid, "soften tone and keep it short")

    updated = result["session"]
    assert "Edited body" in (updated.get("working_body_md") or "")
    assert updated.get("working_publish_guidance") == "Keep it short."
    assert updated.get("working_recommended_action") == "approve"
    assert int((updated.get("working_findings") or [{}])[0].get("severity") or 0) == 3
