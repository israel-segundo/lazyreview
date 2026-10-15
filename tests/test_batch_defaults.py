"""Batch review default configurations in settings."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from lazyreview.config import Settings
from lazyreview.db.schema import DEFAULT_BATCH_REVIEW_DEFAULTS, connect, init_db
from lazyreview.services.settings import (
    normalize_batch_review_defaults,
    parse_batch_review_defaults,
    update_app_settings,
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
    )


def test_normalize_batch_review_defaults():
    rows = normalize_batch_review_defaults(
        [
            {"agent": "Agy", "model": "gemini-3.6-flash-high", "effort": None},
            {"agent": "claude", "model": "opus", "effort": "high"},
            {"agent": "cursor", "model": "composer-2.5"},
        ]
    )
    assert rows == [
        {"agent": "agy", "model": "gemini-3.6-flash-high", "effort": ""},
        {"agent": "claude", "model": "opus", "effort": "high"},
        {"agent": "cursor", "model": "composer-2.5", "effort": ""},
    ]


def test_normalize_rejects_bad_agent():
    try:
        normalize_batch_review_defaults([{"agent": "nope", "model": "x"}])
        assert False, "expected ValueError"
    except ValueError as e:
        assert "agent" in str(e).lower()


def test_parse_falls_back_on_corrupt_json():
    rows = parse_batch_review_defaults("{not-json")
    assert rows == DEFAULT_BATCH_REVIEW_DEFAULTS


def test_settings_seeds_and_saves_batch_defaults(tmp_path: Path):
    from lazyreview.main import create_app

    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    init_db(conn)
    view = update_app_settings(conn)
    assert len(view["batch_review_defaults"]) == 3
    assert view["batch_review_defaults"][1]["model"] == "opus"
    conn.close()

    with patch("lazyreview.main.get_settings", return_value=settings):
        app = create_app()
        with TestClient(app) as client:
            res = client.get("/api/settings")
            assert res.status_code == 200
            assert len(res.json()["batch_review_defaults"]) == 3

            payload = [
                {"agent": "claude", "model": "sonnet", "effort": "medium"},
                {"agent": "cursor", "model": "composer-2.5", "effort": ""},
            ]
            res = client.put("/api/settings", json={"batch_review_defaults": payload})
            assert res.status_code == 200, res.text
            saved = res.json()["batch_review_defaults"]
            assert saved == [
                {"agent": "claude", "model": "sonnet", "effort": "medium"},
                {"agent": "cursor", "model": "composer-2.5", "effort": ""},
            ]

            # Empty list is allowed.
            res = client.put("/api/settings", json={"batch_review_defaults": []})
            assert res.status_code == 200
            assert res.json()["batch_review_defaults"] == []


def test_init_db_persists_default_json(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'batch_review_defaults'"
    ).fetchone()
    assert row is not None
    data = json.loads(row["value"])
    assert data[0]["agent"] == "agy"
    conn.close()
