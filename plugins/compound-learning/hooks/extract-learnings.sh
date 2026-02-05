#!/bin/bash

# Auto-extract learnings from Claude sessions via hooks
# Triggered by PreCompact and SessionEnd hooks

# Bail if plugin root not set
if [ -z "$CLAUDE_PLUGIN_ROOT" ]; then
  exit 0
fi

# Skip all hooks for subprocess calls (prevents recursion)
if [ -n "$CLAUDE_SUBPROCESS" ]; then
  exit 0
fi

# Read hook input from stdin
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id')
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path')
CWD=$(echo "$INPUT" | jq -r '.cwd')

# Expand ~ in transcript path
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

# Skip tiny transcripts (trivial sessions)
LINES=$(wc -l < "$TRANSCRIPT" 2>/dev/null || echo "0")
if [ "$LINES" -lt 20 ]; then
  exit 0
fi

# Extract just user/assistant messages (skip tool calls, file snapshots, metadata)
# Limit to ~80KB to fit context window with prompt overhead
MAX_BYTES=80000
TRANSCRIPT_CONTENT=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/extract-transcript-messages.py" "$TRANSCRIPT" "$MAX_BYTES" 2>/dev/null)
if [ -z "$TRANSCRIPT_CONTENT" ]; then
  exit 0
fi

TODAY=$(date +%Y-%m-%d)
GLOBAL_DIR="$HOME/.projects/learnings"
REPO_DIR="$CWD/.projects/learnings"

# Use heredoc to pass prompt via stdin (avoids temp files and arg size limits)
CLAUDE_SUBPROCESS=1 claude -p --no-session-persistence \
  --permission-mode bypassPermissions \
  --add-dir "$HOME/.projects" "$CWD/.projects" \
  --allowedTools "Write,Bash(mkdir:*)" \
  <<PROMPT_END >/dev/null 2>&1
Analyze this conversation transcript and extract 0-3 meaningful learnings.

IMPORTANT: You MUST use the Write tool to create files. Do not just describe what you would write.

For each learning you extract:
1. Use the Write tool to create a markdown file
2. Path: ${GLOBAL_DIR}/[topic]-${TODAY}.md for global, or ${REPO_DIR}/[topic]-${TODAY}.md for repo-specific
3. Content must include: # Title, **Type:** (pattern/gotcha/security), **Tags:**, ## Problem, ## Solution, ## Why

Be selective - only extract genuine reusable insights.
If nothing worth extracting, just output 'none'.

<transcript>
${TRANSCRIPT_CONTENT}
</transcript>
PROMPT_END

exit 0
