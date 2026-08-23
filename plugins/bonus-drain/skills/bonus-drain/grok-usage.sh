#!/usr/bin/env bash
# Read Grok weekly usage from authenticated billing with a disk log fallback.
set -uo pipefail

_GROK_USAGE_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=skills/bonus-drain/config.sh
source "$_GROK_USAGE_HERE/config.sh"
if [ -r "${BONUS_DRAIN_CONFIG:-}" ]; then
  exec "$BONUS_DRAIN_BIN" usage --config "$BONUS_DRAIN_CONFIG" \
    --legacy-index "${BONUS_GROK_USAGE_LEGACY_INDEX:-2}" --legacy-json "$@"
fi

log_path="${GROK_USAGE_LOG:-$HOME/.grok/logs/unified.jsonl}"
auth_path="$HOME/.grok/auth.json"
billing_url="https://cli-chat-proxy.grok.com/v1/billing?format=credits"
user_url="https://cli-chat-proxy.grok.com/v1/user?include=subscription"
now_epoch="${GROK_USAGE_NOW_EPOCH:-$(date -u +%s)}"
max_age="${GROK_USAGE_MAX_AGE:-3600}"
auth_header_file=""

cleanup() {
  if [ -n "$auth_header_file" ] && [ -f "$auth_header_file" ]; then
    rm -f -- "$auth_header_file"
  fi
}
trap cleanup EXIT

emit_json() {
  local tier="$1" weekly_percent="$2" weekly_reset="$3" source_ts="$4"
  jq -cn \
    --arg tier "$tier" \
    --argjson weekly_percent "$weekly_percent" \
    --argjson weekly_reset "$weekly_reset" \
    --arg source_ts "$source_ts" \
    '{
      tier: (if $tier == "" then null else $tier end),
      weekly_percent: $weekly_percent,
      weekly_reset: $weekly_reset,
      source_ts: (if $source_ts == "" then null else $source_ts end)
    }'
}

clock_valid=0
if [[ "$now_epoch" =~ ^[0-9]+$ ]] && [[ "$max_age" =~ ^[0-9]+$ ]]; then
  clock_valid=1
fi

disk_tier=""
disk_percent=null
disk_reset=null
disk_source_ts=""

event=""
if [ -r "$log_path" ]; then
  while IFS= read -r line; do
    candidate="$(jq -c 'objects | select(.msg == "billing: fetched credits config")' \
      <<<"$line" 2>/dev/null)" || candidate=""
    if [ -n "$candidate" ]; then
      event="$candidate"
      break
    fi
  done < <(tac -- "$log_path")
fi

if [ -n "$event" ]; then
  disk_tier="$(jq -r '.ctx.subscriptionTier | if type == "string" then . else "" end' \
    <<<"$event")"
  period_type="$(jq -r '.ctx.config.currentPeriod.type | if type == "string" then . else "" end' \
    <<<"$event")"
  period_end="$(jq -r '.ctx.config.currentPeriod.end | if type == "string" then . else "" end' \
    <<<"$event")"
  disk_source_ts="$(jq -r '.ts | if type == "string" then . else "" end' <<<"$event")"

  fresh=0
  if [ "$clock_valid" -eq 1 ]; then
    source_epoch="$(date -u -d "$disk_source_ts" +%s 2>/dev/null)" || source_epoch=""
    if [[ "$source_epoch" =~ ^[0-9]+$ ]] && \
       [ "$source_epoch" -le "$now_epoch" ] && \
       [ $((now_epoch - source_epoch)) -le "$max_age" ]; then
      fresh=1
    fi
  fi

  if [ "$disk_tier" = "SuperGrok Plus" ] && \
     [ "$period_type" = "USAGE_PERIOD_TYPE_WEEKLY" ]; then
    reset_epoch="$(date -u -d "$period_end" +%s 2>/dev/null)" || reset_epoch=""
    if [[ "$reset_epoch" =~ ^[0-9]+$ ]]; then
      disk_reset="$reset_epoch"
      if [ "$clock_valid" -eq 1 ] && [ "$fresh" -eq 1 ] && \
         [ "$reset_epoch" -gt "$now_epoch" ] && \
         jq -e '.ctx.config.creditUsagePercent |
           type == "number" and . >= 0 and . <= 100' \
           <<<"$event" >/dev/null 2>&1; then
        disk_percent="$(jq -c '.ctx.config.creditUsagePercent' <<<"$event")"
      fi
    fi
  fi
fi

access_token=""
if [ -r "$auth_path" ]; then
  access_token="$(jq -er \
    '[.[] | objects | .key | select(type == "string" and length > 0)][0] // empty' \
    "$auth_path" 2>/dev/null)" || access_token=""
fi

direct_valid=0
direct_percent=null
direct_reset=null
direct_tier=""
direct_source_ts=""
if [ -n "$access_token" ] && [ "$clock_valid" -eq 1 ]; then
  auth_header_file="$(mktemp)" || auth_header_file=""
  if [ -n "$auth_header_file" ]; then
    chmod 600 "$auth_header_file"
    if printf 'Authorization: Bearer %s\n' "$access_token" > "$auth_header_file"; then
      billing="$(curl --fail --silent --show-error \
        --connect-timeout 3 --max-time 8 --request GET \
        --header "@$auth_header_file" \
        --header 'X-XAI-Token-Auth: xai-grok-cli' \
        "$billing_url" 2>/dev/null)" || billing=""
      user="$(curl --fail --silent --show-error \
        --connect-timeout 3 --max-time 8 --request GET \
        --header "@$auth_header_file" \
        --header 'X-XAI-Token-Auth: xai-grok-cli' \
        "$user_url" 2>/dev/null)" || user=""
      user_valid=0
      if jq -e '
        type == "object" and .subscriptionTier == "SuperGrokPlus"
      ' <<<"$user" >/dev/null 2>&1; then
        user_valid=1
      fi
      billing_valid=0
      billing_config="$(jq -ec '.config | select(type == "object")' \
        <<<"$billing" 2>/dev/null)" || billing_config=""
      if jq -e '
        type == "object" and
        (.currentPeriod.type == "USAGE_PERIOD_TYPE_WEEKLY") and
        (.currentPeriod.end | type == "string") and
        ((has("creditUsagePercent") | not) or
         (.creditUsagePercent | type == "number" and . >= 0 and . <= 100))
      ' <<<"$billing_config" >/dev/null 2>&1; then
        period_end="$(jq -r '.currentPeriod.end' <<<"$billing_config")"
        reset_epoch="$(date -u -d "$period_end" +%s 2>/dev/null)" || reset_epoch=""
        if [[ "$reset_epoch" =~ ^[0-9]+$ ]] && [ "$reset_epoch" -gt "$now_epoch" ]; then
          direct_reset="$reset_epoch"
          if jq -e 'has("creditUsagePercent")' <<<"$billing_config" >/dev/null 2>&1; then
            direct_percent="$(jq -c '.creditUsagePercent' <<<"$billing_config")"
          else
            direct_percent=0
          fi
          billing_valid=1
        fi
      fi
      if [ "$billing_valid" -eq 1 ] && [ "$user_valid" -eq 1 ]; then
        direct_tier="SuperGrok Plus"
        direct_source_ts="$(date -u -d "@$now_epoch" +'%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)" || \
          direct_source_ts=""
        direct_valid=1
      fi
    fi
    rm -f -- "$auth_header_file"
    auth_header_file=""
  fi
fi

if [ "$direct_valid" -eq 1 ]; then
  emit_json "$direct_tier" "$direct_percent" "$direct_reset" "$direct_source_ts"
else
  emit_json "$disk_tier" "$disk_percent" "$disk_reset" "$disk_source_ts"
fi
