#!/bin/bash

# Auto-extract learnings from Claude sessions via hooks
# Triggered by PreCompact and Stop hooks

DEBUG_LOG="$HOME/.claude/compound-hook-debug.log"

# Read hook input from stdin
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id')
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path')
CWD=$(echo "$INPUT" | jq -r '.cwd')
HOOK_TYPE="${HOOK_TYPE:-unknown}"

echo "$(date '+%Y-%m-%d %H:%M:%S'): Hook triggered type=$HOOK_TYPE session=$SESSION_ID cwd=$CWD" >> "$DEBUG_LOG"

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
claude -p "Read the transcript at $TRANSCRIPT and extract 0-3 meaningful learnings.

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
