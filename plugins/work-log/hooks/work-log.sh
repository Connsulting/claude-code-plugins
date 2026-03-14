#!/bin/bash

# Auto-log substantive Claude Code sessions to Notion Work Log
# Triggered by SessionEnd hook

# Bail if plugin root not set
if [ -z "$CLAUDE_PLUGIN_ROOT" ]; then
  exit 0
fi

# Setup logging
LOG_DIR="$HOME/.claude/plugins/work-log"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/activity.log"

log_activity() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# Skip all hooks for subprocess calls (prevents recursion)
if [ -n "$CLAUDE_SUBPROCESS" ]; then
  exit 0
fi

# Read hook input from stdin (single jq call)
INPUT=$(cat)
eval "$(echo "$INPUT" | jq -r '@sh "SESSION_ID=\(.session_id) TRANSCRIPT=\(.transcript_path) CWD=\(.cwd)"')"

# Expand ~ in transcript path
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

# Quick size check before any config reads
LINES=$(head -n 50 "$TRANSCRIPT" 2>/dev/null | wc -l)

# Merge user config (survives reinstalls) over bundled defaults
BUNDLED_CONFIG="${CLAUDE_PLUGIN_ROOT}/.claude-plugin/config.json"
USER_CONFIG="$LOG_DIR/config.json"
TEMP_CONFIG=""
if [ -f "$USER_CONFIG" ]; then
  TEMP_CONFIG=$(mktemp)
  jq -s '.[0] * .[1]' "$BUNDLED_CONFIG" "$USER_CONFIG" > "$TEMP_CONFIG"
  CONFIG_FILE="$TEMP_CONFIG"
else
  CONFIG_FILE="$BUNDLED_CONFIG"
fi
eval "$(jq -r '@sh "DATABASE_ID=\(.notion.databaseId) MCP_SERVER=\(.notion.mcpServerName // "claude_ai_Notion") SOURCE_PREFIX=\(.sourcePrefix // "cc") MIN_LINES=\(.minTranscriptLines // 40) TIMEZONE=\(.timezone // "UTC") DEFAULT_PROJECT=\(.defaultProject // "personal") PROJECT_PATTERN=\(.projectPattern // ".*/git/([^/]+).*")"' "$CONFIG_FILE")"

if [ "$LINES" -lt "$MIN_LINES" ]; then
  log_activity "SKIP: transcript too short ($LINES lines, minimum $MIN_LINES)"
  exit 0
fi

# Extract just user/assistant messages (skip tool calls, file snapshots, metadata)
# Limit to ~80KB to fit context window with prompt overhead
MAX_BYTES=80000
ERR_FILE=$(mktemp)
TRANSCRIPT_CONTENT=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/extract-transcript-messages.py" "$TRANSCRIPT" "$MAX_BYTES" 2>"$ERR_FILE")
[ -s "$ERR_FILE" ] && log_activity "[work-log] parse error: $(cat "$ERR_FILE")"
rm -f "$ERR_FILE"
if [ -z "$TRANSCRIPT_CONTENT" ]; then
  log_activity "SKIP: empty transcript content"
  exit 0
fi

# Extract project from cwd using configurable pattern (default: */git/{project}/*)
PROJECT=$(echo "$CWD" | sed -nE "s|${PROJECT_PATTERN}|\1|p")

# Apply project mappings from config
if [ -n "$PROJECT" ]; then
  MAPPED=$(jq -r --arg p "$PROJECT" '.projectMappings[$p] // empty' "$CONFIG_FILE")
  if [ -n "$MAPPED" ]; then
    PROJECT="$MAPPED"
  fi
fi

# Default if not in a git/ path
: "${PROJECT:=$DEFAULT_PROJECT}"

# Look up session name from sessions-index.json (customTitle from /rename, or summary)
ENCODED_CWD=$(echo "$CWD" | sed 's|/|-|g')
SESSIONS_INDEX="$HOME/.claude/projects/${ENCODED_CWD}/sessions-index.json"
SESSION_NAME=""
if [ -f "$SESSIONS_INDEX" ]; then
  SESSION_NAME=$(jq -r --arg id "$SESSION_ID" '
    .entries[] | select(.sessionId == $id) |
    .customTitle // .summary // empty
  ' "$SESSIONS_INDEX" 2>/dev/null | head -1)
fi

# Compute session tag and timestamp
SESSION_TAG="${SOURCE_PREFIX}:${SESSION_ID:0:8}"
TODAY=$(TZ="$TIMEZONE" date +%Y-%m-%d)
TIMESTAMP=$(TZ="$TIMEZONE" date +'%I:%M %p')

# Capture output to log file generation
OUTPUT_FILE=$(mktemp)
trap "rm -f $OUTPUT_FILE $TEMP_CONFIG" EXIT

log_activity "WORK_LOG_START: session=$SESSION_ID project=$PROJECT tag=$SESSION_TAG"

# Use heredoc to pass prompt via stdin (avoids temp files and arg size limits)
NOTION_TOOLS="mcp__${MCP_SERVER}__notion-search,mcp__${MCP_SERVER}__notion-fetch,mcp__${MCP_SERVER}__notion-create-pages,mcp__${MCP_SERVER}__notion-update-page,mcp__${MCP_SERVER}__notion-create-comment,mcp__${MCP_SERVER}__notion-get-comments"
CLAUDE_SUBPROCESS=1 ENABLE_CLAUDEAI_MCP_SERVERS=true claude -p --no-session-persistence \
  --permission-mode bypassPermissions \
  --allowedTools "Read,ToolSearch,${NOTION_TOOLS}" \
  <<PROMPT_END >"$OUTPUT_FILE" 2>&1
You are a work log assistant. Evaluate this session and, if substantive, log it to Notion.

Session Tag: ${SESSION_TAG}
Session Name: ${SESSION_NAME:-none}
Project: ${PROJECT}
Date: ${TODAY}
Time: ${TIMESTAMP}
Database ID: ${DATABASE_ID}

## Evaluate

Skip trivial sessions (quick lookups, single questions, typos, meta-config). Log sessions with meaningful work (features, fixes, architecture, planning, infra).

If not worth logging, output only the word SKIP on a line by itself followed by a brief reason, then stop.

## Write the summary

Focus on outcomes and value delivered, not technical implementation details:
- What was accomplished? What concrete work product was delivered?
- Why does it matter for the project? What problem does it solve or what capability does it add?
- Include specific details: what was built, fixed, or decided. Avoid vague language.
- If ticket/issue IDs appear in the transcript (JIRA like PROJ-1234, Linear, GitHub #123), include them as references.

Bad: "Refactored the codebase and improved code quality"
Good: "Built auto-logging plugin that writes session summaries to Notion on session end. Enables automatic work tracking across client projects without manual timekeeping."

## Toggle label

The toggle label format is: ${SESSION_TAG} - {Short Name}
- If Session Name above is not "none", use it as the short name
- Otherwise, generate a descriptive 3-5 word name from the transcript (e.g., "Notion Work Log Plugin", "Auth Bug Fix", "API Rate Limiting")

When searching for an existing toggle (for resume/dedup), match on "${SESSION_TAG}" as a prefix only. Ignore the name portion after " - ".

## Log to Notion

1. Search database ${DATABASE_ID} for a page titled "${TODAY}". Create one if missing.
2. Fetch the page blocks. Look for an H2 "${PROJECT}" and a toggle starting with "${SESSION_TAG}".
3. If no "${PROJECT}" H2 exists, append one.
4. If a toggle starting with "${SESSION_TAG}" exists (resumed session):
   a. If the toggle label differs from your computed label (e.g., name changed via /rename), update the toggle's title text
   b. Append inside it: [${TIMESTAMP}] claude-code (resumed): {summary}
5. Otherwise, append a new toggle with the label from above containing:
   [${TIMESTAMP}] claude-code: {1-2 sentence summary with ticket refs if any}
   {1 sentence on value to the project}

If toggles are unsupported, use H3 + paragraph as fallback. Prefer duplicates over data loss.

<transcript>
${TRANSCRIPT_CONTENT}
</transcript>
PROMPT_END

EXIT_CODE=$?

# Log output for debugging
if [ -f "$OUTPUT_FILE" ]; then
  # Check if subprocess decided to skip
  if grep -qm1 "^SKIP" "$OUTPUT_FILE" 2>/dev/null; then
    REASON=$(grep -m1 "^SKIP" "$OUTPUT_FILE")
    log_activity "WORK_LOG_SKIP: $REASON"
  else
    log_activity "WORK_LOG_END: session=$SESSION_ID exit=$EXIT_CODE"
  fi
  # Log first few lines of output
  head -5 "$OUTPUT_FILE" >> "$LOG_FILE" 2>/dev/null
fi

exit 0
