#!/bin/bash

# Auto-install Python dependencies for compound-learning plugin
# Runs on SessionStart to ensure deps are ready before other hooks fire
# Idempotent: skips pip if all packages already importable
# Exits 0 always to avoid blocking session start

LOG_DIR="$HOME/.claude/plugins/compound-learning"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/activity.log"

log_activity() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# Quick check: can we import all required packages?
if python3 -c "
import importlib.util, sys
for pkg in ['pysqlite3', 'sqlite_vec', 'sentence_transformers']:
    if importlib.util.find_spec(pkg) is None:
        sys.exit(1)
" 2>/dev/null; then
  exit 0
fi

log_activity "[setup] Missing Python dependencies, installing..."

# Install all required packages quietly
if pip install --quiet pysqlite3-binary sqlite-vec sentence-transformers 2>>"$LOG_FILE"; then
  log_activity "[setup] Python dependencies installed successfully"
else
  log_activity "[setup] pip install failed (exit $?), plugin may not work correctly"
fi

exit 0
