#!/bin/bash
# Grok SessionEnd -> work-log.sh.
# Grok's session teardown budget is about 10 seconds, so this wrapper returns
# immediately and the Notion write runs detached. Subagent teardowns are skipped.

set -u

WL_GROK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CLAUDE_PLUGIN_ROOT="$(cd "$WL_GROK_DIR/.." && pwd)"
export WORK_LOG_ENGINE=grok
export WORK_LOG_SOURCE_PREFIX=gx

LOG_DIR="$HOME/.claude/plugins/work-log"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/activity.log"

log_activity() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

INPUT=$(cat 2>/dev/null || true)
[ -n "${CLAUDE_SUBPROCESS:-}" ] && exit 0

SUBAGENT=$(printf '%s' "$INPUT" | jq -r '.subagentType // .subagent_type // empty' 2>/dev/null)
if [ -n "$SUBAGENT" ]; then
  exit 0
fi

SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.sessionId // .session_id // empty' 2>/dev/null)
MARKER_DIR="$LOG_DIR/grok-sessions"
mkdir -p "$MARKER_DIR"
if [ -n "$SESSION_ID" ] && [ -f "$MARKER_DIR/${SESSION_ID}.logged" ]; then
  log_activity "[grok] SKIP: already logged session=$SESSION_ID"
  exit 0
fi

TMPIN=$(mktemp)
printf '%s' "$INPUT" > "$TMPIN"

if [ -n "$SESSION_ID" ]; then
  echo "$(date -Iseconds)" > "$MARKER_DIR/${SESSION_ID}.logged"
fi

log_activity "[grok] session-end backgrounding session=${SESSION_ID:-unknown}"

# Detach so claude -p outlives Grok's SessionEnd budget.
setsid bash -c 'bash "$1" < "$2"; rm -f "$2"' _ \
  "$CLAUDE_PLUGIN_ROOT/hooks/work-log.sh" "$TMPIN" \
  >>"$LOG_FILE" 2>&1 < /dev/null &

exit 0
