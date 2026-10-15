"""CLI entrypoint: serve, config."""

from __future__ import annotations

import argparse
import json

from lazyreview import __version__
from lazyreview.config import ensure_config_file, get_settings, write_default_config


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
    if args.command == "config":
        return cmd_config(args.init, args.force)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
