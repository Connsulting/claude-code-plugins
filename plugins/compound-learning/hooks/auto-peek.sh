#!/bin/bash

# Auto-peek learnings on UserPromptSubmit
# Uses Haiku to extract keywords, then searches SQLite
# Output is added as context Claude can see

# Bail if plugin root not set
if [ -z "$CLAUDE_PLUGIN_ROOT" ]; then
  exit 0
fi

# Setup logging
# Verify the dir is actually writable — a dangling symlink here will cause mkdir -p
# to succeed but every subsequent append to fail silently, breaking both logs and
# session dedup. When that happens, fall back to a temp dir and emit a loud warning.
LOG_DIR="$HOME/.claude/plugins/compound-learning"
mkdir -p "$LOG_DIR" 2>/dev/null
if [ ! -d "$LOG_DIR" ] || [ ! -w "$LOG_DIR" ]; then
  FALLBACK_LOG_DIR="${TMPDIR:-/tmp}/claude-compound-learning-fallback"
  mkdir -p "$FALLBACK_LOG_DIR" 2>/dev/null
  echo "[auto-peek] WARN: primary state dir $LOG_DIR is not writable (check for dangling symlink). Falling back to $FALLBACK_LOG_DIR — session dedup WILL NOT persist."
  LOG_DIR="$FALLBACK_LOG_DIR"
fi
LOG_FILE="$LOG_DIR/activity.log"

log_activity() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# Skip all hooks for subprocess calls (prevents recursion)
if [ -n "$CLAUDE_SUBPROCESS" ]; then
  exit 0
fi

# Surface indexing failures recorded by extract-learnings.sh. The async indexing
# hook can't echo to the live conversation, so it appends to this sentinel file
# and we surface it here on the next prompt — then clear so we don't re-warn.
INDEX_FAILURE_FLAG="$LOG_DIR/index-failures.log"
if [ -s "$INDEX_FAILURE_FLAG" ]; then
  FAIL_COUNT=$(wc -l < "$INDEX_FAILURE_FLAG" | tr -d ' ')
  FAIL_LATEST=$(tail -1 "$INDEX_FAILURE_FLAG")
  echo "[auto-peek] HEALTH: $FAIL_COUNT learning file(s) failed to index — most recent: $FAIL_LATEST. Run /compound-learning:index-learnings to retry. (full log: $LOG_FILE)"
  rm -f "$INDEX_FAILURE_FLAG"
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
# Co-locate under LOG_DIR so the fallback path above keeps sessions and logs together.
SESSIONS_DIR="$LOG_DIR/sessions"
mkdir -p "$SESSIONS_DIR" 2>/dev/null
if [ ! -w "$SESSIONS_DIR" ]; then
  log_activity "[auto-peek] WARN: sessions dir $SESSIONS_DIR not writable — dedup disabled this invocation"
fi

# Prune session files older than 24 hours
find "$SESSIONS_DIR" -name "*.seen" -mmin +1440 -delete 2>/dev/null

# Derive session ID from the transcript filename UUID
SESSION_ID=$(basename "$TRANSCRIPT" .jsonl)
SESSION_FILE="$SESSIONS_DIR/${SESSION_ID}.seen"

# Pinned learnings are loaded via CLAUDE.md @import (not injected here) so the
# agent sees them on every session without paying hook-output overhead. See
# scripts/build-pinned.py for the generator.

# Read already-seen IDs from the session file
EXCLUDE_IDS=""
EXCLUDE_COUNT=0
if [ -f "$SESSION_FILE" ]; then
  EXCLUDE_IDS=$(sort -u "$SESSION_FILE" | tr '\n' ',' | sed 's/,$//')
  EXCLUDE_COUNT=$(sort -u "$SESSION_FILE" | wc -l)
fi

# Extract recent conversation context from transcript
# This provides Haiku with context about what Claude was working on
CONTEXT=""
if [ -f "$TRANSCRIPT" ]; then
  ERR_FILE=$(mktemp)
  CONTEXT=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/extract-transcript-context.py" "$TRANSCRIPT" 3000 2>"$ERR_FILE")
  [ -s "$ERR_FILE" ] && log_activity "[auto-peek] context extraction error: $(cat "$ERR_FILE")"
  rm -f "$ERR_FILE"
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
# --safe-mode disables CLAUDE.md/AGENTS.md auto-discovery, plugins, hooks, and MCP.
# Without it, every keyword-extraction call loaded the full global memory (~25k tokens
# of CLAUDE.md + AGENTS.md + pinned.md) just to extract two keywords, paying cache
# creation at the 1h ephemeral rate on each fire (~$0.06/call). --safe-mode cuts that
# to ~$0.011/call (82% less); at hundreds of fires/day across concurrent sessions the
# loaded-memory tax was the dominant Haiku spend.
export CLAUDE_SUBPROCESS=1
ERR_FILE=$(mktemp)
KEYWORDS_JSON=$(timeout 15 claude -p \
  --no-session-persistence \
  --safe-mode \
  --model haiku \
  --output-format json \
  --mcp-config "$EMPTY_MCP" \
  --strict-mcp-config \
  "$HAIKU_PROMPT" 2>"$ERR_FILE")
[ -s "$ERR_FILE" ] && log_activity "[auto-peek] keyword extraction error: $(cat "$ERR_FILE")"
rm -f "$ERR_FILE"

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

log_activity "[auto-peek] keywords extracted: $KEYWORDS_DISPLAY (dedup excluding $EXCLUDE_COUNT prior IDs)"

# Search with extracted keywords in parallel, exclude seen IDs
# Each keyword is searched independently and results are merged
# HF_HUB_OFFLINE=1: the embedding model is already cached locally; this stops
# huggingface_hub from emitting an "unauthenticated requests" nag to stderr on
# every invocation, which would otherwise trip the stderr-as-error guard below.
ERR_FILE=$(mktemp)
SEARCH_RESULT=$(HF_HUB_OFFLINE=1 python3 "${CLAUDE_PLUGIN_ROOT}/scripts/search-learnings.py" \
  --peek \
  --max-results 1 \
  --exclude-ids "$EXCLUDE_IDS" \
  --keywords-json "$KEYWORDS_ARRAY" \
  "$CWD" 2>"$ERR_FILE")
SEARCH_EXIT=$?
# Filter benign third-party warnings (huggingface_hub / transformers nags) and
# the multi-line BertModel LOAD REPORT that sentence-transformers prints when
# loading a cached model. Strip ANSI escapes first so the line patterns match.
# Real exceptions still get raised as multi-line tracebacks with non-Warning
# prefixes.
SEARCH_ERR=""
if [ -s "$ERR_FILE" ]; then
  SEARCH_ERR=$(sed 's/\x1b\[[0-9;]*m//g' "$ERR_FILE" | tr '\r' '\n' | grep -v -E '^[[:space:]]*$|^(Warning:|/.*\.py:[0-9]+: (User|Future|Deprecation)Warning|Loading weights:|.*BertModel LOAD REPORT|Key[[:space:]]+\||[-]+\+[-]+|[a-zA-Z_.]+[[:space:]]+\|[[:space:]]+(UNEXPECTED|MISSING|OK|EXTRA)|Notes:|- (UNEXPECTED|MISSING|OK|EXTRA))' || true)
  [ -n "$SEARCH_ERR" ] && log_activity "[auto-peek] search error: $SEARCH_ERR"
fi
rm -f "$ERR_FILE"

# Surface failures visibly. The search subprocess catches per-keyword exceptions
# internally and exits 0 with empty results, so non-empty stderr (after filtering
# benign warnings above) is the canonical signal that something went wrong even
# when the exit code looks healthy.
if [ $SEARCH_EXIT -ne 0 ] || [ -n "$SEARCH_ERR" ]; then
  FIRST_ERR=$(echo "$SEARCH_ERR" | head -1)
  echo "[auto-peek] ERROR: search broken — ${FIRST_ERR:-exit $SEARCH_EXIT} (full log: $LOG_FILE)"
  exit 0
fi

STATUS=$(echo "$SEARCH_RESULT" | jq -r '.status' 2>/dev/null)
if [ "$STATUS" != "found" ]; then
  log_activity "[auto-peek] no results for: $KEYWORDS_DISPLAY"
  echo "[auto-peek] no learnings for: $KEYWORDS_DISPLAY"
  exit 0
fi

echo "$SEARCH_RESULT" | jq -r '.learnings[].id' 2>/dev/null >> "$SESSION_FILE"

# Format compact output for Claude's context
COUNT=$(echo "$SEARCH_RESULT" | jq -r '.count' 2>/dev/null)
echo "[auto-peek] $COUNT learning(s) found for: $KEYWORDS_DISPLAY"

# Log search results with details (include distance for threshold tuning)
log_activity "SEARCH: query='$KEYWORDS_DISPLAY' count=$COUNT"
echo "$SEARCH_RESULT" | jq -r '.learnings[] | "  file: \(.metadata.file_path | split("/") | .[-1])  distance: \(.distance // .original_distance // "?")  orig_distance: \(.original_distance // "?")  keyword_overlap: \(.keyword_overlap // "?")  title: \(.metadata.summary // .document | split("\n")[0] | .[0:100])  id: \(.id)"' 2>/dev/null | while read -r line; do
  log_activity "  RESULT: $line"
done

# Show compact summary: filename and first line of summary/title
echo "$SEARCH_RESULT" | jq -r '.learnings[] | "  -> " + (.metadata.file_path | split("/") | .[-1]) + ": " + (.metadata.summary // .document | split("\n")[0] | .[0:80])' 2>/dev/null

# Output full content for Claude to use
echo ""
echo "--- Relevant Learnings ---"
echo "$SEARCH_RESULT" | jq -r '.learnings[] | "[\(.id)]\n\(.document)\n"' 2>/dev/null
echo "(Briefly mention you found a stored learning on \"$KEYWORDS_DISPLAY\" that's relevant here. One sentence max, then continue normally.)"

exit 0
