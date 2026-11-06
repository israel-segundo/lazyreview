"""Agent dropdown eligibility: PATH + auth + enabled."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from lazyreview.config import Settings
from lazyreview.db.schema import SCHEMA_VERSION, connect, init_db
from lazyreview.services import credentials
from lazyreview.services.agent_eligibility import (
    get_agent_enabled_map,
    list_agent_catalog,
    normalize_agent_enabled,
    require_selectable_agent,
    set_agent_enabled_map,
)
from lazyreview.services.settings import get_settings_view, update_app_settings


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


def test_schema_seeds_agent_enabled(tmp_path: Path):
    conn = _conn(tmp_path)
    ver = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert ver == SCHEMA_VERSION == 32
    enabled = get_agent_enabled_map(conn)
    assert enabled["agy"] is True
    assert enabled["claude"] is True


def test_normalize_agent_enabled_defaults_and_coerce():
    out = normalize_agent_enabled({"agy": False, "unknown": True, "claude": "yes"})
    assert out["agy"] is False
    assert out["claude"] is True
    assert "unknown" not in out
    assert out["cursor"] is True


def test_disable_agent_makes_not_selectable(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        credentials, "_env_baseline", {k: None for k in credentials.CREDENTIAL_KEYS}
    )
    credentials.set_credential(settings, "GEMINI_API_KEY", "test-gemini-key-xxxx")

    class _Adapter:
        name = "agy"
        display_name = "Agy"
        binary = "agy"

        def available(self):
            return True

        def list_models(self):
            return []

        def effort_options(self):
            return []

    with (
        patch(
            "lazyreview.services.agent_eligibility.get_adapter",
            return_value=_Adapter(),
        ),
        patch(
            "lazyreview.services.agent_eligibility.list_adapters",
            return_value=[_Adapter()],
        ),
        patch(
            "lazyreview.services.agent_eligibility.check_provider",
            return_value={"ok": True, "detail": "ok"},
        ),
    ):
        catalog = list_agent_catalog(settings, conn, probe_cli=False)
        agy = next(a for a in catalog if a["name"] == "agy")
        assert agy["selectable"] is True

        set_agent_enabled_map(conn, {"agy": False})
        catalog = list_agent_catalog(settings, conn, probe_cli=False)
        agy = next(a for a in catalog if a["name"] == "agy")
        assert agy["enabled"] is False
        assert agy["selectable"] is False
        assert "disabled" in (agy["reason"] or "")

        try:
            require_selectable_agent(settings, conn, "agy", probe_cli=False)
            assert False, "expected ValueError"
        except ValueError as e:
            assert "not selectable" in str(e)


def test_settings_view_exposes_and_updates_agent_enabled(tmp_path: Path):
    conn = _conn(tmp_path)
    view = get_settings_view(conn)
    assert view["agent_enabled"]["claude"] is True
    update_app_settings(conn, agent_enabled={"claude": False})
    view = get_settings_view(conn)
    assert view["agent_enabled"]["claude"] is False
    assert view["agent_enabled"]["agy"] is True
