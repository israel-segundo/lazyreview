"""Credentials stored from Settings: env override, masking, agy provider, logins."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from lazyreview.config import Settings
from lazyreview.services import cli_login, credentials
from lazyreview.services.cli_login import LoginManager, LoginProvider


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


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch):
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for key in credentials.CREDENTIAL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        credentials, "_env_baseline", {k: None for k in credentials.CREDENTIAL_KEYS}
    )


def test_stored_value_overrides_env_and_clear_restores_it(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setitem(credentials._env_baseline, "CURSOR_API_KEY", "from-env-1234")
    monkeypatch.setenv("CURSOR_API_KEY", "from-env-1234")

    credentials.set_credential(settings, "CURSOR_API_KEY", "  from-ui-abcdefgh  ")
    assert os.environ["CURSOR_API_KEY"] == "from-ui-abcdefgh"
    path = credentials.credentials_path(settings)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"CURSOR_API_KEY": "from-ui-abcdefgh"}

    row = next(r for r in credentials.credential_status(settings) if r["key"] == "CURSOR_API_KEY")
    assert row["source"] == "settings"
    assert row["hint"] == "…efgh"
    assert "from-ui" not in json.dumps(credentials.credential_status(settings))

    credentials.clear_credential(settings, "CURSOR_API_KEY")
    assert os.environ["CURSOR_API_KEY"] == "from-env-1234"
    row = next(r for r in credentials.credential_status(settings) if r["key"] == "CURSOR_API_KEY")
    assert row["source"] == "env"


def test_apply_drops_empty_env_values(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "")
    credentials.apply_stored_credentials(_settings(tmp_path))
    assert "GH_TOKEN" not in os.environ


def test_unknown_credential_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        credentials.set_credential(_settings(tmp_path), "PATH", "x")


def test_gemini_key_toggles_agy_model_provider(tmp_path: Path):
    settings = _settings(tmp_path)
    agy = credentials.agy_settings_path()
    agy.parent.mkdir(parents=True)
    agy.write_text(json.dumps({"theme": "dark"}))

    credentials.set_credential(settings, "GEMINI_API_KEY", "gemini-key-123456")
    assert json.loads(agy.read_text()) == {"theme": "dark", "modelProvider": "gemini"}

    credentials.clear_credential(settings, "GEMINI_API_KEY")
    assert json.loads(agy.read_text()) == {"theme": "dark"}


def _fake_provider(script: str, *, accepts_code: bool = False) -> LoginProvider:
    return LoginProvider(
        id="fake",
        label="Fake",
        binary=sys.executable,
        argv=(sys.executable, "-c", script),
        accepts_code=accepts_code,
    )


def _wait(session, predicate, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = session.view()
        if predicate(view):
            return view
        time.sleep(0.05)
    raise AssertionError(f"timed out; last view: {session.view()}")


def test_login_relays_url_and_code(monkeypatch):
    script = (
        "import sys\n"
        "print('Visit https://example.com/auth?x=1 to log in', flush=True)\n"
        "print('Or, paste the authorization code here and press Enter:', flush=True)\n"
        "sys.exit(0 if sys.stdin.readline().strip() == 'abc' else 3)\n"
    )
    monkeypatch.setitem(cli_login.LOGIN_PROVIDERS, "fake", _fake_provider(script, accepts_code=True))
    manager = LoginManager()
    session = manager.start("fake")

    view = _wait(session, lambda v: v["needs_code"])
    assert view["url"] == "https://example.com/auth?x=1"

    manager.submit_code(session.id, " abc ")
    view = _wait(session, lambda v: v["status"] != "running")
    assert view["status"] == "succeeded"
    assert view["needs_code"] is False


def test_login_can_give_the_cli_a_tty_stdin(monkeypatch):
    script = (
        "import sys\n"
        "if not sys.stdin.isatty(): sys.exit(5)\n"
        "print('https://accounts.example.com/o/oauth2/auth', flush=True)\n"
        "print('Or, paste the authorization code here and press Enter:', flush=True)\n"
        "sys.exit(0 if sys.stdin.readline().strip() == 'tty-code' else 3)\n"
    )
    provider = LoginProvider(
        id="fake",
        label="Fake",
        binary=sys.executable,
        argv=(sys.executable, "-c", script),
        accepts_code=True,
        stdin_tty=True,
    )
    monkeypatch.setitem(cli_login.LOGIN_PROVIDERS, "fake", provider)
    manager = LoginManager()
    session = manager.start("fake")

    _wait(session, lambda v: v["needs_code"])
    manager.submit_code(session.id, "tty-code")
    assert _wait(session, lambda v: v["status"] != "running")["status"] == "succeeded"
    assert session.tty_fd is None


def test_login_parses_gh_device_code_and_cancels(monkeypatch):
    script = (
        "import time\n"
        "print('! First copy your one-time code: AB12-CD34', flush=True)\n"
        "print('Open this URL to continue in your web browser: https://github.com/login/device', flush=True)\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setitem(cli_login.LOGIN_PROVIDERS, "fake", _fake_provider(script))
    manager = LoginManager()
    session = manager.start("fake")

    view = _wait(session, lambda v: v["user_code"] and v["url"])
    assert view["user_code"] == "AB12-CD34"
    assert view["url"] == "https://github.com/login/device"

    manager.cancel(session.id)
    assert _wait(session, lambda v: v["exit_code"] is not None)["status"] == "cancelled"


def test_subprocess_env_keeps_only_own_provider_keys(monkeypatch):
    for key in credentials.CREDENTIAL_KEYS:
        monkeypatch.setenv(key, f"value-{key}")
    monkeypatch.setenv("PATH", "/usr/bin")

    def kept(argv):
        return set(credentials.subprocess_env(argv)) & credentials.CREDENTIAL_KEYS

    assert kept(["/usr/local/bin/claude", "-p"]) == {
        "GH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    }
    assert kept(["/usr/local/bin/agent"]) == {"GH_TOKEN", "CURSOR_API_KEY"}
    assert kept(["/usr/bin/gh", "pr", "review"]) == {"GH_TOKEN"}
    assert credentials.subprocess_env(["claude"])["PATH"] == "/usr/bin"


def test_untrusted_host_header_is_rejected(tmp_path: Path):
    settings = dataclasses.replace(_settings(tmp_path), allowed_hosts=("localhost", "127.0.0.1"))
    with patch("lazyreview.main.get_settings", return_value=settings):
        from lazyreview.main import create_app

        app = create_app()
        with TestClient(app, base_url="http://localhost:3334") as client:
            assert client.get("/api/credentials").status_code == 200
            evil = client.get("/api/credentials", headers={"Host": "attacker.example"})
            assert evil.status_code == 400


def test_credentials_api_never_returns_values(tmp_path: Path):
    settings = _settings(tmp_path)
    with patch("lazyreview.main.get_settings", return_value=settings):
        from lazyreview.main import create_app

        app = create_app()
        with TestClient(app) as client:
            res = client.put("/api/credentials/ANTHROPIC_API_KEY", json={"value": "sk-ant-secret-9999"})
            assert res.status_code == 200
            assert "sk-ant-secret" not in res.text
            row = next(r for r in res.json()["credentials"] if r["key"] == "ANTHROPIC_API_KEY")
            assert row == {**row, "source": "settings", "hint": "…9999"}

            providers = {p["id"]: p["login"] for p in res.json()["providers"]}
            assert providers == {"github": True, "claude": False, "cursor": True, "agy": True}

            assert client.put("/api/credentials/PATH", json={"value": "x"}).status_code == 404
            res = client.delete("/api/credentials/ANTHROPIC_API_KEY")
            row = next(r for r in res.json()["credentials"] if r["key"] == "ANTHROPIC_API_KEY")
            assert row["source"] is None
            assert client.post("/api/credentials/login/claude").status_code == 404
