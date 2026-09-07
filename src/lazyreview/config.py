"""Load runtime configuration from TOML (XDG config path).

Precedence (highest wins):
1. Explicit env overrides: LAZYREVIEW_HOST, LAZYREVIEW_PORT, LAZYREVIEW_DATA_DIR
2. Values in the TOML config file
3. Built-in defaults

Config file path:
- LAZYREVIEW_CONFIG if set
- else ~/.config/lazyreview/config.toml (XDG)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    if raw:
        return Path(raw)
    return Path.home() / ".config"


def _xdg_data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    if raw:
        return Path(raw)
    return Path.home() / ".local" / "share"


DEFAULT_CONFIG_TOML = """\
# lazyreview configuration
# Docs: edit and restart the daemon (`lazyreview restart`) for server/path changes.

[server]
host = "127.0.0.1"
port = 3334

[paths]
# Empty = ~/.local/share/lazyreview (XDG data home)
data_dir = ""
# Empty = {data_dir}/runs
runs_dir = ""

[worker]
job_timeout_seconds = 3600
retention_sweep_seconds = 3600
# How many agent reviews may run at once across all PRs (each is a full CLI process).
max_concurrent_jobs = 10

[service]
unit_name = "lazyreview.service"
"""


def default_config_path() -> Path:
    return _xdg_config_home() / "lazyreview" / "config.toml"


def resolve_config_path() -> Path:
    override = os.environ.get("LAZYREVIEW_CONFIG")
    if override:
        return Path(override).expanduser()
    return default_config_path()


def ensure_config_file(path: Path | None = None) -> Path:
    """Create a default config.toml if missing. Returns the path used."""
    cfg_path = path or resolve_config_path()
    if cfg_path.exists():
        return cfg_path
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    # Prefer packaged template if present
    text = DEFAULT_CONFIG_TOML
    try:
        pkg = resources.files("lazyreview")
        candidate = pkg.joinpath("config.default.toml")
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError, ModuleNotFoundError):
        pass
    cfg_path.write_text(text, encoding="utf-8")
    return cfg_path


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    raw = data.get(name) or {}
    return raw if isinstance(raw, dict) else {}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    db_path: Path
    runs_dir: Path
    config_path: Path
    job_timeout_seconds: int
    retention_sweep_seconds: int
    max_concurrent_jobs: int
    service_name: str
    unit_name: str

    @classmethod
    def load(cls, *, create_if_missing: bool = True) -> Settings:
        cfg_path = resolve_config_path()
        if create_if_missing:
            ensure_config_file(cfg_path)
        data = _read_toml(cfg_path) if cfg_path.exists() else {}

        server = _section(data, "server")
        paths = _section(data, "paths")
        worker = _section(data, "worker")
        service = _section(data, "service")

        host = str(server.get("host") or "127.0.0.1")
        port = int(server.get("port") or 3334)

        data_dir_raw = str(paths.get("data_dir") or "").strip()
        if data_dir_raw:
            data_dir = Path(data_dir_raw).expanduser()
        else:
            data_dir = _xdg_data_home() / "lazyreview"

        runs_raw = str(paths.get("runs_dir") or "").strip()
        runs_dir = Path(runs_raw).expanduser() if runs_raw else data_dir / "runs"

        job_timeout = int(worker.get("job_timeout_seconds") or 3600)
        sweep = int(worker.get("retention_sweep_seconds") or 3600)
        concurrency = int(worker.get("max_concurrent_jobs") or 1)
        unit_name = str(service.get("unit_name") or "lazyreview.service")

        # Env overrides (optional, for one-off / install scripts)
        host = os.environ.get("LAZYREVIEW_HOST", host)
        if os.environ.get("LAZYREVIEW_PORT"):
            port = int(os.environ["LAZYREVIEW_PORT"])
        if os.environ.get("LAZYREVIEW_DATA_DIR"):
            data_dir = Path(os.environ["LAZYREVIEW_DATA_DIR"]).expanduser()
            if not runs_raw:
                runs_dir = data_dir / "runs"

        return cls(
            host=host,
            port=port,
            data_dir=data_dir,
            db_path=data_dir / "lazyreview.db",
            runs_dir=runs_dir,
            config_path=cfg_path,
            job_timeout_seconds=job_timeout,
            retention_sweep_seconds=sweep,
            max_concurrent_jobs=max(1, concurrency),
            service_name="lazyreview",
            unit_name=unit_name,
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "host": self.host,
            "port": self.port,
            "base_url": self.base_url,
            "data_dir": str(self.data_dir),
            "runs_dir": str(self.runs_dir),
            "db_path": str(self.db_path),
            "job_timeout_seconds": self.job_timeout_seconds,
            "retention_sweep_seconds": self.retention_sweep_seconds,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "unit_name": self.unit_name,
        }


_cached: Settings | None = None


def get_settings(*, reload: bool = False) -> Settings:
    global _cached
    if _cached is None or reload:
        _cached = Settings.load()
    return _cached


def write_default_config(path: Path | None = None, *, force: bool = False) -> Path:
    """Write the default TOML. If force=False and file exists, leave it."""
    cfg_path = path or resolve_config_path()
    if cfg_path.exists() and not force:
        return cfg_path
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return cfg_path
