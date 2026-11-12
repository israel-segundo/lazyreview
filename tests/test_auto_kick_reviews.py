"""Auto-kick batch reviews after analysis succeeds."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from lazyreview.agents.base import ModelInfo
from lazyreview.config import Settings
from lazyreview.db import create_pr_analysis, list_runs_for_pr, update_pr_analysis, upsert_pr
from lazyreview.db.schema import connect, init_db
from lazyreview.services.batch_review import (
    maybe_auto_kick_reviews,
    randomize_batch_run_configs,
)
from lazyreview.services.settings import (
    get_auto_kick_reviews,
    get_batch_randomize_models_effort,
    get_settings_view,
    update_app_settings,
)


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


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
    )


def _seed_succeeded_analysis(conn, pr):
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


def test_auto_kick_and_randomize_settings_round_trip(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        view = get_settings_view(conn)
        assert view["auto_kick_reviews"] is False
        assert view["batch_randomize_models_effort"] is False

        updated = update_app_settings(
            conn,
            auto_kick_reviews=True,
            batch_randomize_models_effort=True,
        )
        assert updated["auto_kick_reviews"] is True
        assert updated["batch_randomize_models_effort"] is True
        assert get_auto_kick_reviews(conn) is True
        assert get_batch_randomize_models_effort(conn) is True

        updated = update_app_settings(
            conn,
            auto_kick_reviews=False,
            batch_randomize_models_effort=False,
        )
        assert updated["auto_kick_reviews"] is False
        assert updated["batch_randomize_models_effort"] is False
    finally:
        conn.close()


def test_randomize_batch_run_configs_keeps_agent(tmp_path: Path):
    fake = MagicMock()
    fake.list_models.return_value = [
        ModelInfo(id="sonnet", label="Sonnet", efforts=["low", "high"]),
        ModelInfo(id="opus", label="Opus", efforts=["medium"]),
    ]
    with (
        patch("lazyreview.services.batch_review.get_adapter", return_value=fake),
        patch("lazyreview.services.batch_review.random.choice", side_effect=lambda seq: seq[0]),
    ):
        out = randomize_batch_run_configs(
            [{"agent": "claude", "model": "haiku", "effort": "low"}]
        )
    assert out == [{"agent": "claude", "model": "sonnet", "effort": "low"}]


def test_maybe_auto_kick_skips_when_disabled(tmp_path: Path):
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
        head_sha="aaa",
    )
    _seed_succeeded_analysis(conn, pr)
    jobs = MagicMock()
    assert maybe_auto_kick_reviews(conn, settings, jobs, int(pr["id"])) is None
    jobs.enqueue.assert_not_called()
    conn.close()


def test_maybe_auto_kick_starts_batch_with_defaults(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    update_app_settings(
        conn,
        auto_kick_reviews=True,
        batch_review_defaults=[
            {"agent": "claude", "model": "opus", "effort": "high"},
            {"agent": "claude", "model": "haiku", "effort": "low"},
        ],
    )
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=2,
        url="https://example.com/o/r/pull/2",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="bbb",
    )
    _seed_succeeded_analysis(conn, pr)
    jobs = MagicMock()
    fake = MagicMock()
    fake.available.return_value = True
    with (
        patch("lazyreview.services.batch_review.get_adapter", return_value=fake),
        patch(
            "lazyreview.services.batch_review.require_selectable_agent",
            return_value={},
        ),
    ):
        result = maybe_auto_kick_reviews(conn, settings, jobs, int(pr["id"]))
    assert result is not None
    assert result["count"] == 2
    assert result["consolidation"]["status"] == "dormant"
    assert {r["model"] for r in result["runs"]} == {"opus", "haiku"}
    assert jobs.enqueue.call_count == 2

    # Second kick for same head is a no-op.
    with (
        patch("lazyreview.services.batch_review.get_adapter", return_value=fake),
        patch(
            "lazyreview.services.batch_review.require_selectable_agent",
            return_value={},
        ),
    ):
        assert maybe_auto_kick_reviews(conn, settings, jobs, int(pr["id"])) is None
    conn.close()


def test_maybe_auto_kick_applies_randomize(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    update_app_settings(
        conn,
        auto_kick_reviews=True,
        batch_randomize_models_effort=True,
        batch_review_defaults=[
            {"agent": "claude", "model": "haiku", "effort": "low"},
        ],
    )
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=3,
        url="https://example.com/o/r/pull/3",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="ccc",
    )
    _seed_succeeded_analysis(conn, pr)
    jobs = MagicMock()
    fake = MagicMock()
    fake.available.return_value = True
    fake.list_models.return_value = [
        ModelInfo(id="opus", label="Opus", efforts=["high"]),
    ]
    with (
        patch("lazyreview.services.batch_review.get_adapter", return_value=fake),
        patch(
            "lazyreview.services.batch_review.require_selectable_agent",
            return_value={},
        ),
    ):
        result = maybe_auto_kick_reviews(conn, settings, jobs, int(pr["id"]))
    assert result is not None
    assert result["runs"][0]["model"] == "opus"
    assert result["runs"][0]["effort"] == "high"
    runs = list_runs_for_pr(conn, int(pr["id"]))
    assert len(runs) == 1
    assert runs[0]["model"] == "opus"
    conn.close()


def test_on_analysis_succeeded_triggers_auto_kick(tmp_path: Path):
    from lazyreview.services import lifecycle

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    update_app_settings(
        conn,
        auto_kick_reviews=True,
        batch_review_defaults=[
            {"agent": "claude", "model": "opus", "effort": "high"},
        ],
    )
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=4,
        url="https://example.com/o/r/pull/4",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="ddd",
    )
    analysis = _seed_succeeded_analysis(conn, pr)
    jobs = MagicMock()
    fake = MagicMock()
    fake.available.return_value = True
    lifecycle.bind(settings=settings, jobs=jobs, consolidations=MagicMock(), analyses=MagicMock())
    try:
        with (
            patch("lazyreview.services.batch_review.get_adapter", return_value=fake),
            patch(
                "lazyreview.services.batch_review.require_selectable_agent",
                return_value={},
            ),
        ):
            lifecycle.on_analysis_succeeded(conn, analysis)
        assert jobs.enqueue.call_count == 1
        assert len(list_runs_for_pr(conn, int(pr["id"]))) == 1
    finally:
        lifecycle.unbind()
        conn.close()
