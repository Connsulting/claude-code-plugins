#!/bin/bash
# Codex UserPromptSubmit -> compound-learning auto-peek.
# Reads the Codex hook payload, converts the rollout to a Claude transcript,
# rewrites transcript_path to the converted file, and delegates to the plugin's
# auto-peek.sh. The bridge wraps auto-peek output as Codex UserPromptSubmit
# hook JSON while preserving the shared Claude hook behavior.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
cl_log "[peek] invoked (CLAUDE_SUBPROCESS='${CLAUDE_SUBPROCESS:-}')"

INPUT=$(cat 2>/dev/null || true)

# Never recurse into ourselves via the keyword-extraction subprocess.
[ -n "$CLAUDE_SUBPROCESS" ] && exit 0

ROLLOUT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
[ -z "$ROLLOUT" ] || [ -z "$SESSION_ID" ] && exit 0
[ -f "$ROLLOUT" ] || exit 0

# Stable per-session path: auto-peek derives its dedup key from this basename,
# so it must be the same file across prompts in one session. peek reads it
# synchronously (no detached reader), so a stable mutable path is safe here.
CONV="$CL_TRANSCRIPT_DIR/${SESSION_ID}.jsonl"
cl_convert_rollout "$ROLLOUT" "$CONV"

# Swap the transcript path so auto-peek reads the converted (Claude-format) file
# and derives a stable session id from its basename.
NEWINPUT=$(echo "$INPUT" | jq -c --arg t "$CONV" '.transcript_path = $t')

AUTO_PEEK_OUTPUT=$(echo "$NEWINPUT" | bash "$CLAUDE_PLUGIN_ROOT/hooks/auto-peek.sh")
AUTO_PEEK_STATUS=$?

if printf '%s' "$AUTO_PEEK_OUTPUT" | grep -q '[^[:space:]]'; then
  jq -cn --arg additionalContext "$AUTO_PEEK_OUTPUT" '{
    continue: true,
    suppressOutput: true,
    hookSpecificOutput: {
      hookEventName: "UserPromptSubmit",
      additionalContext: $additionalContext
    }
  }'
fi

exit "$AUTO_PEEK_STATUS"
