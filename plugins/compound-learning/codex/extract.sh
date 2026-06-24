#!/bin/bash
# Codex Stop -> compound-learning extract-learnings.
# Codex has no SessionEnd/PreCompact event and does not support async hooks, so
# we hang extraction off Stop (which fires once per `codex exec`, per-turn when
# interactive) and:
#   1. debounce so interactive sessions extract only after enough new content,
#   2. self-background the heavy generation so the hook returns immediately.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
cl_log "[extract] invoked (CLAUDE_SUBPROCESS='${CLAUDE_SUBPROCESS:-}')"

INPUT=$(cat 2>/dev/null || true)

[ -n "$CLAUDE_SUBPROCESS" ] && exit 0

# Floor: skip sessions too short to hold a reusable learning. Matches the
# plugin's own <20-line bail, applied here so we never spawn claude -p for them.
MIN_MSGS=20
# Re-extract within a long interactive session only after this many new messages.
DELTA_MSGS=12

ROLLOUT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
[ -z "$ROLLOUT" ] || [ -z "$SESSION_ID" ] && exit 0
[ -f "$ROLLOUT" ] || exit 0

# Immutable per-invocation snapshot. The detached generator reads this for the
# duration of the (slow) claude -p call, so it must not be a path a later peek/
# Stop can overwrite. Keep the basename ${SESSION_ID}.jsonl for tidiness.
SNAP_DIR="$CL_SNAP_DIR/${SESSION_ID}.$$"
mkdir -p "$SNAP_DIR" 2>/dev/null
CONV="$SNAP_DIR/${SESSION_ID}.jsonl"
cl_convert_rollout "$ROLLOUT" "$CONV"

COUNT=$(wc -l < "$CONV" 2>/dev/null | tr -d ' ')
[[ "$COUNT" =~ ^[0-9]+$ ]] || COUNT=0
if [ "$COUNT" -lt "$MIN_MSGS" ]; then rm -rf "$SNAP_DIR"; exit 0; fi

MARKER="$CL_MARKER_DIR/${SESSION_ID}.count"
LAST=$(cat "$MARKER" 2>/dev/null)
[[ "$LAST" =~ ^[0-9]+$ ]] || LAST=0
if [ -f "$MARKER" ] && [ "$((COUNT - LAST))" -lt "$DELTA_MSGS" ]; then
  rm -rf "$SNAP_DIR"
  exit 0
fi

NEWINPUT=$(echo "$INPUT" | jq -c --arg t "$CONV" '.transcript_path = $t')
TMPIN=$(mktemp)
echo "$NEWINPUT" > "$TMPIN"

cl_log "[extract] backgrounding generation session=$SESSION_ID msgs=$COUNT"

# Detach so the generation outlives this `codex exec` process. The plugin's
# extract-learnings.sh generates via `claude -p` (a Claude subprocess, so it
# cannot recurse into Codex Stop hooks) and indexes into the shared SQLite DB.
# Positional args avoid quoting hazards in interpolated paths; the snapshot dir
# is cleaned up by the detached job once generation finishes.
setsid bash -c 'bash "$1" < "$2"; rm -f "$2"; rm -rf "$3"' _ \
  "$CLAUDE_PLUGIN_ROOT/hooks/extract-learnings.sh" "$TMPIN" "$SNAP_DIR" \
  >>"$CL_LOG" 2>&1 < /dev/null &

# Advance the debounce marker only after a successful launch, so a failed
# launch does not suppress a later retry.
echo "$COUNT" > "$MARKER"

exit 0
