#!/bin/bash

# Auto-peek learnings on UserPromptSubmit
# Uses Haiku to extract keywords, then searches ChromaDB
# Output is added as context Claude can see

# Bail if plugin root not set
if [ -z "$CLAUDE_PLUGIN_ROOT" ]; then
  exit 0
fi

# Prevent recursive execution from our own claude -p call
if [ -n "$CLAUDE_HOOK_PEEKING" ]; then
  exit 0
fi

# Read hook input from stdin
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt')
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path')
CWD=$(echo "$INPUT" | jq -r '.cwd')

# Skip empty or very short prompts
if [ ${#PROMPT} -lt 10 ]; then
  exit 0
fi

# Expand ~ in transcript path
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

# Extract learning IDs already shown in this session from transcript
# Learning IDs appear as "id": "scope/topic-name" in search results
EXCLUDE_IDS=""
if [ -f "$TRANSCRIPT" ]; then
  EXCLUDE_IDS=$(grep -oE '"id":\s*"[^"]+"' "$TRANSCRIPT" 2>/dev/null | \
    grep -oE '"[^"]+/[^"]+"' | tr -d '"' | sort -u | tr '\n' ',' | sed 's/,$//')
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
  HAIKU_PROMPT="Extract 1-3 technical keywords from this conversation for a developer knowledge base search. Topics: aws, kubernetes, security, database, debugging, deployment, nodejs, git, docker, python.

Recent context:
$CONTEXT

User message: $PROMPT

Return JSON only: {\"keywords\": [\"word1\", \"word2\"]} or {\"keywords\": []}"
else
  HAIKU_PROMPT="Extract 1-3 technical keywords from this prompt for a developer knowledge base search. Topics: aws, kubernetes, security, database, debugging, deployment, nodejs, git, docker, python.

Prompt: $PROMPT

Return JSON only: {\"keywords\": [\"word1\", \"word2\"]} or {\"keywords\": []}"
fi

# Use Haiku to extract search keywords
# This is fast because: no MCP, no tools, just prompt/response
# Use timeout to prevent hook from hanging
export CLAUDE_HOOK_PEEKING=1
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
KEYWORDS=$(echo "$KEYWORDS_JSON" | jq -r '.result // empty | gsub("```json"; "") | gsub("```"; "") | fromjson | .keywords // [] | join(" ")' 2>/dev/null)

# If no keywords extracted, skip search
if [ -z "$KEYWORDS" ] || [ "$KEYWORDS" = "null" ]; then
  exit 0
fi

# Search with extracted keywords, exclude seen IDs
# Threshold 0.5 is permissive since exclude-ids prevents duplicates
SEARCH_RESULT=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/search-learnings.py" \
  --peek \
  --threshold 0.5 \
  --max-results 2 \
  --exclude-ids "$EXCLUDE_IDS" \
  "$KEYWORDS" \
  "$CWD" 2>/dev/null)

# Check if we got results
if [ $? -ne 0 ]; then
  echo "[auto-peek] search failed"
  exit 0
fi

STATUS=$(echo "$SEARCH_RESULT" | jq -r '.status' 2>/dev/null)
if [ "$STATUS" != "found" ]; then
  echo "[auto-peek] no learnings for: $KEYWORDS"
  exit 0
fi

# Format compact output for Claude's context
COUNT=$(echo "$SEARCH_RESULT" | jq -r '.count' 2>/dev/null)
echo "[auto-peek] $COUNT learning(s) found for: $KEYWORDS"

# Show compact summary: filename and first line of summary/title
echo "$SEARCH_RESULT" | jq -r '.learnings[] | "  -> " + (.metadata.file_path | split("/") | .[-1]) + ": " + (.metadata.summary // .document | split("\n")[0] | .[0:80])' 2>/dev/null

# Output full content for Claude to use
echo ""
echo "--- Relevant Learnings ---"
echo "$SEARCH_RESULT" | jq -r '.learnings[] | "[\(.id)]\n\(.document)\n"' 2>/dev/null

exit 0
