#!/usr/bin/env bash
# Prints one line: <5h_pct> <5h_reset_epoch> <7d_pct> <7d_reset_epoch> for the CODEX
# (ChatGPT-plan) rate limits, mirroring usage.sh's format for the Claude side. Codex has no
# standalone usage endpoint, so the source of truth is the newest Codex rollout's last
# `token_count` event, which carries a `rate_limits` object:
#   .payload.rate_limits.primary and .secondary, identified by window_minutes:
#     300   -> the 5h window
#     10080 -> the weekly window
# Each has used_percent (0-100) and resets_at (epoch seconds). Slight staleness is acceptable
# (same philosophy as usage.sh: worst case we hit the limit and stop). If a window has already
# reset (now >= resets_at) the stored used_percent is from a PAST window, so we report 0 (room).
# Missing data emits zero placeholders. The scout treats the absent weekly reset as a closed
# Codex gate while leaving an independently valid Claude window unaffected.
#
# WINDOWLESS ROLLOUTS (2026-08-19). Once the weekly limit is spent, a Codex session still writes
# a `rate_limits` object, but a DIFFERENT one - no windows at all:
#   {"limit_id":"premium","primary":null,"secondary":null,
#    "credits":{"has_credits":false,"balance":"0"}, ...}
# alongside a task_complete carrying `codex_error_info: "usage_limit_exceeded"`. That rollout is
# by construction the NEWEST file on disk at the exact moment the limit is hit, so taking the
# first rollout that merely CONTAINS "rate_limits" reported `0 0 0 0` - and the viewer, which
# maps a zero weekly reset to "no signal", dropped the whole Codex card at precisely the moment
# it had something to say. A reading needs an actual window, so keep scanning past any rollout
# whose primary and secondary are both null; the next one down still carries the real
# used_percent and resets_at, which renders as "capped - ceiling reached" instead of vanishing.
set -uo pipefail

SESS_DIR="${CODEX_SESSIONS_DIR:-$HOME/.codex/sessions}"
# Scan depth has to clear a RUN of windowless rollouts, not just one: while the limit is spent
# every further Codex attempt (interactive, /implement, agent-router) appends another one, and
# once they push the last real reading past the scan depth the card vanishes again. They are
# tiny files, and the healthy case still stops at the first rollout, so depth is nearly free.
SCAN_N="${CODEX_USAGE_SCAN_N:-60}"     # how many newest rollouts to scan for a rate_limits event
now=$(date +%s)

[ -d "$SESS_DIR" ] || { echo "0 0 0 0"; exit 0; }
command -v jq >/dev/null 2>&1 || { echo "0 0 0 0"; exit 0; }

# Newest rollout files first; scan until one carries a rate_limits event with at least one
# window (a rollout with no model turn yet won't have the event at all; a spent-limit rollout
# has the event but no windows - see above). grep -F is a fast literal-substring scan; tail -1
# takes the most recent event in that file. jq -e is the window test: it exits non-zero on
# `false` and on malformed JSON alike, so both keep the scan moving.
line=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  l="$(grep -F '"rate_limits"' "$f" 2>/dev/null | tail -1)"
  [ -n "$l" ] || continue
  printf '%s' "$l" \
    | jq -e '(.payload.rate_limits.primary // .payload.rate_limits.secondary) != null' \
      >/dev/null 2>&1 || continue
  line="$l"; break
done < <(find "$SESS_DIR" -name '*.jsonl' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n "$SCAN_N" | cut -d' ' -f2-)

[ -z "$line" ] && { echo "0 0 0 0"; exit 0; }

# Parse and normalize a rolled window (resets_at already passed) to 0% room.
out="$(printf '%s' "$line" | jq -r --argjson now "$now" '
  .payload.rate_limits as $rl
  | [$rl.primary, $rl.secondary] | map(select(. != null)) as $windows
  | ($windows | map(select(.window_minutes == 300)) | .[0] // {}) as $five_hour
  | ($windows | map(select(.window_minutes == 10080)) | .[0] // {}) as $weekly
  | ($five_hour.resets_at // 0) as $five_hour_reset
  | ($weekly.resets_at // 0) as $weekly_reset
  | [ (if $five_hour_reset <= $now then 0 else ($five_hour.used_percent // 0) end), $five_hour_reset,
      (if $weekly_reset <= $now then 0 else ($weekly.used_percent // 0) end), $weekly_reset ]
  | @tsv' 2>/dev/null)"

[ -z "$out" ] && { echo "0 0 0 0"; exit 0; }
# @tsv is tab-separated; normalize to single spaces for the space-delimited contract.
printf '%s\n' "$out" | tr '\t' ' '
