#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE="${LAZYREVIEW_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/lazyreview/config.toml}"
DEFAULT_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/lazyreview"
UNIT_DST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/lazyreview.service"
PURGE=0
if [[ "${1:-}" == "--purge" ]]; then
  PURGE=1
fi

echo "==> lazyreview uninstall"

DATA_DIR="$DEFAULT_DATA"
if command -v python3 >/dev/null 2>&1; then
  RESOLVED="$(
    LAZYREVIEW_CONFIG="$CONFIG_FILE" python3 -c \
      'from lazyreview.config import get_settings; print(get_settings(reload=True).data_dir)' \
      2>/dev/null || true
  )"
  if [[ -n "${RESOLVED:-}" ]]; then
    DATA_DIR="$RESOLVED"
  fi
fi

systemctl --user stop lazyreview.service 2>/dev/null || true
systemctl --user disable lazyreview.service 2>/dev/null || true
rm -f "$UNIT_DST"
systemctl --user daemon-reload 2>/dev/null || true

if [[ -d "$DATA_DIR/venv" ]]; then
  rm -rf "$DATA_DIR/venv"
  echo "Removed venv"
fi

if [[ "$PURGE" -eq 1 ]]; then
  rm -rf "$DATA_DIR"
  rm -f "$CONFIG_FILE"
  rmdir "$(dirname "$CONFIG_FILE")" 2>/dev/null || true
  echo "Purged data ($DATA_DIR) and config ($CONFIG_FILE)"
else
  echo "Kept data at $DATA_DIR and config at $CONFIG_FILE (pass --purge to delete)"
fi

echo "Done."
