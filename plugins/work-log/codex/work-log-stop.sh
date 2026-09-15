#!/bin/bash
# Codex Stop -> work-log.sh.
# Codex has no SessionEnd and no async hooks. Stop fires once per `codex exec`
# and per turn when interactive. Debounce short/noisy turns, then detach so
# the hook returns immediately.

set -u

WL_CODEX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CLAUDE_PLUGIN_ROOT="$(cd "$WL_CODEX_DIR/.." && pwd)"
export WORK_LOG_ENGINE=codex
export WORK_LOG_SOURCE_PREFIX=cx

LOG_DIR="$HOME/.claude/plugins/work-log"
MARKER_DIR="$LOG_DIR/codex-sessions"
mkdir -p "$MARKER_DIR"
LOG_FILE="$LOG_DIR/activity.log"

log_activity() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

INPUT=$(cat 2>/dev/null || true)
[ -n "${CLAUDE_SUBPROCESS:-}" ] && exit 0

ROLLOUT=$(printf '%s' "$INPUT" | jq -r '.transcript_path // .transcriptPath // empty' 2>/dev/null)
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // .sessionId // empty' 2>/dev/null)
[ -z "$ROLLOUT" ] || [ -z "$SESSION_ID" ] && exit 0
ROLLOUT="${ROLLOUT/#\~/$HOME}"
[ -f "$ROLLOUT" ] || exit 0

# Floor: skip sessions too short to be worth a Notion write. Matches work-log.sh.
LINES=$(head -n 50 "$ROLLOUT" 2>/dev/null | wc -l)
LINES=${LINES// /}
if [ "${LINES:-0}" -lt 40 ]; then
  exit 0
fi

# Re-log an interactive session only after this many new raw lines.
DELTA_LINES=40
MARKER="$MARKER_DIR/${SESSION_ID}.logged"
COUNT=$(wc -l < "$ROLLOUT" 2>/dev/null | tr -d ' ')
[[ "$COUNT" =~ ^[0-9]+$ ]] || COUNT=0
LAST=$(awk '{print $1; exit}' "$MARKER" 2>/dev/null)
[[ "$LAST" =~ ^[0-9]+$ ]] || LAST=0
if [ -f "$MARKER" ] && [ "$((COUNT - LAST))" -lt "$DELTA_LINES" ]; then
  exit 0
fi

TMPIN=$(mktemp)
printf '%s' "$INPUT" > "$TMPIN"

log_activity "[codex] stop backgrounding session=$SESSION_ID lines=$COUNT"

setsid bash -c 'bash "$1" < "$2"; rm -f "$2"' _ \
  "$CLAUDE_PLUGIN_ROOT/hooks/work-log.sh" "$TMPIN" \
  >>"$LOG_FILE" 2>&1 < /dev/null &

# Record the line count we launched with so a later Stop can detect growth.
echo "$COUNT $(date -Iseconds)" > "$MARKER"

exit 0
