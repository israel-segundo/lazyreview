# lazyreview

Agentic code-review management: run Cursor / Claude / Agy reviews against GitHub PRs, store findings and metrics in SQLite, consolidate and post feedback via `gh`.

## Prerequisites

- Python 3.12+
- `gh` authenticated (`gh auth login`)
- At least one agent CLI: `agent` (Cursor), `claude`, or `agy`
- Ubuntu with systemd user services (for install/autostart)
- Node/npm (for Tailwind CSS build during install)



## PR analysis & shared workspaces

lazyreview runs a **PR Analysis** job (orientation brief) before reviews or consolidations can start. At most **`max_concurrent_analyses`** (default `2`) analyses run at once; the rest stay queued.

Checkouts live under `{data_dir}/workspaces/` (bare mirrors + per-PR worktrees) and are reused by analysis and reviews. When a PR is closed or merged, its worktree is queued for deletion.

## Configuration

All daemon settings live in a TOML file:

```
~/.config/lazyreview/config.toml
```

Override the path with `LAZYREVIEW_CONFIG=/path/to/config.toml`.

```toml
[server]
host = "127.0.0.1"
port = 3334

[paths]
data_dir = ""   # empty → ~/.local/share/lazyreview
runs_dir = ""   # empty → {data_dir}/runs

[worker]
job_timeout_seconds = 3600
retention_sweep_seconds = 3600
max_concurrent_jobs = 10  # global parallel reviews (not per-PR)

[service]
unit_name = "lazyreview.service"
```

```bash
lazyreview config          # show effective settings
lazyreview config --init   # write default config if missing
```

Optional env overrides (for one-offs): `LAZYREVIEW_HOST`, `LAZYREVIEW_PORT`, `LAZYREVIEW_DATA_DIR`.

After changing `host`/`port`, re-run `./scripts/install.sh` (or edit the systemd unit bind) then `lazyreview restart`.

## Quick start (dev)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
npm install && npm run build:css
lazyreview config --init
lazyreview serve
```

Open http://127.0.0.1:3334 (or whatever `[server]` says).

## Install (Ubuntu, autostart)

```bash
./scripts/install.sh
lazyreview status
```

Uninstall:

```bash
./scripts/uninstall.sh          # keeps DB/runs + config
./scripts/uninstall.sh --purge  # deletes data + config
```

Service control:

```bash
lazyreview start|stop|restart|status|enable|disable
```

## Data layout

- Config: `~/.config/lazyreview/config.toml`
- DB: `{data_dir}/lazyreview.db` (default under `~/.local/share/lazyreview/`)
- Runs: `{data_dir}/runs/{id}/` (prompt, logs, findings, report)
- Clone `repo/` is removed when each run finishes

## Settings (UI)

`/settings` — run retention (default 1 week). Hourly daemon sweep + **Purge old runs now**.
