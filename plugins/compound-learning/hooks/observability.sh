#!/bin/bash

# Shared logging + structured observability helpers for compound-learning hooks.

hook_now_ms() {
  local ms
  ms=$(date +%s%3N 2>/dev/null)
  if [ -n "$ms" ]; then
    echo "$ms"
  else
    python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
  fi
}

hook_iso_utc() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

hook_normalize_level() {
  case "${1:-info}" in
    debug) echo "debug" ;;
    info) echo "info" ;;
    warning|warn) echo "warn" ;;
    error) echo "error" ;;
    *) echo "info" ;;
  esac
}

hook_level_rank() {
  case "$(hook_normalize_level "$1")" in
    debug) echo 10 ;;
    info) echo 20 ;;
    warn) echo 30 ;;
    error) echo 40 ;;
    *) echo 20 ;;
  esac
}

hook_expand_home() {
  local raw="$1"
  echo "${raw//\$\{HOME\}/$HOME}"
}

hook_make_correlation_id() {
  if [ -n "${LEARNINGS_OBS_CORRELATION_ID:-}" ]; then
    echo "${LEARNINGS_OBS_CORRELATION_ID}"
    return
  fi
  if [ -f /proc/sys/kernel/random/uuid ]; then
    tr -d '-' < /proc/sys/kernel/random/uuid
    return
  fi
  python3 - <<'PY'
import uuid
print(uuid.uuid4().hex)
PY
}

hook_json_or_null() {
  local raw="$1"
  if [ -z "$raw" ]; then
    echo "null"
    return
  fi
  if echo "$raw" | jq -e . >/dev/null 2>&1; then
    echo "$raw"
  else
    echo "null"
  fi
}

hook_log_init() {
  HOOK_NAME="${1:-unknown}"
  HOOK_LOG_DIR="$HOME/.claude/plugins/compound-learning"
  if ! mkdir -p "$HOOK_LOG_DIR" 2>/dev/null; then
    HOOK_LOG_DIR="$HOME/.claude/plugins/compound-learning-logs"
    if ! mkdir -p "$HOOK_LOG_DIR" 2>/dev/null; then
      HOOK_LOG_DIR="/tmp/compound-learning-logs"
      mkdir -p "$HOOK_LOG_DIR" 2>/dev/null || true
    fi
  fi
  HOOK_ACTIVITY_LOG="$HOOK_LOG_DIR/activity.log"

  local config_file=""
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    config_file="${CLAUDE_PLUGIN_ROOT}/.claude-plugin/config.json"
  else
    local hook_dir
    hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    config_file="$(cd "$hook_dir/.." && pwd)/.claude-plugin/config.json"
  fi

  local cfg_enabled=""
  local cfg_level=""
  local cfg_log_path=""
  if [ -f "$config_file" ] && command -v jq >/dev/null 2>&1; then
    cfg_enabled=$(jq -r '.observability.enabled // empty' "$config_file" 2>/dev/null)
    cfg_level=$(jq -r '.observability.level // empty' "$config_file" 2>/dev/null)
    cfg_log_path=$(jq -r '.observability.logPath // empty' "$config_file" 2>/dev/null)
  fi

  local enabled_raw="${LEARNINGS_OBS_ENABLED:-$cfg_enabled}"
  case "${enabled_raw,,}" in
    1|true|yes|on) HOOK_OBS_ENABLED="1" ;;
    0|false|no|off) HOOK_OBS_ENABLED="0" ;;
    *) HOOK_OBS_ENABLED="0" ;;
  esac

  HOOK_OBS_LEVEL="$(hook_normalize_level "${LEARNINGS_OBS_LEVEL:-$cfg_level}")"

  local log_path_raw="${LEARNINGS_OBS_LOG_PATH:-$cfg_log_path}"
  if [ -z "$log_path_raw" ]; then
    log_path_raw="$HOOK_LOG_DIR/observability.jsonl"
  fi
  HOOK_OBS_LOG_PATH="$(hook_expand_home "$log_path_raw")"

  HOOK_CORRELATION_ID="$(hook_make_correlation_id)"
  HOOK_START_MS="$(hook_now_ms)"
}

hook_log_activity() {
  local message="$1"
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$message" >> "$HOOK_ACTIVITY_LOG"
}

hook_obs_level_allows() {
  local requested
  local minimum
  requested="$(hook_level_rank "$1")"
  minimum="$(hook_level_rank "${HOOK_OBS_LEVEL:-info}")"
  [ "$requested" -ge "$minimum" ]
}

hook_safe_append() {
  local line="$1"
  mkdir -p "$(dirname "$HOOK_OBS_LOG_PATH")" 2>/dev/null || return 0
  if command -v flock >/dev/null 2>&1; then
    {
      flock -x 200
      printf '%s\n' "$line" >> "$HOOK_OBS_LOG_PATH" 2>/dev/null
    } 200>>"${HOOK_OBS_LOG_PATH}.lock"
  else
    printf '%s\n' "$line" >> "$HOOK_OBS_LOG_PATH" 2>/dev/null
  fi
}

hook_obs_event() {
  local level="$1"
  local operation="$2"
  local status="$3"
  shift 3

  [ "${HOOK_OBS_ENABLED:-0}" = "1" ] || return 0
  hook_obs_level_allows "$level" || return 0

  local message=""
  local duration_ms=""
  local session_id=""
  local correlation_id="${HOOK_CORRELATION_ID:-}"
  local counts_json="null"
  local error_json="null"
  local extra_json="null"

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --message)
        message="$2"
        shift 2
        ;;
      --duration-ms)
        duration_ms="$2"
        shift 2
        ;;
      --session-id)
        session_id="$2"
        shift 2
        ;;
      --correlation-id)
        correlation_id="$2"
        shift 2
        ;;
      --counts-json)
        counts_json="$(hook_json_or_null "$2")"
        shift 2
        ;;
      --error-json)
        error_json="$(hook_json_or_null "$2")"
        shift 2
        ;;
      --extra-json)
        extra_json="$(hook_json_or_null "$2")"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done

  local duration_json="null"
  if [ -n "$duration_ms" ]; then
    duration_json="$duration_ms"
  fi

  local event
  event=$(jq -cn \
    --arg timestamp "$(hook_iso_utc)" \
    --arg level "$(hook_normalize_level "$level")" \
    --arg component "hook" \
    --arg hook "${HOOK_NAME:-unknown}" \
    --arg operation "$operation" \
    --arg status "$status" \
    --arg message "$message" \
    --arg session_id "$session_id" \
    --arg correlation_id "$correlation_id" \
    --argjson duration_ms "$duration_json" \
    --argjson counts "$counts_json" \
    --argjson error "$error_json" \
    --argjson extra "$extra_json" \
    '
    {
      timestamp: $timestamp,
      level: $level,
      component: $component,
      hook: $hook,
      operation: $operation,
      status: $status
    }
    + (if $duration_ms == null then {} else {duration_ms: $duration_ms} end)
    + (if ($message | length) == 0 then {} else {message: $message} end)
    + (if ($session_id | length) == 0 then {} else {session_id: $session_id} end)
    + (if ($correlation_id | length) == 0 then {} else {correlation_id: $correlation_id} end)
    + (if $counts == null then {} else {counts: $counts} end)
    + (if $error == null then {} else {error: $error} end)
    + (if $extra == null then {} else $extra end)
    ' 2>/dev/null)

  [ -n "$event" ] || return 0
  hook_safe_append "$event"
}

hook_elapsed_ms() {
  local end_ms
  end_ms="$(hook_now_ms)"
  local start_ms="${HOOK_START_MS:-$end_ms}"
  echo $((end_ms - start_ms))
}
