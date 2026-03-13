#!/bin/bash

# Ensure lightweight SQLite dependencies on SessionStart.
# Heavy embedding dependencies install lazily on first semantic use.
# Exits 0 always to avoid blocking Claude session startup on bootstrap errors.

if [ -z "$CLAUDE_PLUGIN_ROOT" ]; then
  CLAUDE_PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi

STATUS_FILE="$HOME/.claude/plugins/compound-learning/bootstrap-status.json"

if [ -f "$STATUS_FILE" ] && command -v jq >/dev/null 2>&1; then
  CORE_STATE=$(jq -r '.dependencies.core.state // empty' "$STATUS_FILE" 2>/dev/null)

  if [ "$CORE_STATE" = "ready" ] && python3 - <<'PY' >/dev/null 2>&1
import importlib.util
import os
import sqlite3
import sys

site_dir = os.path.expanduser("~/.claude/plugins/compound-learning/site-packages")
if site_dir not in sys.path:
    sys.path.insert(0, site_dir)

def module_available(name):
    return importlib.util.find_spec(name) is not None

def sqlite_supports_extensions(sqlite_module):
    try:
        conn = sqlite_module.connect(":memory:")
    except Exception:
        return False
    try:
        return hasattr(conn, "enable_load_extension")
    finally:
        conn.close()

if not module_available("sqlite_vec"):
    raise SystemExit(1)

if sqlite_supports_extensions(sqlite3):
    raise SystemExit(0)

try:
    import pysqlite3 as pysqlite3_sqlite
except ImportError:
    raise SystemExit(1)

raise SystemExit(0 if sqlite_supports_extensions(pysqlite3_sqlite) else 1)
PY
  then
    exit 0
  fi

  if [ "$CORE_STATE" = "installing" ] && python3 "${CLAUDE_PLUGIN_ROOT}/lib/bootstrap.py" probe core --quiet >/dev/null 2>&1; then
    exit 0
  fi
fi

python3 "${CLAUDE_PLUGIN_ROOT}/lib/bootstrap.py" ensure core --quiet >/dev/null 2>&1 || true

exit 0
