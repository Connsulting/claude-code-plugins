#!/bin/bash

# Auto-extract learnings from Claude sessions via hooks
# Triggered by PreCompact and SessionEnd hooks

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/observability.sh" || exit 0

hook_log_init "extract-learnings"
SESSION_ID="${CLAUDE_SESSION_ID:-}"
HOOK_OUTCOME="success"
HOOK_OUTCOME_MESSAGE=""
FILE_COUNT=0
INDEXED_COUNT=0
INDEX_FAILED_COUNT=0

OUTPUT_FILE=""
BEFORE_FILES=""
AFTER_FILES=""

extract_finalize() {
  [ -n "$OUTPUT_FILE" ] && [ -f "$OUTPUT_FILE" ] && rm -f "$OUTPUT_FILE"
  [ -n "$BEFORE_FILES" ] && [ -f "$BEFORE_FILES" ] && rm -f "$BEFORE_FILES"
  [ -n "$AFTER_FILES" ] && [ -f "$AFTER_FILES" ] && rm -f "$AFTER_FILES"

  local duration_ms
  duration_ms="$(hook_elapsed_ms)"
  local level="info"
  case "$HOOK_OUTCOME" in
    success) level="info" ;;
    skipped) level="info" ;;
    error) level="error" ;;
    *) level="warn" ;;
  esac

  hook_obs_event "$level" "hook_end" "$HOOK_OUTCOME" \
    --session-id "$SESSION_ID" \
    --duration-ms "$duration_ms" \
    --message "$HOOK_OUTCOME_MESSAGE" \
    --counts-json "{\"generated\":$FILE_COUNT,\"indexed\":$INDEXED_COUNT,\"index_failed\":$INDEX_FAILED_COUNT}"
}
trap extract_finalize EXIT

hook_obs_event "info" "hook_start" "start" --session-id "$SESSION_ID"

# Bail if plugin root not set
if [ -z "$CLAUDE_PLUGIN_ROOT" ]; then
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="CLAUDE_PLUGIN_ROOT not set"
  hook_obs_event "info" "skip_reason" "skipped" --session-id "$SESSION_ID" --message "plugin root not set"
  exit 0
fi

# Skip all hooks for subprocess calls (prevents recursion)
if [ -n "$CLAUDE_SUBPROCESS" ]; then
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="Skipping in subprocess context"
  hook_obs_event "info" "skip_reason" "skipped" --session-id "$SESSION_ID" --message "CLAUDE_SUBPROCESS is set"
  exit 0
fi

# Source worktree-aware repo root resolution
source "${CLAUDE_PLUGIN_ROOT}/lib/git-worktree.sh" || {
  hook_log_activity "[extract-learnings] WARN: could not source git-worktree.sh, using CWD as REPO_ROOT"
  hook_obs_event "warn" "repo_root_resolve" "fallback" --session-id "$SESSION_ID" --message "git-worktree.sh source failed"
}

# Read hook input from stdin
INPUT=$(cat)
SESSION_FROM_INPUT=$(echo "$INPUT" | jq -r '.session_id // empty')
if [ -n "$SESSION_FROM_INPUT" ] && [ "$SESSION_FROM_INPUT" != "null" ]; then
  SESSION_ID="$SESSION_FROM_INPUT"
fi
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')

if [ -z "$TRANSCRIPT" ] || [ "$TRANSCRIPT" = "null" ]; then
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="No transcript path provided"
  hook_obs_event "info" "skip_reason" "skipped" --session-id "$SESSION_ID" --message "missing transcript path"
  exit 0
fi

# Resolve real repo root (handles worktrees)
REPO_ROOT="$CWD"
if command -v resolve_repo_root >/dev/null 2>&1; then
  REPO_ROOT=$(resolve_repo_root "$CWD")
fi
REPO_ROOT="${REPO_ROOT:-$CWD}"

# Expand ~ in transcript path
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

# Skip tiny transcripts (trivial sessions)
LINES=$(wc -l < "$TRANSCRIPT" 2>/dev/null || echo "0")
if [ "$LINES" -lt 20 ]; then
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="Transcript too short"
  hook_obs_event "info" "skip_reason" "skipped" --session-id "$SESSION_ID" --message "transcript line count < 20"
  exit 0
fi

# Extract just user/assistant messages (skip tool calls, file snapshots, metadata)
# Limit to ~80KB to fit context window with prompt overhead
MAX_BYTES=80000
extract_started="$(hook_now_ms)"
ERR_FILE=$(mktemp)
TRANSCRIPT_CONTENT=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/extract-transcript-messages.py" "$TRANSCRIPT" "$MAX_BYTES" 2>"$ERR_FILE")
extract_exit=$?
extract_duration=$(( $(hook_now_ms) - extract_started ))
if [ -s "$ERR_FILE" ]; then
  hook_log_activity "[extract-learnings] parse error: $(cat "$ERR_FILE")"
fi
if [ "$extract_exit" -eq 0 ]; then
  hook_obs_event "info" "subprocess_exit" "success" \
    --session-id "$SESSION_ID" \
    --duration-ms "$extract_duration" \
    --counts-json '{"command":"extract_transcript_messages","exit_code":0}'
else
  hook_obs_event "warn" "subprocess_exit" "failure" \
    --session-id "$SESSION_ID" \
    --duration-ms "$extract_duration" \
    --counts-json "{\"command\":\"extract_transcript_messages\",\"exit_code\":$extract_exit}"
fi
rm -f "$ERR_FILE"

if [ -z "$TRANSCRIPT_CONTENT" ]; then
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="No transcript content extracted"
  hook_obs_event "info" "skip_reason" "skipped" --session-id "$SESSION_ID" --message "transcript extraction returned empty content"
  exit 0
fi

TODAY=$(date +%Y-%m-%d)
GLOBAL_DIR="$HOME/.projects/learnings"
REPO_DIR="$REPO_ROOT/.projects/learnings"

# Capture output to log file generation
OUTPUT_FILE=$(mktemp)

# Snapshot learnings directories before claude call for filesystem diff
BEFORE_FILES=$(mktemp)
AFTER_FILES=$(mktemp)
find "$GLOBAL_DIR" "$REPO_DIR" -type f -name "*.md" 2>/dev/null | sort > "$BEFORE_FILES"

hook_log_activity "EXTRACT_START: session=$SESSION_ID lines=$LINES"
hook_obs_event "info" "extract_start" "start" \
  --session-id "$SESSION_ID" \
  --counts-json "{\"transcript_lines\":$LINES}"

# Use heredoc to pass prompt via stdin (avoids temp files and arg size limits)
claude_started="$(hook_now_ms)"
CLAUDE_SUBPROCESS=1 claude -p --no-session-persistence \
  --permission-mode bypassPermissions \
  --add-dir "$HOME/.projects" "$REPO_ROOT/.projects" \
  --allowedTools "Write,Bash(mkdir:*)" \
  <<PROMPT_END >"$OUTPUT_FILE" 2>&1
Analyze this conversation transcript and extract 0-3 meaningful learnings.

IMPORTANT: You MUST use the Write tool to create files. Do not just describe what you would write.

For each learning you extract:
1. Use the Write tool to create a markdown file
2. Path: ${GLOBAL_DIR}/[topic]-${TODAY}.md for global, or ${REPO_DIR}/[topic]-${TODAY}.md for repo-specific
3. Content must include: # Title, **Type:** (pattern/gotcha/security), **Topic:** (broad category), **Tags:**, ## Problem, ## Solution, ## Why

Be selective - only extract genuine reusable insights.
If nothing worth extracting, just output 'none'.

<transcript>
${TRANSCRIPT_CONTENT}
</transcript>
PROMPT_END
claude_exit=$?
claude_duration=$(( $(hook_now_ms) - claude_started ))

if [ "$claude_exit" -eq 0 ]; then
  hook_obs_event "info" "subprocess_exit" "success" \
    --session-id "$SESSION_ID" \
    --duration-ms "$claude_duration" \
    --counts-json '{"command":"claude_extract","exit_code":0}'
else
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="claude extraction command failed"
  hook_obs_event "error" "subprocess_exit" "failure" \
    --session-id "$SESSION_ID" \
    --duration-ms "$claude_duration" \
    --counts-json "{\"command\":\"claude_extract\",\"exit_code\":$claude_exit}"
fi

# Detect new files via filesystem diff
find "$GLOBAL_DIR" "$REPO_DIR" -type f -name "*.md" 2>/dev/null | sort > "$AFTER_FILES"
NEW_FILES=$(comm -13 "$BEFORE_FILES" "$AFTER_FILES")

INDEX_SCRIPT="${CLAUDE_PLUGIN_ROOT}/skills/index-learnings/index-learnings.py"

while IFS= read -r file; do
  [ -z "$file" ] && continue
  FILE_COUNT=$((FILE_COUNT + 1))
  FILENAME=$(basename "$file")

  # Extract first # heading as title
  TITLE=$(grep -m1 "^# " "$file" 2>/dev/null | sed 's/^# //' | tr -d '\n\r')
  [ -z "$TITLE" ] && TITLE="$FILENAME"

  hook_log_activity "  GENERATED: file=$FILENAME title=\"$TITLE\""

  # Index the newly created file into SQLite (non-fatal if unavailable)
  if [ -f "$INDEX_SCRIPT" ]; then
    index_started="$(hook_now_ms)"
    CLAUDE_PLUGIN_ROOT="$CLAUDE_PLUGIN_ROOT" python3 "$INDEX_SCRIPT" --file "$file" >> "$HOOK_ACTIVITY_LOG" 2>&1
    index_exit=$?
    index_duration=$(( $(hook_now_ms) - index_started ))
    if [ "$index_exit" -eq 0 ]; then
      INDEXED_COUNT=$((INDEXED_COUNT + 1))
      hook_log_activity "  INDEXED: $FILENAME"
      hook_obs_event "info" "subprocess_exit" "success" \
        --session-id "$SESSION_ID" \
        --duration-ms "$index_duration" \
        --counts-json "{\"command\":\"index_single_file\",\"exit_code\":0}"
    else
      INDEX_FAILED_COUNT=$((INDEX_FAILED_COUNT + 1))
      hook_log_activity "  INDEX_SKIP: indexing failed for $FILENAME"
      hook_obs_event "warn" "subprocess_exit" "failure" \
        --session-id "$SESSION_ID" \
        --duration-ms "$index_duration" \
        --counts-json "{\"command\":\"index_single_file\",\"exit_code\":$index_exit}"
    fi
  fi
done <<< "$NEW_FILES"

hook_log_activity "EXTRACT_END: session=$SESSION_ID generated=$FILE_COUNT"
hook_obs_event "info" "extract_complete" "success" \
  --session-id "$SESSION_ID" \
  --counts-json "{\"generated\":$FILE_COUNT,\"indexed\":$INDEXED_COUNT,\"index_failed\":$INDEX_FAILED_COUNT}"

if [ "$FILE_COUNT" -eq 0 ] && [ "$HOOK_OUTCOME" = "success" ]; then
  HOOK_OUTCOME_MESSAGE="No new learnings generated"
else
  HOOK_OUTCOME_MESSAGE="Generated $FILE_COUNT learning file(s)"
fi

exit 0
