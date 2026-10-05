#!/bin/sh
set -e
export CONFIG_DIR="$ROOT_DIR/Config"
export DATA_DIR="$ROOT_DIR/Data"
mkdir -p "$CONFIG_DIR" "$DATA_DIR"
# Data can be huge; only fix ownership of the folders themselves, not every file.
chown "$PUID:$PGID" "$ROOT_DIR" "$DATA_DIR" 2>/dev/null || true
chown -R "$PUID:$PGID" "$CONFIG_DIR"
umask "$UMASK"
export HOME="$CONFIG_DIR"
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups \
  python -m uvicorn main:app --host 0.0.0.0 --port 8080 --app-dir /srv \
  --log-level "${LOG_LEVEL:-warning}"
