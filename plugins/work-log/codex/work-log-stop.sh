#!/bin/bash

# Codex Stop hook for work-log
# Mirrors the Claude Code SessionEnd hook behavior for Codex sessions.
# Reads config from ~/.claude/plugins/work-log/config.json (shared with Claude Code plugin).

LOG_DIR="$HOME/.claude/plugins/work-log"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/activity.log"

log_activity() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [codex] $1" >> "$LOG_FILE"
}

# Find the most recent Codex session file
LATEST=$(find ~/.codex/sessions -name "rollout-*.jsonl" -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
[ -z "$LATEST" ] && exit 0

# Extract session ID from session_meta in the JSONL
SESSION_ID=$(jq -r 'select(.type == "session_meta") | .payload.id' "$LATEST" 2>/dev/null | head -1)
[ -z "$SESSION_ID" ] && exit 0

# Session dedup: skip if already logged (Stop fires per-turn)
STATE_DIR="$LOG_DIR/codex-sessions"
mkdir -p "$STATE_DIR"
find "$STATE_DIR" -name "*.logged" -mtime +1 -delete 2>/dev/null
[ -f "$STATE_DIR/${SESSION_ID}.logged" ] && exit 0

# Check session size
LINES=$(wc -l < "$LATEST" 2>/dev/null || echo "0")
if [ "$LINES" -lt 20 ]; then
  exit 0
fi

# Read config (same user config as Claude Code plugin)
CONFIG="$LOG_DIR/config.json"
if [ ! -f "$CONFIG" ]; then
  log_activity "SKIP: no config at $CONFIG"
  exit 0
fi
eval "$(jq -r '@sh "DATABASE_ID=\(.notion.databaseId) MCP_SERVER=\(.notion.mcpServerName // "claude_ai_Notion") TIMEZONE=\(.timezone // "UTC") DEFAULT_PROJECT=\(.defaultProject // "personal") PROJECT_PATTERN=\(.projectPattern // ".*/git/([^/]+).*")"' "$CONFIG")"

if [ "$DATABASE_ID" = "YOUR_DATABASE_ID_HERE" ] || [ -z "$DATABASE_ID" ]; then
  log_activity "SKIP: database ID not configured"
  exit 0
fi

# Get cwd from session meta
CWD=$(jq -r 'select(.type == "session_meta") | .payload.cwd' "$LATEST" 2>/dev/null | head -1)

# Extract project
PROJECT=$(echo "$CWD" | sed -nE "s|${PROJECT_PATTERN}|\1|p")
if [ -n "$PROJECT" ]; then
  MAPPED=$(jq -r --arg p "$PROJECT" '.projectMappings[$p] // empty' "$CONFIG" 2>/dev/null)
  [ -n "$MAPPED" ] && PROJECT="$MAPPED"
fi
: "${PROJECT:=$DEFAULT_PROJECT}"

# Extract transcript (user and assistant messages, truncated to ~80KB)
TRANSCRIPT_CONTENT=$(jq -r '
  select(.type == "response_item")
  | select(.payload.role == "user" or .payload.role == "assistant")
  | "[" + (.payload.role | ascii_upcase) + "]: " + (
      [.payload.content[]? | select(.type == "input_text" or .type == "output_text") | .text // empty]
      | join("\n")
    )
' "$LATEST" 2>/dev/null | tail -c 80000)

[ -z "$TRANSCRIPT_CONTENT" ] && exit 0

SESSION_TAG="cx:${SESSION_ID:0:8}"
TODAY=$(TZ="$TIMEZONE" date +%Y-%m-%d)
TIMESTAMP=$(TZ="$TIMEZONE" date +'%I:%M %p')

# Mark as logged before spawning (prevent double-fire from per-turn Stop)
touch "$STATE_DIR/${SESSION_ID}.logged"

log_activity "WORK_LOG_START: session=$SESSION_ID project=$PROJECT tag=$SESSION_TAG"

# Fire and forget: invoke Claude Code headless for analysis + Notion write
NOTION_TOOLS="mcp__${MCP_SERVER}__notion-search,mcp__${MCP_SERVER}__notion-fetch,mcp__${MCP_SERVER}__notion-create-pages,mcp__${MCP_SERVER}__notion-update-page,mcp__${MCP_SERVER}__notion-create-comment,mcp__${MCP_SERVER}__notion-get-comments"
CLAUDE_SUBPROCESS=1 ENABLE_CLAUDEAI_MCP_SERVERS=true claude -p --no-session-persistence \
  --permission-mode bypassPermissions \
  --allowedTools "Read,ToolSearch,${NOTION_TOOLS}" \
  <<PROMPT_END >/dev/null 2>&1 &
You are a work log assistant. Evaluate this Codex coding session and, if substantive, log it to Notion.

Session Tag: ${SESSION_TAG}
Project: ${PROJECT}
Date: ${TODAY}
Time: ${TIMESTAMP}
Database ID: ${DATABASE_ID}

## Evaluate

Skip trivial sessions (quick lookups, single questions, typos, meta-config). Log sessions with meaningful work (features, fixes, architecture, planning, infra).

If not worth logging, do nothing and exit.

## Write the summary

Focus on outcomes and value delivered, not technical implementation details:
- What was accomplished? What concrete work product was delivered?
- Why does it matter for the project? What problem does it solve or what capability does it add?
- Include specific details: what was built, fixed, or decided. Avoid vague language.
- If ticket/issue IDs appear in the transcript (JIRA like PROJ-1234, Linear, GitHub #123), include them as references.

## Log to Notion

1. Search database ${DATABASE_ID} for a page titled "${TODAY}". Create one if missing.
2. Fetch the page blocks. Look for an H2 "${PROJECT}" and a toggle "${SESSION_TAG}".
3. If no "${PROJECT}" H2 exists, append one.
4. If a toggle "${SESSION_TAG}" exists (resumed session), append inside it:
   [${TIMESTAMP}] codex: {summary}
5. Otherwise, append a new toggle with label "${SESSION_TAG}" containing:
   [${TIMESTAMP}] codex: {1-2 sentence summary with ticket refs if any}
   {1 sentence on value to the project}

If toggles are unsupported, use H3 + paragraph as fallback. Prefer duplicates over data loss.

<transcript>
${TRANSCRIPT_CONTENT}
</transcript>
PROMPT_END

log_activity "WORK_LOG_SPAWNED: session=$SESSION_ID (backgrounded)"
exit 0
