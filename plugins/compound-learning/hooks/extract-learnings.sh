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

# Source worktree-aware repo root resolution
source "${CLAUDE_PLUGIN_ROOT}/lib/git-worktree.sh" || { log_activity "[extract-learnings] WARN: could not source git-worktree.sh, using CWD as REPO_ROOT"; }

# Read hook input from stdin
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id')
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path')
CWD=$(echo "$INPUT" | jq -r '.cwd')

# Resolve real repo root (handles worktrees)
REPO_ROOT=$(resolve_repo_root "$CWD")
REPO_ROOT="${REPO_ROOT:-$CWD}"

# Expand ~ in transcript path
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

# Skip tiny transcripts (trivial sessions)
LINES=$(wc -l < "$TRANSCRIPT" 2>/dev/null || echo "0")
if [ "$LINES" -lt 20 ]; then
  exit 0
fi

TODAY=$(date +%Y-%m-%d)

# Daily ingestion cap. This hook fires on every SessionEnd and PreCompact, so an
# active day writes 60-108 rows (measured July 2026) into a corpus where the large
# majority are never used again. Pruning downstream of an unbounded writer is a
# treadmill: the 2026-07-21 crawl recovered 1,140 rows and was fully undone in 19
# days. Capping the writer is the half that was missing.
#
# The cap is checked BEFORE transcript extraction and generation, so hitting it
# also saves the generator tokens rather than just discarding its output.
# 0 disables the cap.
MAX_PER_DAY="${COMPOUND_LEARNING_MAX_PER_DAY:-20}"
LEARNING_DB="${COMPOUND_LEARNING_DB:-$HOME/.claude/compound-learning.db}"
if [ "$MAX_PER_DAY" -gt 0 ] && [ -f "$LEARNING_DB" ] && command -v sqlite3 >/dev/null 2>&1; then
  # created_at is stored as ISO8601 with a 'T' separator; compare against the
  # same shape. Using SQLite's `datetime('now',...)` here would compare 'T'
  # against ' ' and silently drop every row dated on the boundary day.
  TODAY_COUNT=$(sqlite3 "$LEARNING_DB" \
    "SELECT COUNT(*) FROM learnings WHERE created_at >= '${TODAY}T00:00:00';" 2>/dev/null)
  case "$TODAY_COUNT" in
    ''|*[!0-9]*) TODAY_COUNT=0 ;;
  esac
  if [ "$TODAY_COUNT" -ge "$MAX_PER_DAY" ]; then
    log_activity "EXTRACT_SKIP: daily cap reached ($TODAY_COUNT/$MAX_PER_DAY for $TODAY)"
    exit 0
  fi
fi

# Extract just user/assistant messages (skip tool calls, file snapshots, metadata)
# Limit to ~80KB to fit context window with prompt overhead
MAX_BYTES=80000
ERR_FILE=$(mktemp)
TRANSCRIPT_CONTENT=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/extract-transcript-messages.py" "$TRANSCRIPT" "$MAX_BYTES" 2>"$ERR_FILE")
[ -s "$ERR_FILE" ] && log_activity "[extract-learnings] parse error: $(cat "$ERR_FILE")"
rm -f "$ERR_FILE"
if [ -z "$TRANSCRIPT_CONTENT" ]; then
  exit 0
fi

GLOBAL_DIR="$HOME/.projects/learnings"
REPO_DIR="$REPO_ROOT/.projects/learnings"

# Capture output to log file generation
OUTPUT_FILE=$(mktemp)
PROMPT_FILE=$(mktemp)

# Snapshot learnings directories before generation for filesystem diff
BEFORE_FILES=$(mktemp)
AFTER_FILES=$(mktemp)
mkdir -p "$GLOBAL_DIR" 2>/dev/null
find "$GLOBAL_DIR" "$REPO_DIR" -type f -name "*.md" 2>/dev/null | sort > "$BEFORE_FILES"

trap "rm -f $OUTPUT_FILE $PROMPT_FILE $BEFORE_FILES $AFTER_FILES" EXIT

log_activity "EXTRACT_START: session=$SESSION_ID lines=$LINES"

# Write the prompt once so both generation engines read identical stdin.
cat >"$PROMPT_FILE" <<PROMPT_END
Analyze this conversation transcript and extract 0-2 meaningful learnings.

Most sessions warrant ZERO. Writing nothing is the common, correct outcome, and
'none' is a complete answer. Measured over the 30 days to 2026-07-29, under 2% of
retrieved learnings were ever actually cited in later work, so a marginal entry
does not sit harmlessly in a corpus -- it gets injected into future sessions and
displaces something useful.

Extract ONLY if the insight passes all four:
1. Non-obvious - not something a competent engineer would conclude in five minutes.
2. Durable - still true next month; not tied to one branch, ticket, or transient state.
3. Reusable - will plausibly recur in a DIFFERENT task, not just this one.
4. Not already covered - if it restates an existing learning, skip it.

Do NOT write a learning for: what the task was, what you changed, a summary of the
session, a bug fixed once, a one-off command that worked, or a restatement of
documented tool behavior.

IMPORTANT: You MUST create markdown files in the filesystem. Do not just describe what you would write.

For each learning you extract:
1. Create a markdown file
2. Path: ${GLOBAL_DIR}/[topic]-${TODAY}.md for global, or ${REPO_DIR}/[topic]-${TODAY}.md for repo-specific
3. Content must include: # Title, **Type:** (pattern/gotcha/security), **Topic:** (broad category), **Tags:**, ## Problem, ## Solution, ## Why
4. Keep it under 3,000 characters. Anything injected into future sessions pays its
   size on every hit; past ~4,000 chars it gets truncated at injection time anyway.

If nothing worth extracting, just output 'none'.

<transcript>
${TRANSCRIPT_CONTENT}
</transcript>
PROMPT_END

if [ "${COMPOUND_LEARNING_GENERATION_ENGINE:-}" = "codex" ]; then
  CLAUDE_SUBPROCESS=1 timeout 300 codex exec \
    --model gpt-5.4-mini \
    -c 'model_reasoning_effort="low"' \
    --ignore-user-config \
    --ignore-rules \
    --disable hooks \
    --ephemeral \
    --skip-git-repo-check \
    --sandbox workspace-write \
    -C "$REPO_ROOT" \
    --add-dir "$GLOBAL_DIR" \
    - <"$PROMPT_FILE" >"$OUTPUT_FILE" 2>&1
else
  CLAUDE_SUBPROCESS=1 claude -p --no-session-persistence \
    --permission-mode bypassPermissions \
    --add-dir "$HOME/.projects" "$REPO_ROOT/.projects" \
    --allowedTools "Write,Bash(mkdir:*)" \
    <"$PROMPT_FILE" >"$OUTPUT_FILE" 2>&1
fi

# Detect new files via filesystem diff
find "$GLOBAL_DIR" "$REPO_DIR" -type f -name "*.md" 2>/dev/null | sort > "$AFTER_FILES"
NEW_FILES=$(comm -13 "$BEFORE_FILES" "$AFTER_FILES")

INDEX_SCRIPT="${CLAUDE_PLUGIN_ROOT}/skills/index-learnings/index-learnings.py"
FILE_COUNT=0

while IFS= read -r file; do
  [ -z "$file" ] && continue
  FILE_COUNT=$((FILE_COUNT + 1))
  FILENAME=$(basename "$file")

  # Extract first # heading as title
  TITLE=$(grep -m1 "^# " "$file" 2>/dev/null | sed 's/^# //' | tr -d '\n\r')
  [ -z "$TITLE" ] && TITLE="$FILENAME"

  log_activity "  GENERATED: file=$FILENAME title=\"$TITLE\""

  # Index the newly created file into SQLite (non-fatal if unavailable).
  # On failure, append to index-failures.log so the next auto-peek surfaces it
  # to the user — this hook is async and can't echo to the live conversation.
  if [ -f "$INDEX_SCRIPT" ]; then
    if CLAUDE_PLUGIN_ROOT="$CLAUDE_PLUGIN_ROOT" python3 "$INDEX_SCRIPT" --file "$file" >> "$LOG_FILE" 2>&1; then
      log_activity "  INDEXED: $FILENAME"
    else
      log_activity "  INDEX_SKIP: indexing failed for $FILENAME"
      echo "$(date '+%Y-%m-%d %H:%M:%S') $FILENAME" >> "$LOG_DIR/index-failures.log"
    fi
  fi
done <<< "$NEW_FILES"

log_activity "EXTRACT_END: session=$SESSION_ID generated=$FILE_COUNT"

exit 0
