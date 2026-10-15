"""Agent and GitHub credentials entered in Settings.

Stored in ``{data_dir}/credentials.json`` (mode 0600) and never sent back to the
browser. A stored value overrides the same variable from the process environment
(``.env`` under Docker). Every CLI the daemon spawns inherits ``os.environ``, so
applying a change means updating ``os.environ`` in place.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lazyreview.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CredentialSpec:
    key: str
    label: str
    provider: str
    help: str


CREDENTIALS: tuple[CredentialSpec, ...] = (
    CredentialSpec(
        key="GH_TOKEN",
        label="GitHub token",
        provider="github",
        help="Used by gh and git for PR sync, clones and posting. `gh auth token` prints yours.",
    ),
    CredentialSpec(
        key="CLAUDE_CODE_OAUTH_TOKEN",
        label="Claude subscription token",
        provider="claude",
        help="Run `claude setup-token` on any machine and paste the token. Uses your Claude plan.",
    ),
    CredentialSpec(
        key="ANTHROPIC_API_KEY",
        label="Anthropic API key",
        provider="claude",
        help="Billed per token through the Anthropic API.",
    ),
    CredentialSpec(
        key="CURSOR_API_KEY",
        label="Cursor API key",
        provider="cursor",
        help="From the Cursor dashboard (Integrations → API keys).",
    ),
    CredentialSpec(
        key="GEMINI_API_KEY",
        label="Gemini API key",
        provider="agy",
        help="From Google AI Studio. While set, agy uses the Gemini API instead of an account login.",
    ),
    CredentialSpec(
        key="CODEX_API_KEY",
        label="OpenAI API key",
        provider="codex",
        help="Billed per token through the OpenAI API. Use the browser login for your ChatGPT plan.",
    ),
)

CREDENTIAL_KEYS = frozenset(spec.key for spec in CREDENTIALS)

_BINARY_PROVIDER = {"claude": "claude", "agent": "cursor", "agy": "agy", "codex": "codex"}

_lock = threading.Lock()
# Environment values before any stored credential was applied, so clearing a
# stored value falls back to what .env provided.
_env_baseline: dict[str, str | None] = {
    key: (os.environ.get(key) or None) for key in CREDENTIAL_KEYS
}


def credentials_path(settings: Settings) -> Path:
    return settings.data_dir / "credentials.json"


def load_stored(settings: Settings) -> dict[str, str]:
    path = credentials_path(settings)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("ignoring unreadable %s: %s", path, e)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: str(v) for k, v in raw.items() if k in CREDENTIAL_KEYS and isinstance(v, str) and v
    }


def _write_stored(settings: Settings, data: dict[str, str]) -> None:
    path = credentials_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _apply(stored: dict[str, str]) -> None:
    for key in CREDENTIAL_KEYS:
        value = stored.get(key) or _env_baseline.get(key)
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    sync_agy_model_provider()


def apply_stored_credentials(settings: Settings) -> None:
    with _lock:
        _apply(load_stored(settings))


def set_credential(settings: Settings, key: str, value: str) -> None:
    if key not in CREDENTIAL_KEYS:
        raise ValueError(f"unknown credential {key}")
    value = value.strip()
    if not value:
        raise ValueError("value must not be empty")
    with _lock:
        stored = load_stored(settings)
        stored[key] = value
        _write_stored(settings, stored)
        _apply(stored)


def clear_credential(settings: Settings, key: str) -> None:
    if key not in CREDENTIAL_KEYS:
        raise ValueError(f"unknown credential {key}")
    with _lock:
        stored = load_stored(settings)
        stored.pop(key, None)
        _write_stored(settings, stored)
        _apply(stored)


def subprocess_env(argv: Sequence[str]) -> dict[str, str]:
    """Environment for a spawned CLI: GitHub plus only that CLI's own credentials,
    so a review agent cannot read the other providers' keys from its env."""
    provider = _BINARY_PROVIDER.get(Path(argv[0]).name) if argv else None
    env = dict(os.environ)
    for spec in CREDENTIALS:
        if spec.provider not in ("github", provider):
            env.pop(spec.key, None)
    return env


def _hint(value: str) -> str:
    return f"…{value[-4:]}" if len(value) > 8 else "set"


def credential_status(settings: Settings) -> list[dict[str, Any]]:
    stored = load_stored(settings)
    out: list[dict[str, Any]] = []
    for spec in CREDENTIALS:
        if stored.get(spec.key):
            source, value = "settings", stored[spec.key]
        elif _env_baseline.get(spec.key):
            source, value = "env", _env_baseline[spec.key] or ""
        else:
            source, value = None, ""
        out.append(
            {
                "key": spec.key,
                "label": spec.label,
                "provider": spec.provider,
                "help": spec.help,
                "source": source,
                "hint": _hint(value) if value else None,
            }
        )
    return out


def agy_settings_path() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def sync_agy_model_provider() -> None:
    """agy only honors GEMINI_API_KEY with modelProvider "gemini", and refuses to
    start with that provider once the key is gone."""
    path = agy_settings_path()
    has_key = bool(os.environ.get("GEMINI_API_KEY"))
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        logger.warning("cannot read %s; leaving agy provider unchanged", path)
        return
    if not isinstance(data, dict):
        return
    if has_key and data.get("modelProvider") != "gemini":
        data["modelProvider"] = "gemini"
    elif not has_key and data.get("modelProvider") == "gemini":
        del data["modelProvider"]
    else:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("cannot update %s: %s", path, e)
