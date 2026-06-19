#!/bin/bash
# Shared resolution for the Codex compound-learning hook wrappers.
# Sourced by peek.sh, extract.sh, and setup.sh.

# Resolve the plugin root from this script's own location: these wrappers live
# at <plugin-root>/codex/, so the plugin root is the parent dir. This keeps the
# wrappers portable (no hardcoded paths) — they work wherever the plugin is
# installed. Codex does not set CLAUDE_PLUGIN_ROOT (it is a Claude plugin var),
# so we export it for the plugin's own scripts.
CL_CODEX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CLAUDE_PLUGIN_ROOT="$(cd "$CL_CODEX_DIR/.." && pwd)"

# Codex-side state: converted transcripts, extraction snapshots, debounce
# markers. Lives under the plugin's existing state location.
CL_STATE_DIR="$HOME/.claude/state/codex-compound-learning"
CL_TRANSCRIPT_DIR="$CL_STATE_DIR/transcripts"
CL_SNAP_DIR="$CL_STATE_DIR/snapshots"
CL_MARKER_DIR="$CL_STATE_DIR/markers"
CL_LOG="$HOME/.claude/plugins/compound-learning/codex-activity.log"
mkdir -p "$CL_TRANSCRIPT_DIR" "$CL_SNAP_DIR" "$CL_MARKER_DIR" 2>/dev/null

# Prune state older than 24h so converted transcripts and extraction snapshots
# don't accumulate unbounded.
find "$CL_TRANSCRIPT_DIR" -maxdepth 1 -name "*.jsonl" -mmin +1440 -delete 2>/dev/null
find "$CL_SNAP_DIR" -maxdepth 1 -type d -mmin +1440 -exec rm -rf {} + 2>/dev/null

cl_log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$CL_LOG" 2>/dev/null; }

# Convert a Codex rollout into a Claude-format transcript at $2.
cl_convert_rollout() {
  local rollout="$1" out="$2"
  python3 "$CL_CODEX_DIR/rollout-to-transcript.py" "$rollout" > "$out" 2>>"$CL_LOG"
}
