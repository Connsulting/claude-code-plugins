#!/bin/bash

# Auto-log substantive sessions to Notion Work Log.
# Claude: SessionEnd plugin hook. Grok/Codex: thin wrappers that detach this script.

if [ -z "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  CLAUDE_PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
export CLAUDE_PLUGIN_ROOT

# Setup logging
LOG_DIR="$HOME/.claude/plugins/work-log"
mkdir -p "$LOG_DIR/locks"
LOG_FILE="$LOG_DIR/activity.log"

log_activity() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# Skip all hooks for subprocess calls (prevents recursion)
if [ -n "${CLAUDE_SUBPROCESS:-}" ]; then
  exit 0
fi

export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:$PATH"

# Read hook input from stdin (single jq call). Accept Claude snake_case and Grok camelCase.
INPUT=$(cat)
eval "$(printf '%s' "$INPUT" | jq -r '@sh "SESSION_ID=\(.session_id // .sessionId // "") TRANSCRIPT=\(.transcript_path // .transcriptPath // "") CWD=\(.cwd // .workspaceRoot // "") SUBAGENT=\(.subagent_type // .subagentType // "")"')"

if [ -n "$SUBAGENT" ]; then
  exit 0
fi

ENGINE="${WORK_LOG_ENGINE:-}"
if [ -z "$ENGINE" ]; then
  if [ -n "${GROK_HOOK_EVENT:-}${GROK_SESSION_ID:-}" ]; then
    ENGINE=grok
  elif [ -n "${CODEX_THREAD_ID:-}" ]; then
    ENGINE=codex
  else
    ENGINE=claude
  fi
fi

# Expand ~ in transcript path
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

# Grok SessionEnd may omit transcriptPath; chat_history.jsonl is the conversation.
if [ ! -s "$TRANSCRIPT" ] && [ "$ENGINE" = grok ] && [ -n "$SESSION_ID" ] && [ -n "$CWD" ]; then
  ENC_CWD=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$CWD")
  CANDIDATE="$HOME/.grok/sessions/${ENC_CWD}/${SESSION_ID}/chat_history.jsonl"
  if [ -s "$CANDIDATE" ]; then
    TRANSCRIPT="$CANDIDATE"
  fi
fi

# Quick size check before any config reads
LINES=$(head -n 50 "$TRANSCRIPT" 2>/dev/null | wc -l)
LINES=${LINES// /}

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

if [ -n "${WORK_LOG_SOURCE_PREFIX:-}" ]; then
  SOURCE_PREFIX="$WORK_LOG_SOURCE_PREFIX"
else
  case "$ENGINE" in
    grok) SOURCE_PREFIX=gx ;;
    codex) SOURCE_PREFIX=cx ;;
  esac
fi

if [ "${LINES:-0}" -lt "$MIN_LINES" ]; then
  log_activity "[$ENGINE] SKIP: transcript too short ($LINES lines, minimum $MIN_LINES)"
  exit 0
fi

# One writer per session so Claude/Grok/Codex wrappers cannot double-fire.
LOCK_FILE="$LOG_DIR/locks/${SESSION_ID}.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log_activity "[$ENGINE] SKIP: already logging session=$SESSION_ID"
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
  log_activity "[$ENGINE] SKIP: empty transcript content"
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

# Look up session name from Claude sessions-index.json, else Grok summary.json
ENCODED_CWD=$(echo "$CWD" | sed 's|/|-|g')
SESSIONS_INDEX="$HOME/.claude/projects/${ENCODED_CWD}/sessions-index.json"
SESSION_NAME=""
if [ -f "$SESSIONS_INDEX" ]; then
  SESSION_NAME=$(jq -r --arg id "$SESSION_ID" '
    .entries[] | select(.sessionId == $id) |
    .customTitle // .summary // empty
  ' "$SESSIONS_INDEX" 2>/dev/null | head -1)
fi
if [ -z "$SESSION_NAME" ] && [ "$ENGINE" = grok ] && [ -n "$SESSION_ID" ] && [ -n "$CWD" ]; then
  ENC_CWD=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$CWD")
  GROK_SUMMARY="$HOME/.grok/sessions/${ENC_CWD}/${SESSION_ID}/summary.json"
  if [ -f "$GROK_SUMMARY" ]; then
    SESSION_NAME=$(jq -r '.generated_title // .session_summary // empty' "$GROK_SUMMARY" 2>/dev/null | head -1)
  fi
fi

# Compute session tag and timestamp
SESSION_TAG="${SOURCE_PREFIX}:${SESSION_ID:0:8}"
TODAY=$(TZ="$TIMEZONE" date +%Y-%m-%d)
TIMESTAMP=$(TZ="$TIMEZONE" date +'%I:%M %p')

# Capture output to log file generation
OUTPUT_FILE=$(mktemp)
trap "rm -f $OUTPUT_FILE $TEMP_CONFIG" EXIT

CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"
if [ -z "$CLAUDE_BIN" ] && [ -x "$HOME/.local/bin/claude" ]; then
  CLAUDE_BIN="$HOME/.local/bin/claude"
fi
if [ -z "$CLAUDE_BIN" ]; then
  log_activity "[$ENGINE] WORK_LOG_END: session=$SESSION_ID exit=127 claude: command not found"
  exit 0
fi

log_activity "[$ENGINE] WORK_LOG_START: session=$SESSION_ID project=$PROJECT tag=$SESSION_TAG"

# Use heredoc to pass prompt via stdin (avoids temp files and arg size limits)
NOTION_TOOLS="mcp__${MCP_SERVER}__notion-search,mcp__${MCP_SERVER}__notion-fetch,mcp__${MCP_SERVER}__notion-create-pages,mcp__${MCP_SERVER}__notion-update-page,mcp__${MCP_SERVER}__notion-create-comment,mcp__${MCP_SERVER}__notion-get-comments"
CLAUDE_SUBPROCESS=1 ENABLE_CLAUDEAI_MCP_SERVERS=true "$CLAUDE_BIN" -p --no-session-persistence \
  --model sonnet \
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

## Skip criteria

SKIP these sessions (output "SKIP" and a reason, then stop):
- Routine automation runs (Zapier syncs, auto-management, scheduled tasks, cron triggers)
- Quick lookups, single questions, typos, dependency bumps, config tweaks
- Sessions that are entirely tool/agent review cycles with no new decisions
- Repetitive operational work identical to previous sessions (e.g., the same automation script running again)

## Write the summary

You are writing for a strategic analyst who reviews these notes weekly. They need to understand WHAT happened, WHY it matters, and WHAT is UNRESOLVED. They do not need implementation details.

For each session note, answer these three questions:

1. WHAT: What was accomplished? Be specific (name the feature, bug, system, or decision) but skip implementation mechanics (no PR numbers, file paths, or code details unless they ARE the point).

2. SO WHAT: Why does this matter beyond the immediate task? Connect to one of:
   - Client relationship (was this requested? does it change perception? does it demonstrate a capability?)
   - Systemic issue (does this reveal a pattern, a process gap, or an engineering culture problem?)
   - Delivery milestone (does this unlock something, complete a phase, or ship to users?)
   - Strategic thread (does this connect to positioning, a thesis, or cross-client pattern?)
   If none of these apply, just state the direct project impact.

3. OPEN THREADS (optional): Unresolved questions, follow-ups needed, or things that broke that haven't been fixed. Only include if genuinely unresolved.

Bad example: "Fixed stale useRef bug in opportunity publish button. Updated component to use callback ref pattern for reliable DOM tracking."
Good example: "Fixed publish button regression reported by RS client. Root cause was a React ref lifecycle issue introduced during the multi-agent review refactor -- worth watching whether the review pipeline is introducing more bugs than it catches."

If ticket/issue IDs appear in the transcript, include them as references.

## Toggle format

The Claude.ai Notion MCP uses enhanced Markdown. Toggles use HTML details/summary tags:

<details>
<summary>TOGGLE LABEL HERE</summary>
	CONTENT INDENTED WITH TAB
	SECOND LINE INDENTED WITH TAB
</details>

The toggle label format is: ${SESSION_TAG} - {Short Name}
- If Session Name above is not "none", use it as the short name
- Otherwise, generate a descriptive 3-5 word name from the transcript

When searching for an existing toggle (for resume/dedup), match on "${SESSION_TAG}" as a prefix only.

## Log to Notion

1. Fetch database ${DATABASE_ID} to get its data source URL (collection://...) and title property name. Search that data source (data_source_url) for a page titled "${TODAY}". If missing, create it with notion-create-pages using parent {"type": "data_source_id", "data_source_id": "<that collection id>"} and the title property set to "${TODAY}".
   NEVER call notion-create-pages without that data source parent: omitting the parent creates an untitled private page at the top level of the workspace, outside the database. If you cannot resolve the data source, output "SKIP: could not resolve database" and stop.
2. Fetch the page blocks. Look for an H2 "${PROJECT}" and a toggle starting with "${SESSION_TAG}".
3. If no "${PROJECT}" H2 exists, append one.
4. If a toggle starting with "${SESSION_TAG}" exists (resumed session):
   a. If the toggle label differs, update the title text
   b. Append inside it: [${TIMESTAMP}] (resumed): {summary}
5. Otherwise, append a new toggle containing:
   [${TIMESTAMP}] {WHAT summary}
   {SO WHAT context}
   {OPEN THREADS if any}

IMPORTANT: Only write the toggle content to Notion. No evaluation reasoning or meta-commentary.

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
    log_activity "[$ENGINE] WORK_LOG_SKIP: $REASON"
  else
    log_activity "[$ENGINE] WORK_LOG_END: session=$SESSION_ID exit=$EXIT_CODE"
  fi
  # Log first few lines of output
  head -5 "$OUTPUT_FILE" >> "$LOG_FILE" 2>/dev/null
fi

exit 0
