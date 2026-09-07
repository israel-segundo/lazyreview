"""Async LLM consolidations: schema, eligibility, success/post flags."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from lazyreview.config import Settings
from lazyreview.db import (
    active_consolidation_run_ids,
    create_consolidation,
    get_consolidation,
    mark_runs_consolidated,
    run_is_selectable,
    update_consolidation,
    upsert_pr,
)
from lazyreview.db.schema import SCHEMA_VERSION, _default_consolidator_prompt, connect, init_db
from lazyreview.services.consolidation_jobs import (
    ConsolidationRunner,
    render_consolidator_prompt,
)


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
        service_name="lazyreview",
        unit_name="lazyreview.service",
    )


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _pr_and_run(conn, tmp_path: Path, *, status="succeeded", head="abc"):
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=1,
        url="https://example.com/o/r/pull/1",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha=head,
    )
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path)
        VALUES (?, 'claude', 'opus', ?, ?, ?)
        """,
        (pr["id"], status, head, str(tmp_path / "w")),
    )
    conn.commit()
    run_id = int(cur.lastrowid)
    run = dict(conn.execute("SELECT * FROM review_runs WHERE id = ?", (run_id,)).fetchone())
    return pr, run


def test_schema_v6_consolidations_and_settings(tmp_path: Path):
    conn = _conn(tmp_path)
    ver = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert ver == SCHEMA_VERSION == 7
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(consolidations)").fetchall()
    }
    assert "feedback_provided" in cols
    assert "source_run_ids" in cols
    settings = {
        r["key"]: r["value"]
        for r in conn.execute("SELECT key, value FROM settings").fetchall()
    }
    assert settings.get("consolidator_agent") == "claude"
    assert settings.get("consolidator_model") == "haiku"
    assert "consolidator_prompt" in settings
    prompt = settings["consolidator_prompt"]
    assert "{body_path}" in prompt
    assert "dedup" in prompt.lower()


def test_prompt_seed_contains_posted_comment_rules():
    text = _default_consolidator_prompt()
    assert "Title" in text
    assert "Coverage table" in text or "Agent / Model / Effort" in text
    assert "Total after deduping" in text
    assert "orchestrated agentic" in text.lower()
    assert "{source_runs_label}" in text
    assert "{body_path}" in text
    assert "{findings_json_path}" in text
    assert "{pr_url}" in text
    assert "gh api" in text
    assert "lazyreview" not in text.lower()
    assert "Already addressed by other reviewers" in text
    assert "S1" in text and "S2" in text
    assert "Duplicate" in text or "existing PR" in text or "already covered" in text.lower()


def test_selectable_unchanged_for_consolidated_obsolete(tmp_path: Path):
    conn = _conn(tmp_path)
    _, run = _pr_and_run(conn, tmp_path)
    assert run_is_selectable(run)
    mark_runs_consolidated(conn, [run["id"]])
    run = dict(conn.execute("SELECT * FROM review_runs WHERE id = ?", (run["id"],)).fetchone())
    assert not run_is_selectable(run)


def test_create_rejects_ineligible_runs(tmp_path: Path):
    from lazyreview.main import create_app

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=9,
        url="https://example.com/o/r/pull/9",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="xyz",
    )
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path)
        VALUES (?, 'claude', 'opus', 'failed', 'xyz', ?)
        """,
        (pr["id"], str(tmp_path / "wf")),
    )
    conn.commit()
    bad_id = int(cur.lastrowid)
    conn.close()

    with patch("lazyreview.main.get_settings", return_value=settings):
        app = create_app()
        with TestClient(app) as client:
            res = client.post(
                f"/api/prs/{pr['id']}/consolidations",
                json={"run_ids": [bad_id]},
            )
            assert res.status_code == 400
            assert "not eligible" in res.json()["detail"].lower()


def test_worker_marks_runs_consolidated_only_on_success(tmp_path: Path):
    conn = _conn(tmp_path)
    pr, run = _pr_and_run(conn, tmp_path)
    work = tmp_path / "consol" / "1"
    work.mkdir(parents=True)
    row = create_consolidation(
        conn,
        pr_id=pr["id"],
        head_sha="abc",
        source_run_ids=[run["id"]],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(work),
    )
    cid = int(row["id"])

    class _S:
        data_dir = tmp_path / "data"
        db_path = tmp_path / "t.db"
        max_concurrent_jobs = 1
        job_timeout_seconds = 30

        def ensure_dirs(self):
            self.data_dir.mkdir(parents=True, exist_ok=True)

    _S.data_dir.mkdir(parents=True, exist_ok=True)
    runner = ConsolidationRunner(_S(), conn)  # type: ignore[arg-type]

    with (
        patch.object(runner, "_run_process", return_value=1),
        patch(
            "lazyreview.services.consolidation_jobs.list_pr_comments",
            return_value=[],
        ),
        patch(
            "lazyreview.services.consolidation_jobs.build_consolidate_command",
            return_value=MagicMock(argv=["true"], stdin_text=None),
        ),
        patch(
            "lazyreview.services.consolidation_jobs.get_adapter",
            return_value=MagicMock(
                parse_result=MagicMock(
                    return_value=MagicMock(
                        input_tokens=1,
                        output_tokens=1,
                        total_tokens=2,
                        cost_usd=0.0,
                    )
                )
            ),
        ),
    ):
        runner._execute_with_conn(cid, conn)

    run_row = dict(conn.execute("SELECT * FROM review_runs WHERE id = ?", (run["id"],)).fetchone())
    assert int(run_row["consolidated"]) == 0
    assert get_consolidation(conn, cid)["status"] == "failed"

    update_consolidation(conn, cid, status="queued", error_message=None, body=None)
    body = work / "body.md"
    findings = work / "findings.json"

    def _fake_process(*_a, **_k):
        body.write_text("## Review: ok\n\n> disclosure\n", encoding="utf-8")
        findings.write_text("[]", encoding="utf-8")
        return 0

    with (
        patch.object(runner, "_run_process", side_effect=_fake_process),
        patch(
            "lazyreview.services.consolidation_jobs.list_pr_comments",
            return_value=[],
        ),
        patch(
            "lazyreview.services.consolidation_jobs.build_consolidate_command",
            return_value=MagicMock(argv=["true"], stdin_text=None),
        ),
        patch(
            "lazyreview.services.consolidation_jobs.get_adapter",
            return_value=MagicMock(
                parse_result=MagicMock(
                    return_value=MagicMock(
                        input_tokens=10,
                        output_tokens=5,
                        total_tokens=15,
                        cost_usd=0.01,
                    )
                )
            ),
        ),
    ):
        runner._execute_with_conn(cid, conn)

    run_row = dict(conn.execute("SELECT * FROM review_runs WHERE id = ?", (run["id"],)).fetchone())
    assert int(run_row["consolidated"]) == 1
    c = get_consolidation(conn, cid)
    assert c["status"] == "succeeded"
    assert "Review" in (c.get("body") or "")


def test_post_sets_feedback_provided_and_refuses_double(tmp_path: Path):
    from lazyreview.main import create_app

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=3,
        url="https://example.com/o/r/pull/3",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="ddd",
    )
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path, consolidated)
        VALUES (?, 'claude', 'opus', 'succeeded', 'ddd', ?, 1)
        """,
        (pr["id"], str(tmp_path / "w3")),
    )
    conn.commit()
    run_id = int(cur.lastrowid)
    row = create_consolidation(
        conn,
        pr_id=pr["id"],
        head_sha="ddd",
        source_run_ids=[run_id],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c3"),
    )
    update_consolidation(
        conn,
        int(row["id"]),
        status="succeeded",
        body="## Review: done\n",
    )
    cid = int(row["id"])
    conn.close()

    with patch("lazyreview.main.get_settings", return_value=settings):
        app = create_app()
        with TestClient(app) as client:
            with patch(
                "lazyreview.api.consolidations.post_pr_comment",
                return_value={"output": "https://gh/comment/1"},
            ):
                res = client.post(
                    f"/api/consolidations/{cid}/post",
                    json={"action": "comment"},
                )
            assert res.status_code == 200, res.text
            data = res.json()
            assert int(data["consolidation"]["feedback_provided"]) == 1

            res2 = client.post(
                f"/api/consolidations/{cid}/post",
                json={"action": "comment"},
            )
            assert res2.status_code == 400
            assert "already" in res2.json()["detail"].lower()


def test_regenerate_requeues_with_current_settings(tmp_path: Path):
    from lazyreview.main import create_app

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=4,
        url="https://example.com/o/r/pull/4",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="eee",
    )
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path, consolidated)
        VALUES (?, 'claude', 'opus', 'succeeded', 'eee', ?, 1)
        """,
        (pr["id"], str(tmp_path / "w4")),
    )
    conn.commit()
    run_id = int(cur.lastrowid)
    row = create_consolidation(
        conn,
        pr_id=pr["id"],
        head_sha="eee",
        source_run_ids=[run_id],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c4"),
    )
    cid = int(row["id"])
    update_consolidation(
        conn,
        cid,
        status="succeeded",
        body="## old\n",
        input_tokens=9,
        output_tokens=3,
        total_tokens=12,
        cost_usd=0.02,
        error_message=None,
    )
    from lazyreview.db import set_setting

    set_setting(conn, "consolidator_agent", "claude")
    set_setting(conn, "consolidator_model", "sonnet")
    set_setting(conn, "consolidator_effort", "high")
    conn.commit()
    conn.close()

    fake_adapter = MagicMock()
    fake_adapter.available.return_value = True

    with patch("lazyreview.main.get_settings", return_value=settings):
        app = create_app()
        with TestClient(app) as client:
            with (
                patch("lazyreview.api.consolidations.get_adapter", return_value=fake_adapter),
                patch.object(app.state.consolidations, "enqueue"),
            ):
                res = client.post(f"/api/consolidations/{cid}/regenerate")
            assert res.status_code == 200, res.text
            data = res.json()
            assert data["status"] == "queued"
            assert data["model"] == "sonnet"
            assert data["effort"] == "high"
            assert data["body"] is None
            assert data["input_tokens"] is None
            assert data["source_run_ids"] == [run_id]

            # Still queued (enqueue mocked) → refuse
            with (
                patch("lazyreview.api.consolidations.get_adapter", return_value=fake_adapter),
                patch.object(app.state.consolidations, "enqueue"),
            ):
                res_busy = client.post(f"/api/consolidations/{cid}/regenerate")
            assert res_busy.status_code == 400
            assert "queued" in res_busy.json()["detail"].lower() or "cancel" in res_busy.json()["detail"].lower()


def test_regenerate_refuses_after_feedback(tmp_path: Path):
    from lazyreview.main import create_app

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=5,
        url="https://example.com/o/r/pull/5",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="fff",
    )
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path, consolidated)
        VALUES (?, 'claude', 'opus', 'succeeded', 'fff', ?, 1)
        """,
        (pr["id"], str(tmp_path / "w5")),
    )
    conn.commit()
    run_id = int(cur.lastrowid)
    row = create_consolidation(
        conn,
        pr_id=pr["id"],
        head_sha="fff",
        source_run_ids=[run_id],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c5"),
    )
    cid = int(row["id"])
    update_consolidation(conn, cid, status="succeeded", body="## done\n", feedback_provided=1)
    conn.close()

    with patch("lazyreview.main.get_settings", return_value=settings):
        app = create_app()
        with TestClient(app) as client:
            res = client.post(f"/api/consolidations/{cid}/regenerate")
            assert res.status_code == 400
            assert "feedback" in res.json()["detail"].lower()


def test_active_consolidation_run_ids(tmp_path: Path):
    conn = _conn(tmp_path)
    pr, run = _pr_and_run(conn, tmp_path)
    create_consolidation(
        conn,
        pr_id=pr["id"],
        head_sha="abc",
        source_run_ids=[run["id"]],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "cx"),
        status="dormant",
    )
    from lazyreview.db import in_flight_consolidation_run_ids

    assert run["id"] in active_consolidation_run_ids(conn, pr["id"])
    assert run["id"] not in in_flight_consolidation_run_ids(conn, pr["id"])
    update_consolidation(conn, 1, status="queued")
    assert run["id"] in in_flight_consolidation_run_ids(conn, pr["id"])
    update_consolidation(conn, 1, status="succeeded")
    assert run["id"] not in active_consolidation_run_ids(conn, pr["id"])
    assert run["id"] not in in_flight_consolidation_run_ids(conn, pr["id"])


def test_render_prompt_placeholders():
    out = render_consolidator_prompt(
        "body={body_path} fj={findings_json_path} src={source_runs_label} old={model_label}",
        body_path=Path("/tmp/body.md"),
        findings_json_path=Path("/tmp/f.json"),
        source_runs_label="agy/gemini-high, claude/opus/high",
    )
    assert "/tmp/body.md" in out
    assert "agy/gemini-high, claude/opus/high" in out
    assert "{model_label}" not in out
    assert "{source_runs_label}" not in out
