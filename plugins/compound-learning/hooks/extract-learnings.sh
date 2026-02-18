#!/bin/bash

# Auto-extract learnings from Claude sessions via hooks
# Triggered by PreCompact and SessionEnd hooks

# Bail if plugin root not set
if [ -z "$CLAUDE_PLUGIN_ROOT" ]; then
  exit 0
fi

# Setup logging
LOG_DIR="$HOME/.claude/plugins/compound-learning"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/activity.log"

log_activity() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

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

# Capture output to log file generation
OUTPUT_FILE=$(mktemp)
trap "rm -f $OUTPUT_FILE" EXIT

log_activity "EXTRACT_START: session=$SESSION_ID lines=$LINES"

# Use heredoc to pass prompt via stdin (avoids temp files and arg size limits)
CLAUDE_SUBPROCESS=1 claude -p --no-session-persistence \
  --permission-mode bypassPermissions \
  --add-dir "$HOME/.projects" "$CWD/.projects" \
  --allowedTools "Write,Bash(mkdir:*)" \
  <<PROMPT_END >"$OUTPUT_FILE" 2>&1
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

# Parse output for generated learning files
# Look for file paths created in learnings directories
CREATED_FILES=$(grep -E "\.projects/learnings/.*\.md" "$OUTPUT_FILE" 2>/dev/null | grep -v "file_path" | head -10)
FILE_COUNT=0

INDEX_SCRIPT="${CLAUDE_PLUGIN_ROOT}/skills/index-learnings/index-learnings.py"

if [ -n "$CREATED_FILES" ]; then
  while IFS= read -r file_path; do
    # Clean up file path (remove ANSI codes, quotes, etc)
    CLEAN_PATH=$(echo "$file_path" | sed 's/\x1b\[[0-9;]*m//g' | grep -oE '\.projects/learnings/[^[:space:]"]+\.md' | head -1)

    if [ -n "$CLEAN_PATH" ]; then
      # Extract filename
      FILENAME=$(basename "$CLEAN_PATH")

      # Try to extract title from the file if it exists
      FULL_PATH=""
      if [[ "$CLEAN_PATH" == *"$HOME/.projects"* ]] || [[ "$CLEAN_PATH" == "$HOME"* ]]; then
        FULL_PATH="$HOME/$CLEAN_PATH"
      elif [ -f "$GLOBAL_DIR/$FILENAME" ]; then
        FULL_PATH="$GLOBAL_DIR/$FILENAME"
      elif [ -f "$REPO_DIR/$FILENAME" ]; then
        FULL_PATH="$REPO_DIR/$FILENAME"
      fi

      TITLE="$FILENAME"
      if [ -f "$FULL_PATH" ]; then
        # Extract first # heading as title
        TITLE=$(grep -m1 "^# " "$FULL_PATH" 2>/dev/null | sed 's/^# //')
        [ -z "$TITLE" ] && TITLE="$FILENAME"

        # Index the newly created file into SQLite (non-fatal if unavailable)
        if [ -f "$INDEX_SCRIPT" ]; then
          CLAUDE_PLUGIN_ROOT="$CLAUDE_PLUGIN_ROOT" python3 "$INDEX_SCRIPT" --file "$FULL_PATH" >> "$LOG_FILE" 2>&1 \
            && log_activity "  INDEXED: $FILENAME" \
            || log_activity "  INDEX_SKIP: indexing failed for $FILENAME"
        fi
      fi

      log_activity "  GENERATED: file=$FILENAME title=\"$TITLE\""
      FILE_COUNT=$((FILE_COUNT + 1))
    fi
  done <<< "$CREATED_FILES"
fi

log_activity "EXTRACT_END: session=$SESSION_ID generated=$FILE_COUNT"

exit 0
