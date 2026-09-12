#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/lazyreview"
CONFIG_FILE="${LAZYREVIEW_CONFIG:-$CONFIG_DIR/config.toml}"
DEFAULT_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/lazyreview"
UNIT_DST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/lazyreview.service"

echo "==> lazyreview install"
echo "    config: $CONFIG_FILE"
echo "    root:   $ROOT"

mkdir -p "$CONFIG_DIR" "$(dirname "$UNIT_DST")"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "==> Writing default config.toml"
  cp "$ROOT/src/lazyreview/config.default.toml" "$CONFIG_FILE"
fi

# Temporary venv bootstrap under default data dir; real data_dir may come from TOML
BOOT_DATA="${LAZYREVIEW_DATA_DIR:-$DEFAULT_DATA}"
VENV="$BOOT_DATA/venv"
mkdir -p "$BOOT_DATA"

if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --upgrade pip
pip install -e "$ROOT"

echo "==> Building Tailwind CSS"
if command -v npm >/dev/null 2>&1; then
  (cd "$ROOT" && npm install && npm run build:css)
else
  echo "WARNING: npm not found; writing minimal fallback CSS"
  mkdir -p "$ROOT/src/lazyreview/web/static/css"
  cat > "$ROOT/src/lazyreview/web/static/css/app.css" <<'CSS'
body{font-family:system-ui,sans-serif;margin:0;background:#f8fafc;color:#0f172a}
a{color:#2563eb}
.container{max-width:64rem;margin:0 auto;padding:1rem}
CSS
fi

pip install -e "$ROOT"

# Resolve effective settings from TOML via the installed package
mapfile -t _cfg < <(
  LAZYREVIEW_CONFIG="$CONFIG_FILE" python - <<'PY'
from lazyreview.config import get_settings
s = get_settings(reload=True)
s.ensure_dirs()
print(s.host)
print(s.port)
print(s.data_dir)
print(s.config_path)
print(s.unit_name)
print(s.base_url)
PY
)
HOST="${_cfg[0]}"
PORT="${_cfg[1]}"
DATA_DIR="${_cfg[2]}"
CONFIG_PATH="${_cfg[3]}"
UNIT_NAME="${_cfg[4]}"
BASE_URL="${_cfg[5]}"

# Prefer venv next to configured data_dir
VENV="$DATA_DIR/venv"
if [[ ! -d "$VENV" ]]; then
  if [[ "$DATA_DIR" != "$BOOT_DATA" && -d "$BOOT_DATA/venv" ]]; then
    mkdir -p "$DATA_DIR"
    mv "$BOOT_DATA/venv" "$VENV"
  else
    python3 -m venv "$VENV"
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    pip install --upgrade pip
    pip install -e "$ROOT"
  fi
fi

echo "==> Installing systemd user unit (bind ${HOST}:${PORT})"
cat > "$UNIT_DST" <<EOF
[Unit]
Description=lazyreview — agentic code review daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$VENV/bin/gunicorn lazyreview.main:app --worker-class uvicorn.workers.UvicornWorker --workers 1 --bind ${HOST}:${PORT} --timeout 120
Restart=on-failure
RestartSec=3
Environment=LAZYREVIEW_CONFIG=${CONFIG_PATH}
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

echo ""
echo "Installed and started."
echo "  UI:     $BASE_URL"
echo "  Config: $CONFIG_PATH"
echo "  Status: lazyreview status"
echo "  Tip:    loginctl enable-linger \$USER   # keep running after logout"
echo "  Edit config.toml then: lazyreview restart (re-run install.sh if host/port changed)"
echo ""
