"""CLI entrypoint: serve, status, start, stop, restart, enable, disable, config."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

from lazyreview import __version__
from lazyreview.config import ensure_config_file, get_settings, write_default_config


def _systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["systemctl", "--user", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def cmd_serve(host: str | None, port: int | None) -> int:
    import uvicorn

    settings = get_settings(reload=True)
    settings.ensure_dirs()
    ensure_config_file(settings.config_path)
    uvicorn.run(
        "lazyreview.main:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=False,
    )
    return 0


def cmd_status() -> int:
    settings = get_settings(reload=True)
    if not shutil.which("systemctl"):
        print("systemctl not found")
        return 1
    proc = _systemctl(["status", settings.unit_name, "--no-pager"])
    print(proc.stdout or proc.stderr)
    is_active = _systemctl(["is-active", settings.unit_name])
    print(f"active: {(is_active.stdout or '').strip()}")
    print(f"url: {settings.base_url}")
    print(f"config: {settings.config_path}")
    return 0 if is_active.returncode == 0 else 1


def cmd_start() -> int:
    settings = get_settings(reload=True)
    proc = _systemctl(["start", settings.unit_name])
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout or "start failed", file=sys.stderr)
        return proc.returncode
    print(f"started {settings.unit_name} → {settings.base_url}")
    return 0


def cmd_stop() -> int:
    settings = get_settings(reload=True)
    proc = _systemctl(["stop", settings.unit_name])
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout or "stop failed", file=sys.stderr)
        return proc.returncode
    print(f"stopped {settings.unit_name}")
    return 0


def cmd_restart() -> int:
    settings = get_settings(reload=True)
    proc = _systemctl(["restart", settings.unit_name])
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout or "restart failed", file=sys.stderr)
        return proc.returncode
    print(f"restarted {settings.unit_name} → {settings.base_url}")
    return 0


def cmd_enable() -> int:
    settings = get_settings(reload=True)
    proc = _systemctl(["enable", "--now", settings.unit_name])
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout or "enable failed", file=sys.stderr)
        return proc.returncode
    print(f"enabled {settings.unit_name}")
    print("Tip: loginctl enable-linger $USER so the service survives logout.")
    return 0


def cmd_disable() -> int:
    settings = get_settings(reload=True)
    proc = _systemctl(["disable", "--now", settings.unit_name])
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout or "disable failed", file=sys.stderr)
        return proc.returncode
    print(f"disabled {settings.unit_name}")
    return 0


def cmd_config(init: bool, force: bool) -> int:
    if init:
        path = write_default_config(force=force)
        print(f"wrote {path}")
        return 0
    settings = get_settings(reload=True)
    ensure_config_file(settings.config_path)
    print(json.dumps(settings.as_public_dict(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lazyreview", description="lazyreview daemon CLI")
    parser.add_argument("--version", action="version", version=f"lazyreview {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run foreground HTTP server (dev)")
    p_serve.add_argument("--host", default=None, help="Override [server].host from config.toml")
    p_serve.add_argument("--port", type=int, default=None, help="Override [server].port")

    sub.add_parser("status", help="Show systemd user service status")
    sub.add_parser("start", help="Start systemd user service")
    sub.add_parser("stop", help="Stop systemd user service")
    sub.add_parser("restart", help="Restart systemd user service")
    sub.add_parser("enable", help="Enable and start on login")
    sub.add_parser("disable", help="Disable and stop service")

    p_cfg = sub.add_parser("config", help="Show effective config (from TOML)")
    p_cfg.add_argument(
        "--init",
        action="store_true",
        help="Write default ~/.config/lazyreview/config.toml if missing",
    )
    p_cfg.add_argument(
        "--force",
        action="store_true",
        help="With --init, overwrite an existing config.toml",
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        return cmd_serve(args.host, args.port)
    if args.command == "status":
        return cmd_status()
    if args.command == "start":
        return cmd_start()
    if args.command == "stop":
        return cmd_stop()
    if args.command == "restart":
        return cmd_restart()
    if args.command == "enable":
        return cmd_enable()
    if args.command == "disable":
        return cmd_disable()
    if args.command == "config":
        return cmd_config(args.init, args.force)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
