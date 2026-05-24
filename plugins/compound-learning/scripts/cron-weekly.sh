#!/usr/bin/env bash
# Weekly compound-learning maintenance pass.
#
# Runs every Sunday morning (8am local by convention; adjust your crontab):
#   1. Apply autonomous consolidation under conservative gates
#   2. Refresh pinned.md from current hit counts
#   3. Snapshot the topic landscape for periodic review
#
# Each step is best-effort and logs to a single weekly file. A step failing
# does not abort the others. Idempotent: re-running on the same day produces
# additional log entries but no duplicate side-effects (auto-consolidate
# skips already-merged clusters; build-pinned overwrites; inspect-topics is
# read-only).
#
# Install (system cron):
#   crontab -e
#   0 8 * * 0  /home/$USER/git/connsulting/claude-code-plugins/plugins/compound-learning/scripts/cron-weekly.sh
#
# Install (systemd user timer): see README in this directory.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$HOME/.claude/plugins/compound-learning"
mkdir -p "$STATE_DIR"
LOG_FILE="$STATE_DIR/cron-weekly.log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

run_step() {
  local label="$1"
  shift
  log "START $label"
  if "$@" >> "$LOG_FILE" 2>&1; then
    log "OK    $label"
  else
    log "FAIL  $label (exit $?)"
  fi
}

log "=== weekly maintenance pass START ==="

# Step 1: autonomous consolidation. --apply does real merges; review queue
# captures everything else for periodic human attention.
run_step "auto-consolidate" python3 "$PLUGIN_ROOT/scripts/auto-consolidate.py" --apply

# Step 2: refresh pinned.md from current hit counts so the @import in
# CLAUDE.md surfaces the current top learnings.
run_step "build-pinned" python3 "$PLUGIN_ROOT/scripts/build-pinned.py"

# Step 3: snapshot the topic landscape. Output goes to the log; scan it
# periodically to see drift or emerging vocabulary worth aliasing.
run_step "inspect-topics" python3 "$PLUGIN_ROOT/scripts/inspect-topics.py" --limit 30

log "=== weekly maintenance pass END ==="
