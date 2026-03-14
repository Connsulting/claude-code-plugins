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

# Read hook input from stdin
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id')
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path')
CWD=$(echo "$INPUT" | jq -r '.cwd')

# Expand ~ in transcript path
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

# Read config
CONFIG_FILE="${CLAUDE_PLUGIN_ROOT}/.claude-plugin/config.json"
DATABASE_ID=$(jq -r '.notion.databaseId' "$CONFIG_FILE")
MCP_SERVER=$(jq -r '.notion.mcpServerName // "claude_ai_Notion"' "$CONFIG_FILE")
SOURCE_PREFIX=$(jq -r '.sourcePrefix // "cc"' "$CONFIG_FILE")
MIN_LINES=$(jq -r '.minTranscriptLines // 40' "$CONFIG_FILE")
TIMEZONE=$(jq -r '.timezone // "UTC"' "$CONFIG_FILE")
DEFAULT_PROJECT=$(jq -r '.defaultProject // "personal"' "$CONFIG_FILE")
PROJECT_PATTERN=$(jq -r '.projectPattern // ".*/git/([^/]+).*"' "$CONFIG_FILE")

# Check transcript size
LINES=$(wc -l < "$TRANSCRIPT" 2>/dev/null || echo "0")
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

# Extract project name from cwd using configurable regex pattern
PROJECT=$(echo "$CWD" | sed -nE "s|${PROJECT_PATTERN}|\1|p" | tr -d '\n\r')

# Apply project mappings from config (e.g., {"vulns-curietech": "curietech"})
if [ -n "$PROJECT" ]; then
  MAPPED=$(jq -r --arg p "$PROJECT" '.projectMappings[$p] // empty' "$CONFIG_FILE")
  if [ -n "$MAPPED" ]; then
    PROJECT="$MAPPED"
  fi
fi

# Default if pattern didn't match
if [ -z "$PROJECT" ]; then
  PROJECT="$DEFAULT_PROJECT"
fi

# Compute session tag and timestamp
SESSION_TAG="${SOURCE_PREFIX}:${SESSION_ID:0:8}"
TODAY=$(TZ="$TIMEZONE" date +%Y-%m-%d)
TIMESTAMP=$(TZ="$TIMEZONE" date +'%I:%M %p')

# Capture output to log file generation
OUTPUT_FILE=$(mktemp)
trap "rm -f $OUTPUT_FILE" EXIT

log_activity "WORK_LOG_START: session=$SESSION_ID project=$PROJECT tag=$SESSION_TAG"

# Use heredoc to pass prompt via stdin (avoids temp files and arg size limits)
NOTION_TOOLS="mcp__${MCP_SERVER}__notion-search,mcp__${MCP_SERVER}__notion-fetch,mcp__${MCP_SERVER}__notion-create-pages,mcp__${MCP_SERVER}__notion-update-page,mcp__${MCP_SERVER}__notion-create-comment,mcp__${MCP_SERVER}__notion-get-comments"
CLAUDE_SUBPROCESS=1 ENABLE_CLAUDEAI_MCP_SERVERS=true claude -p --no-session-persistence \
  --permission-mode bypassPermissions \
  --allowedTools "Read,ToolSearch,${NOTION_TOOLS}" \
  <<PROMPT_END >"$OUTPUT_FILE" 2>&1
You are a work log assistant. Your job is to evaluate this Claude Code session and, if substantive, log it to a Notion Work Log database.

## Session Details
- Session ID: ${SESSION_ID}
- Session Tag: ${SESSION_TAG}
- Project: ${PROJECT}
- Today's Date: ${TODAY}
- Timestamp: ${TIMESTAMP}
- Working Directory: ${CWD}

## Step 1: Evaluate Substantiveness

Read the transcript below. Decide if this session is worth logging. The bar is: "Would this be useful context in a weekly review of what was worked on for this project?"

Skip (output "SKIP: [reason]" and stop) if:
- Quick lookups or single-question answers
- Trivial fixes (typos, formatting)
- Sessions that didn't produce meaningful work product
- Meta-sessions about configuring Claude itself (unless significant)

Log if:
- New features were built or significant code was written
- Bugs were investigated or fixed
- Architecture decisions were made
- Meaningful planning or analysis was done
- Infrastructure or deployment work was completed

## Step 2: Generate Entry

If substantive, create a 1-2 sentence summary in this format:
\`\`\`
[${TIMESTAMP}] claude-code: {1-2 sentence summary of what was done}
{1 sentence on why this matters to the project}
\`\`\`

## Step 3: Write to Notion

Use the Notion MCP tools to write this entry. Follow these steps exactly:

### 3a. Find today's page
Search the Work Log database (ID: ${DATABASE_ID}) for a page titled "${TODAY}".

Use the Notion MCP search or database query tool to find it. If no page exists for today, create one with title "${TODAY}" in the database.

### 3b. Read existing blocks
Get the block children of today's page to check:
- Is there already a Heading 2 block with text "${PROJECT}"?
- Is there already a toggle block with label containing "${SESSION_TAG}"?

### 3c. Handle project heading
If no "${PROJECT}" heading exists, append a Heading 2 block with text "${PROJECT}" to the page.

### 3d. Handle session entry

**If a toggle with "${SESSION_TAG}" already exists** (resumed session):
Append a paragraph block INSIDE that toggle with:
\`\`\`
[${TIMESTAMP}] claude-code (resumed): {summary of additional work}
\`\`\`

**If no existing toggle for this session:**
Append a toggle block right after the "${PROJECT}" heading with:
- Toggle label: ${SESSION_TAG}
- Toggle content (as a paragraph block inside): the entry text from Step 2

**Important Notion MCP notes:**
- When appending blocks, append them as children of the page (after the correct heading)
- Toggle blocks in Notion API use type "toggle" with rich_text for the label and children for content
- If toggle blocks are not supported by the MCP, use a Heading 3 for the session tag with a paragraph block beneath it as fallback
- If any Notion operation fails, log the error and exit gracefully. Prefer duplicates over data loss.

## Transcript

<transcript>
${TRANSCRIPT_CONTENT}
</transcript>
PROMPT_END

EXIT_CODE=$?

# Log output for debugging
if [ -f "$OUTPUT_FILE" ]; then
  # Check if subprocess decided to skip
  if grep -q "^SKIP:" "$OUTPUT_FILE" 2>/dev/null; then
    REASON=$(grep "^SKIP:" "$OUTPUT_FILE" | head -1)
    log_activity "WORK_LOG_SKIP: $REASON"
  else
    log_activity "WORK_LOG_END: session=$SESSION_ID exit=$EXIT_CODE"
  fi
  # Log first few lines of output
  head -5 "$OUTPUT_FILE" >> "$LOG_FILE" 2>/dev/null
fi

exit 0
