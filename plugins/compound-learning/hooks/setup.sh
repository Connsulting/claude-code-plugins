#!/bin/bash

# Auto-install Python dependencies for compound-learning plugin.
# Runs on SessionStart to ensure deps are ready before other hooks fire.
# Exits 0 always to avoid blocking session start.
#
# Delegates ALL install logic to lib/bootstrap.py, which is the single
# source of truth for dependency management. Packages are installed into
# ~/.claude/state/compound-learning/site-packages/ (--target), NOT the
# system Python environment.

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG_DIR="$HOME/.claude/plugins/compound-learning"
mkdir -p "$LOG_DIR"

python3 "$PLUGIN_ROOT/lib/bootstrap.py" 2>>"$LOG_DIR/activity.log"

exit 0
