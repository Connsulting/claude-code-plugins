#!/bin/bash

# Auto-peek learnings on UserPromptSubmit
# Uses Haiku to extract keywords, then searches SQLite
# Output is added as context Claude can see

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/observability.sh" || exit 0

hook_log_init "auto-peek"
SESSION_ID="${CLAUDE_SESSION_ID:-}"
hook_set_session_context "$SESSION_ID"
HOOK_OUTCOME="success"
HOOK_OUTCOME_MESSAGE=""
RESULT_COUNT=0
KEYWORD_COUNT=0
EMPTY_MCP=""

auto_peek_finalize() {
  if [ -n "$EMPTY_MCP" ] && [ -f "$EMPTY_MCP" ]; then
    rm -f "$EMPTY_MCP"
  fi
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
    --duration-ms "$duration_ms" \
    --session-id "$SESSION_ID" \
    --message "$HOOK_OUTCOME_MESSAGE" \
    --counts-json "{\"result_count\":$RESULT_COUNT,\"keyword_count\":$KEYWORD_COUNT}"
}
trap auto_peek_finalize EXIT

hook_obs_event "info" "hook_start" "start" --session-id "$SESSION_ID"

# Bail if plugin root not set
if [ -z "$CLAUDE_PLUGIN_ROOT" ]; then
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="CLAUDE_PLUGIN_ROOT not set"
  hook_obs_event "info" "skip_reason" "skipped" --message "plugin root not set" --session-id "$SESSION_ID"
  exit 0
fi

# Skip all hooks for subprocess calls (prevents recursion)
if [ -n "$CLAUDE_SUBPROCESS" ]; then
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="Skipping in subprocess context"
  hook_obs_event "info" "skip_reason" "skipped" --message "CLAUDE_SUBPROCESS is set" --session-id "$SESSION_ID"
  exit 0
fi

# Read hook input from stdin
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt')
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path')
SESSION_FROM_INPUT=$(echo "$INPUT" | jq -r '.session_id // empty')
if [ -n "$SESSION_FROM_INPUT" ] && [ "$SESSION_FROM_INPUT" != "null" ]; then
  SESSION_ID="$SESSION_FROM_INPUT"
  hook_set_session_context "$SESSION_ID"
fi
if [ -z "$TRANSCRIPT" ] || [ "$TRANSCRIPT" = "null" ]; then
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="No transcript path provided"
  hook_obs_event "info" "skip_reason" "skipped" --message "missing transcript path" --session-id "$SESSION_ID"
  exit 0
fi
CWD=$(echo "$INPUT" | jq -r '.cwd')

# Skip empty or very short prompts
if [ ${#PROMPT} -lt 10 ]; then
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="Prompt too short"
  hook_obs_event "info" "skip_reason" "skipped" --message "prompt shorter than 10 characters" --session-id "$SESSION_ID"
  exit 0
fi

# Expand ~ in transcript path
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

# Session-scoped deduplication via a plain-text state file.
SESSIONS_DIR="$HOME/.claude/plugins/compound-learning/sessions"
mkdir -p "$SESSIONS_DIR"

# Prune session files older than 24 hours
find "$SESSIONS_DIR" -name "*.seen" -mmin +1440 -delete 2>/dev/null

# Use transcript filename for dedupe state; keep observability session attribution stable.
SESSION_STATE_ID=$(basename "$TRANSCRIPT" .jsonl)
if [ -z "$SESSION_ID" ] && [ -n "$SESSION_STATE_ID" ]; then
  SESSION_ID="$SESSION_STATE_ID"
  hook_set_session_context "$SESSION_ID"
fi
SESSION_FILE="$SESSIONS_DIR/${SESSION_STATE_ID}.seen"

# Read already-seen IDs from the session file
EXCLUDE_IDS=""
if [ -f "$SESSION_FILE" ]; then
  EXCLUDE_IDS=$(sort -u "$SESSION_FILE" | tr '\n' ',' | sed 's/,$//')
fi

# Extract recent conversation context from transcript
# This provides Haiku with context about what Claude was working on
CONTEXT=""
if [ -f "$TRANSCRIPT" ]; then
  context_started="$(hook_now_ms)"
  ERR_FILE=$(mktemp)
  CONTEXT=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/extract-transcript-context.py" "$TRANSCRIPT" 3000 2>"$ERR_FILE")
  context_exit=$?
  context_duration=$(( $(hook_now_ms) - context_started ))
  if [ -s "$ERR_FILE" ]; then
    hook_log_activity "[auto-peek] context extraction error: $(cat "$ERR_FILE")"
  fi
  if [ "$context_exit" -eq 0 ]; then
    hook_obs_event "debug" "subprocess_exit" "success" \
      --session-id "$SESSION_ID" \
      --duration-ms "$context_duration" \
      --counts-json '{"command":"extract_transcript_context","exit_code":0}'
  else
    hook_obs_event "warn" "subprocess_exit" "failure" \
      --session-id "$SESSION_ID" \
      --duration-ms "$context_duration" \
      --counts-json "{\"command\":\"extract_transcript_context\",\"exit_code\":$context_exit}"
  fi
  rm -f "$ERR_FILE"
fi

# Create empty MCP config to prevent LSP/MCP overhead
EMPTY_MCP=$(mktemp)
echo '{"mcpServers":{}}' > "$EMPTY_MCP"

# Build the Haiku prompt with optional context
if [ -n "$CONTEXT" ]; then
  HAIKU_PROMPT="Extract 1-2 keywords for a knowledge base search. Use context to understand what user means, but extract keywords for what they're ASKING about.

Rules:
- Max 2 keywords, each 1-2 words only
- Focus on the core technical topic
- Examples: \"kubernetes\", \"prompt caching\", \"git rebase\", \"docker\"

Context: $CONTEXT

User asks: $PROMPT

JSON only: {\"keywords\": [\"keyword1\", \"keyword2\"]}"
else
  HAIKU_PROMPT="Extract 1-2 technical keywords from this prompt.

Rules:
- Max 2 keywords, each 1-2 words only
- Focus on the core technical topic
- Examples: \"kubernetes\", \"prompt caching\", \"git rebase\", \"docker\"

Prompt: $PROMPT

JSON only: {\"keywords\": [\"keyword1\", \"keyword2\"]}"
fi

# Use Haiku to extract search keywords
# This is fast because: no MCP, no tools, just prompt/response
# Use timeout to prevent hook from hanging
export CLAUDE_SUBPROCESS=1
keyword_started="$(hook_now_ms)"
ERR_FILE=$(mktemp)
KEYWORDS_JSON=$(timeout 15 claude -p \
  --no-session-persistence \
  --model haiku \
  --output-format json \
  --mcp-config "$EMPTY_MCP" \
  --strict-mcp-config \
  "$HAIKU_PROMPT" 2>"$ERR_FILE")
keyword_exit=$?
keyword_duration=$(( $(hook_now_ms) - keyword_started ))
if [ -s "$ERR_FILE" ]; then
  hook_log_activity "[auto-peek] keyword extraction error: $(cat "$ERR_FILE")"
fi
if [ "$keyword_exit" -eq 0 ]; then
  hook_obs_event "info" "subprocess_exit" "success" \
    --session-id "$SESSION_ID" \
    --duration-ms "$keyword_duration" \
    --counts-json '{"command":"haiku_keyword_extract","exit_code":0}'
else
  hook_obs_event "warn" "subprocess_exit" "failure" \
    --session-id "$SESSION_ID" \
    --duration-ms "$keyword_duration" \
    --counts-json "{\"command\":\"haiku_keyword_extract\",\"exit_code\":$keyword_exit}"
fi
rm -f "$ERR_FILE"

# Parse keywords from Haiku response (handles markdown-wrapped JSON)
# Haiku may return: {"result": "```json\n{\"keywords\": [...]}\n```"}
# Use pure jq to strip markdown fences and parse JSON
# Limit to first 2 keywords, keep as JSON array for parallel search
KEYWORDS_ARRAY=$(echo "$KEYWORDS_JSON" | jq -c '.result // empty | gsub("```json"; "") | gsub("```"; "") | fromjson | .keywords // [] | .[0:2]' 2>/dev/null)
KEYWORDS_DISPLAY=$(echo "$KEYWORDS_ARRAY" | jq -r 'join(", ")' 2>/dev/null)

# If no keywords extracted, skip search
if [ -z "$KEYWORDS_ARRAY" ] || [ "$KEYWORDS_ARRAY" = "null" ] || [ "$KEYWORDS_ARRAY" = "[]" ]; then
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="No keywords extracted"
  hook_obs_event "info" "skip_reason" "skipped" \
    --session-id "$SESSION_ID" \
    --message "keyword extraction produced no keywords"
  exit 0
fi

KEYWORD_COUNT=$(echo "$KEYWORDS_ARRAY" | jq -r 'length' 2>/dev/null)
KEYWORD_COUNT="${KEYWORD_COUNT:-0}"
hook_log_activity "[auto-peek] keywords extracted: $KEYWORDS_DISPLAY"
hook_obs_event "info" "keyword_extract" "success" \
  --session-id "$SESSION_ID" \
  --counts-json "{\"keywords\":$KEYWORD_COUNT}"

# Search with extracted keywords in parallel, exclude seen IDs
# Each keyword is searched independently and results are merged
ERR_FILE=$(mktemp)
search_started="$(hook_now_ms)"
SEARCH_RESULT=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/search-learnings.py" \
  --peek \
  --max-results 2 \
  --exclude-ids "$EXCLUDE_IDS" \
  --keywords-json "$KEYWORDS_ARRAY" \
  "$CWD" 2>"$ERR_FILE")
SEARCH_EXIT=$?
search_duration=$(( $(hook_now_ms) - search_started ))
if [ -s "$ERR_FILE" ]; then
  hook_log_activity "[auto-peek] search error: $(cat "$ERR_FILE")"
fi
if [ "$SEARCH_EXIT" -eq 0 ]; then
  hook_obs_event "info" "subprocess_exit" "success" \
    --session-id "$SESSION_ID" \
    --duration-ms "$search_duration" \
    --counts-json '{"command":"search_learnings","exit_code":0}'
else
  hook_obs_event "error" "subprocess_exit" "failure" \
    --session-id "$SESSION_ID" \
    --duration-ms "$search_duration" \
    --counts-json "{\"command\":\"search_learnings\",\"exit_code\":$SEARCH_EXIT}"
fi
rm -f "$ERR_FILE"

# Check if we got results
if [ $SEARCH_EXIT -ne 0 ]; then
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="search-learnings.py failed"
  echo "[auto-peek] search failed (check ~/.claude/plugins/compound-learning/activity.log)"
  exit 0
fi

STATUS=$(echo "$SEARCH_RESULT" | jq -r '.status' 2>/dev/null)
if [ "$STATUS" != "found" ]; then
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="No relevant learnings found"
  hook_obs_event "info" "skip_reason" "skipped" \
    --session-id "$SESSION_ID" \
    --message "search status $STATUS"
  hook_log_activity "[auto-peek] no results for: $KEYWORDS_DISPLAY"
  echo "[auto-peek] no learnings for: $KEYWORDS_DISPLAY"
  exit 0
fi

echo "$SEARCH_RESULT" | jq -r '.learnings[].id' 2>/dev/null >> "$SESSION_FILE"

# Format compact output for Claude's context
COUNT=$(echo "$SEARCH_RESULT" | jq -r '.count' 2>/dev/null)
RESULT_COUNT="${COUNT:-0}"
echo "[auto-peek] $COUNT learning(s) found for: $KEYWORDS_DISPLAY"

# Log search results with details
hook_log_activity "SEARCH: query='$KEYWORDS_DISPLAY' count=$COUNT"
echo "$SEARCH_RESULT" | jq -r '.learnings[] | "  file: \(.metadata.file_path | split("/") | .[-1])  title: \(.metadata.summary // .document | split("\n")[0] | .[0:100])  id: \(.id)"' 2>/dev/null | while read -r line; do
  hook_log_activity "  RESULT: $line"
done

hook_obs_event "info" "search_result" "found" \
  --session-id "$SESSION_ID" \
  --counts-json "{\"result_count\":$RESULT_COUNT}"

# Show compact summary: filename and first line of summary/title
echo "$SEARCH_RESULT" | jq -r '.learnings[] | "  -> " + (.metadata.file_path | split("/") | .[-1]) + ": " + (.metadata.summary // .document | split("\n")[0] | .[0:80])' 2>/dev/null

# Output full content for Claude to use
echo ""
echo "--- Relevant Learnings ---"
echo "$SEARCH_RESULT" | jq -r '.learnings[] | "[\(.id)]\n\(.document)\n"' 2>/dev/null
echo "(Briefly mention you found a stored learning on \"$KEYWORDS_DISPLAY\" that's relevant here. One sentence max, then continue normally.)"

exit 0
