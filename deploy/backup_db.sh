#!/usr/bin/env bash
# ==============================================================================
# backup_db.sh
# Performs a non-blocking live SQLite online backup and retains rolling 14 days.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
INSTANCE_DIR="${BASE_DIR}/instance"
BACKUP_DIR="${INSTANCE_DIR}/backups"
DB_PATH="${INSTANCE_DIR}/mateo.db"

mkdir -p "$BACKUP_DIR"
TARGET="$BACKUP_DIR/mateo_$(date +%Y%m%d).db"

PYTHON_BIN="python3"
if [ -f "${BASE_DIR}/venv/bin/python3" ]; then
    PYTHON_BIN="${BASE_DIR}/venv/bin/python3"
elif [ -f "${BASE_DIR}/.venv/bin/python3" ]; then
    PYTHON_BIN="${BASE_DIR}/.venv/bin/python3"
fi

"$PYTHON_BIN" -c "
import sqlite3
src = sqlite3.connect('$DB_PATH')
dst = sqlite3.connect('$TARGET')
src.backup(dst)
src.close()
dst.close()
"

# Keep rolling 14 days of backups
find "$BACKUP_DIR" -name "mateo_*.db" -mtime +14 -delete
echo "Backup successfully created: $TARGET"
