#!/usr/bin/env bash
# Prints one line: <5h_pct> <5h_reset_epoch> <7d_pct> <7d_reset_epoch>
# Fields are empty if unavailable. Source of truth is the Anthropic OAuth usage
# endpoint; we reuse the statusline's /tmp cache when fresh and keep a durable copy
# in ~/.claude that is NEVER deleted (so a somewhat-stale-but-latest value always
# survives reboot). Slight staleness is acceptable: worst case we hit the limit and stop.
set -uo pipefail

TMP_CACHE="/tmp/claude-usage-cache.json"               # shared with statusline
DURABLE_CACHE="$HOME/.claude/.bonus-usage-cache.json"  # our persistent fallback, never deleted
MAX_AGE=300
now=$(date +%s)

is_fresh() { [ -f "$1" ] && [ $(( now - $(stat -c %Y "$1" 2>/dev/null || echo 0) )) -le "$MAX_AGE" ]; }

# Opportunistic refresh if neither cache is fresh.
if ! is_fresh "$TMP_CACHE" && ! is_fresh "$DURABLE_CACHE"; then
    token=$(jq -r '.claudeAiOauth.accessToken // empty' "$HOME/.claude/.credentials.json" 2>/dev/null)
    if [ -n "$token" ]; then
        resp=$(curl -s --max-time 6 "https://api.anthropic.com/api/oauth/usage" \
            -H "Authorization: Bearer $token" \
            -H "anthropic-beta: oauth-2025-04-20" \
            -H "Content-Type: application/json" 2>/dev/null)
        if echo "$resp" | jq -e '.seven_day' >/dev/null 2>&1; then
            echo "$resp" > "$TMP_CACHE"
            echo "$resp" > "$DURABLE_CACHE"
        fi
    fi
fi

# Read whichever cache is newer; never delete either.
SRC=""
if is_fresh "$TMP_CACHE"; then SRC="$TMP_CACHE"
elif is_fresh "$DURABLE_CACHE"; then SRC="$DURABLE_CACHE"
elif [ -f "$TMP_CACHE" ]; then SRC="$TMP_CACHE"
elif [ -f "$DURABLE_CACHE" ]; then SRC="$DURABLE_CACHE"
fi
[ -z "$SRC" ] && { echo "   "; exit 0; }

to_epoch() { [ -n "$1" ] && date -d "$1" +%s 2>/dev/null || echo "0"; }
u5=$(jq -r '.five_hour.utilization // empty' "$SRC" 2>/dev/null)
r5=$(jq -r '.five_hour.resets_at // empty' "$SRC" 2>/dev/null)
u7=$(jq -r '.seven_day.utilization // empty' "$SRC" 2>/dev/null)
r7=$(jq -r '.seven_day.resets_at // empty' "$SRC" 2>/dev/null)
echo "$u5 $(to_epoch "$r5") $u7 $(to_epoch "$r7")"
