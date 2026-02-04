#!/bin/bash

# Auto-extract learnings from Claude sessions via hooks
# Triggered by PreCompact and Stop hooks

# Bail if plugin root not set (though this script doesn't use it directly,
# the plugin context should always be available)
if [ -z "$CLAUDE_PLUGIN_ROOT" ]; then
  exit 0
fi

# Prevent recursive execution: when we spawn claude -p below, that session
# ending would trigger this hook again. This env var breaks the cycle.
if [ -n "$CLAUDE_HOOK_EXTRACTING" ]; then
  exit 0
fi

# Read hook input from stdin
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id')
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path')
CWD=$(echo "$INPUT" | jq -r '.cwd')

# Expand ~ in transcript path
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

# Simple cycle prevention: track processed sessions
STATE="$HOME/.claude/compound-processed-sessions"
mkdir -p "$(dirname "$STATE")"
if grep -qx "$SESSION_ID" "$STATE" 2>/dev/null; then
  exit 0
fi

# Skip tiny transcripts (trivial sessions like the -p call itself)
LINES=$(wc -l < "$TRANSCRIPT" 2>/dev/null || echo "0")
if [ "$LINES" -lt 20 ]; then
  exit 0
fi

# Let Claude do everything: read transcript, analyze, write files
# CLAUDE_HOOK_EXTRACTING prevents the spawned session from triggering this hook again
CLAUDE_HOOK_EXTRACTING=1 claude -p --no-session-persistence "Read the transcript at $TRANSCRIPT and extract 0-3 meaningful learnings.

For each learning:
1. Determine scope: 'global' (~/.projects/learnings/) or 'repo' ($CWD/.projects/learnings/)
2. Write a markdown file with format: [topic]-[YYYY-MM-DD].md
3. Include: title, type (pattern/gotcha/security), tags, problem, solution, why

Be selective. Only extract genuine insights that help future work.
If nothing meaningful, write no files.

Output only the list of files written (one per line), or 'none' if no learnings." \
  --allowedTools "Read,Write,Bash(mkdir:*)" \
  2>/dev/null

# Mark as processed (only after successful extraction attempt)
echo "$SESSION_ID" >> "$STATE"
exit 0
