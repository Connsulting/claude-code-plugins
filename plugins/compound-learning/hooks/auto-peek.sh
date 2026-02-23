#!/bin/bash

# Auto-peek learnings on UserPromptSubmit
# Uses Haiku to extract keywords, then searches SQLite
# Output is added as context Claude can see

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
PROMPT=$(echo "$INPUT" | jq -r '.prompt')
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path')
if [ -z "$TRANSCRIPT" ] || [ "$TRANSCRIPT" = "null" ]; then
  exit 0
fi
CWD=$(echo "$INPUT" | jq -r '.cwd')

# Skip empty or very short prompts
if [ ${#PROMPT} -lt 10 ]; then
  exit 0
fi

# Expand ~ in transcript path
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

# Session-scoped deduplication via a plain-text state file.
SESSIONS_DIR="$HOME/.claude/plugins/compound-learning/sessions"
mkdir -p "$SESSIONS_DIR"

# Prune session files older than 24 hours
find "$SESSIONS_DIR" -name "*.seen" -mmin +1440 -delete 2>/dev/null

# Derive session ID from the transcript filename UUID
SESSION_ID=$(basename "$TRANSCRIPT" .jsonl)
SESSION_FILE="$SESSIONS_DIR/${SESSION_ID}.seen"

# Read already-seen IDs from the session file
EXCLUDE_IDS=""
if [ -f "$SESSION_FILE" ]; then
  EXCLUDE_IDS=$(sort -u "$SESSION_FILE" | tr '\n' ',' | sed 's/,$//')
fi

# Extract recent conversation context from transcript
# This provides Haiku with context about what Claude was working on
CONTEXT=""
if [ -f "$TRANSCRIPT" ]; then
  CONTEXT=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/extract-transcript-context.py" "$TRANSCRIPT" 3000 2>/dev/null)
fi

# Create empty MCP config to prevent LSP/MCP overhead
EMPTY_MCP=$(mktemp)
echo '{"mcpServers":{}}' > "$EMPTY_MCP"
trap "rm -f $EMPTY_MCP" EXIT

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
KEYWORDS_JSON=$(timeout 15 claude -p \
  --no-session-persistence \
  --model haiku \
  --output-format json \
  --mcp-config "$EMPTY_MCP" \
  --strict-mcp-config \
  "$HAIKU_PROMPT" 2>/dev/null)

# Parse keywords from Haiku response (handles markdown-wrapped JSON)
# Haiku may return: {"result": "```json\n{\"keywords\": [...]}\n```"}
# Use pure jq to strip markdown fences and parse JSON
# Limit to first 2 keywords, keep as JSON array for parallel search
RESULT_FIELD=$(echo "$KEYWORDS_JSON" | jq -r '.result // empty' 2>/dev/null)
KEYWORDS_ARRAY=$(echo "$KEYWORDS_JSON" | jq -c '.result // empty | gsub("```json"; "") | gsub("```"; "") | fromjson | .keywords // [] | .[0:2]' 2>/dev/null)
KEYWORDS_DISPLAY=$(echo "$KEYWORDS_ARRAY" | jq -r 'join(", ")' 2>/dev/null)

# If no keywords extracted, skip search
if [ -z "$KEYWORDS_ARRAY" ] || [ "$KEYWORDS_ARRAY" = "null" ] || [ "$KEYWORDS_ARRAY" = "[]" ]; then
  exit 0
fi

# Search with extracted keywords in parallel, exclude seen IDs
# Each keyword is searched independently and results are merged
SEARCH_RESULT=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/search-learnings.py" \
  --peek \
  --max-results 2 \
  --exclude-ids "$EXCLUDE_IDS" \
  --keywords-json "$KEYWORDS_ARRAY" \
  "$CWD" 2>/dev/null)

# Check if we got results
if [ $? -ne 0 ]; then
  echo "[auto-peek] search failed"
  exit 0
fi

STATUS=$(echo "$SEARCH_RESULT" | jq -r '.status' 2>/dev/null)
if [ "$STATUS" != "found" ]; then
  echo "[auto-peek] no learnings for: $KEYWORDS_DISPLAY"
  exit 0
fi

echo "$SEARCH_RESULT" | jq -r '.learnings[].id' 2>/dev/null >> "$SESSION_FILE"

# Format compact output for Claude's context
COUNT=$(echo "$SEARCH_RESULT" | jq -r '.count' 2>/dev/null)
echo "[auto-peek] $COUNT learning(s) found for: $KEYWORDS_DISPLAY"

# Log search results with details
log_activity "SEARCH: query='$KEYWORDS_DISPLAY' count=$COUNT"
echo "$SEARCH_RESULT" | jq -r '.learnings[] | "  file: \(.metadata.file_path | split("/") | .[-1])  title: \(.metadata.summary // .document | split("\n")[0] | .[0:100])  id: \(.id)"' 2>/dev/null | while read -r line; do
  log_activity "  RESULT: $line"
done

# Show compact summary: filename and first line of summary/title
echo "$SEARCH_RESULT" | jq -r '.learnings[] | "  -> " + (.metadata.file_path | split("/") | .[-1]) + ": " + (.metadata.summary // .document | split("\n")[0] | .[0:80])' 2>/dev/null

# Output full content for Claude to use
echo ""
echo "--- Relevant Learnings ---"
echo "$SEARCH_RESULT" | jq -r '.learnings[] | "[\(.id)]\n\(.document)\n"' 2>/dev/null

exit 0
