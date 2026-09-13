"""Re-run an existing review with the same configuration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from lazyreview.config import Settings
from lazyreview.db import (
    create_consolidation,
    create_run,
    list_findings,
    replace_findings,
    update_run,
    upsert_pr,
)
from lazyreview.db.schema import connect, init_db



def _seed_succeeded_analysis(conn, pr):
    """Satisfy the PR-analysis gate for API tests."""
    from lazyreview.db import create_pr_analysis, update_pr_analysis

    row = create_pr_analysis(
        conn,
        pr_id=int(pr["id"]),
        head_sha=pr["head_sha"],
        agent="claude",
        model="haiku",
        effort="low",
    )
    update_pr_analysis(
        conn,
        int(row["id"]),
        status="succeeded",
        brief_markdown="# ok",
        finished_at="2026-01-01T00:00:00+00:00",
    )
    return row

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
        max_concurrent_jobs=2,
        service_name="lazyreview",
        unit_name="lazyreview.service",
    )


def test_rerun_replaces_results_and_requeues(tmp_path: Path):
    from lazyreview.main import create_app

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=1,
        url="https://example.com/o/r/pull/1",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="newsha",
    )
    _seed_succeeded_analysis(conn, pr)
    run = create_run(
        conn,
        pr_id=int(pr["id"]),
        agent="claude",
        model="opus",
        effort="high",
        skill="pr-code-review-batch",
        head_sha="oldsha",
        workdir_path=str(settings.runs_dir / "99"),
    )
    work = Path(run["workdir_path"])
    work.mkdir(parents=True, exist_ok=True)
    (work / "stdout.log").write_text("old", encoding="utf-8")
    (work / "repo").mkdir()
    (work / "repo" / "x").write_text("y", encoding="utf-8")
    replace_findings(
        conn,
        int(run["id"]),
        [{"external_id": "F1", "severity": 1, "title": "t", "body": "b"}],
    )
    update_run(
        conn,
        int(run["id"]),
        status="succeeded",
        findings_sev1=1,
        total_tokens=10,
        cost_usd=0.5,
        consolidated=1,
        error_message=None,
    )
    conn.close()

    fake = MagicMock()
    fake.available.return_value = True

    with patch("lazyreview.main.get_settings", return_value=settings):
        app = create_app()
        with TestClient(app) as client:
            with (
                patch("lazyreview.api.runs.get_adapter", return_value=fake),
                patch.object(app.state.jobs, "enqueue") as enqueue,
            ):
                res = client.post(f"/api/runs/{run['id']}/rerun")
            assert res.status_code == 200, res.text
            data = res.json()
            assert data["status"] == "queued"
            assert data["agent"] == "claude"
            assert data["model"] == "opus"
            assert data["effort"] == "high"
            assert data["head_sha"] == "newsha"
            assert data["findings_sev1"] == 0
            assert data["total_tokens"] is None
            assert data["consolidated"] == 0
            enqueue.assert_called_once_with(int(run["id"]))

    conn = connect(settings.db_path)
    assert list_findings(conn, int(run["id"])) == []
    assert not (work / "stdout.log").exists()
    assert not (work / "repo").exists()
    conn.close()


def test_rerun_blocked_while_in_active_consolidation(tmp_path: Path):
    from lazyreview.main import create_app

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=2,
        url="https://example.com/o/r/pull/2",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="abc",
    )
    _seed_succeeded_analysis(conn, pr)
    run = create_run(
        conn,
        pr_id=int(pr["id"]),
        agent="claude",
        model="haiku",
        effort="low",
        skill="pr-code-review-batch",
        head_sha="abc",
        workdir_path=str(tmp_path / "w"),
    )
    update_run(conn, int(run["id"]), status="succeeded")
    create_consolidation(
        conn,
        pr_id=int(pr["id"]),
        head_sha="abc",
        source_run_ids=[int(run["id"])],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c"),
        status="dormant",
    )
    conn.close()

    fake = MagicMock()
    fake.available.return_value = True
    with patch("lazyreview.main.get_settings", return_value=settings):
        app = create_app()
        with TestClient(app) as client:
            with patch("lazyreview.api.runs.get_adapter", return_value=fake):
                res = client.post(f"/api/runs/{run['id']}/rerun")
            assert res.status_code == 400
            assert "consolidation" in res.json()["detail"].lower()
