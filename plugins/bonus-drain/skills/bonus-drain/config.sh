#!/usr/bin/env bash
# XDG-authoritative compatibility exports. JSON remains the source of provider policy.
_BONUS_DRAIN_SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export BONUS_DRAIN_CONFIG="${BONUS_DRAIN_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/bonus-drain/config.json}"
export BONUS_DRAIN_STATE_DIR="${BONUS_DRAIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/bonus-drain}"
export BONUS_DRAIN_CACHE_DIR="${BONUS_DRAIN_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/bonus-drain}"
export BONUS_USAGE_CACHE_DIR="${BONUS_USAGE_CACHE_DIR:-$BONUS_DRAIN_CACHE_DIR/usage}"

if [ -z "${BONUS_DRAIN_BIN:-}" ]; then
  if [ -x "$HOME/.local/bin/bonus-drain" ]; then
    BONUS_DRAIN_BIN="$HOME/.local/bin/bonus-drain"
  else
    BONUS_DRAIN_BIN="$_BONUS_DRAIN_SOURCE_DIR/bin/bonus-drain"
  fi
fi
export BONUS_DRAIN_BIN

if [ -z "${BONUS_DB:-}" ]; then
  BONUS_DB="$BONUS_DRAIN_STATE_DIR/queue.db"
  if [ -f "$BONUS_DRAIN_CONFIG" ]; then
    _bonus_config_database="$({ python3 - "$BONUS_DRAIN_CONFIG" <<'PY'
import json
import sys
from pathlib import Path
try:
    path = Path(sys.argv[1])
    value = json.loads(path.read_text(encoding="utf-8")).get("database")
    if value:
        candidate = Path(value).expanduser()
        print(candidate if candidate.is_absolute() else (path.parent / candidate).resolve())
except Exception:
    pass
PY
    } 2>/dev/null)"
    [ -n "$_bonus_config_database" ] && BONUS_DB="$_bonus_config_database"
    unset _bonus_config_database
  fi
fi
export BONUS_DB

export BONUSDB="${BONUSDB:-$_BONUS_DRAIN_SOURCE_DIR/bonusdb.sh}"
export BONUS_USAGE_CMD="${BONUS_USAGE_CMD:-$_BONUS_DRAIN_SOURCE_DIR/usage.sh}"
export BONUS_CODEX_USAGE_CMD="${BONUS_CODEX_USAGE_CMD:-$_BONUS_DRAIN_SOURCE_DIR/codex-usage.sh}"
export BONUS_GROK_USAGE_CMD="${BONUS_GROK_USAGE_CMD:-$_BONUS_DRAIN_SOURCE_DIR/grok-usage.sh}"

# Legacy shell consumers still source these policy defaults.  The generic JSON graph remains
# authoritative for the Python planner; these exports are compatibility inputs only.
export DRAIN_MIN_WEEKLY_REMAINING_PCT="${DRAIN_MIN_WEEKLY_REMAINING_PCT:-2}"
export DRAIN_UNTIL_PCT="${DRAIN_UNTIL_PCT:-$((100 - DRAIN_MIN_WEEKLY_REMAINING_PCT))}"
export FIVE_HOUR_START_MAX="${FIVE_HOUR_START_MAX:-75}"
export DRAIN_LEAD_MAX_HOURS="${DRAIN_LEAD_MAX_HOURS:-72}"
export WINDOW_HOURS="${WINDOW_HOURS:-5}"
export PCT_PER_WINDOW="${PCT_PER_WINDOW:-2.5}"
export EST_PCT_PER_JOB="${EST_PCT_PER_JOB:-0.75}"
export BATCH_N="${BATCH_N:-6}"

export BONUS_MODEL="${BONUS_MODEL:-opus[1m]}"
export CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
export CODEX_BIN="${CODEX_BIN:-$HOME/.local/bin/codex}"
export GROK_BIN="${GROK_BIN:-$HOME/.local/bin/grok}"
export AGENT_ROUTER_BIN="${AGENT_ROUTER_BIN:-$HOME/.local/bin/agent-router}"
export SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
export BONUS_PR_REPOS="${BONUS_PR_REPOS:-}"

export CODEX_DRAIN_LEAD_MAX_HOURS="${CODEX_DRAIN_LEAD_MAX_HOURS:-$DRAIN_LEAD_MAX_HOURS}"
export CODEX_WEEKLY_CEIL="${CODEX_WEEKLY_CEIL:-98}"
export CODEX_PCT_PER_WINDOW="${CODEX_PCT_PER_WINDOW:-$PCT_PER_WINDOW}"
export CODEX_EST_PCT_PER_JOB="${CODEX_EST_PCT_PER_JOB:-$EST_PCT_PER_JOB}"
export BEHIND_SAFETY_PCT="${BEHIND_SAFETY_PCT:-100}"
export GROK_DRAIN_LEAD_MAX_HOURS="${GROK_DRAIN_LEAD_MAX_HOURS:-72}"
export GROK_WEEKLY_CEIL="${GROK_WEEKLY_CEIL:-98}"
export GROK_PCT_PER_WINDOW="${GROK_PCT_PER_WINDOW:-2.5}"
export GROK_EST_PCT_PER_JOB="${GROK_EST_PCT_PER_JOB:-0.75}"

export BONUS_ACCOUNTS_STORE="${BONUS_ACCOUNTS_STORE:-${XDG_DATA_HOME:-$HOME/.local/share}/bonus-drain/legacy-accounts}"
export BONUS_PIN_FILE="${BONUS_PIN_FILE:-$BONUS_ACCOUNTS_STORE/PIN}"
export BONUS_CODEX_ACCOUNTS_STORE="${BONUS_CODEX_ACCOUNTS_STORE:-${XDG_DATA_HOME:-$HOME/.local/share}/bonus-drain/legacy-codex-accounts}"
export BONUS_CODEX_PIN_FILE="${BONUS_CODEX_PIN_FILE:-$BONUS_CODEX_ACCOUNTS_STORE/PIN}"
export BONUS_ROTATOR_DIR="${BONUS_ROTATOR_DIR:-}"
export BONUS_ROTATE_CMD="${BONUS_ROTATE_CMD:-${BONUS_ROTATOR_DIR:+$BONUS_ROTATOR_DIR/rotate.sh}}"
export BONUS_CODEX_ROTATE_CMD="${BONUS_CODEX_ROTATE_CMD:-${BONUS_ROTATOR_DIR:+$BONUS_ROTATOR_DIR/codex-rotate.sh}}"
export BONUS_ROTATOR_CONFIG="${BONUS_ROTATOR_CONFIG:-${BONUS_ROTATOR_DIR:+$BONUS_ROTATOR_DIR/config.env}}"
export BONUS_WEEKLY_ANCHOR_DOW="${BONUS_WEEKLY_ANCHOR_DOW:-6}"
export BONUS_WEEKLY_ANCHOR_HHMM="${BONUS_WEEKLY_ANCHOR_HHMM:-2205}"
export BONUS_USAGE_MAX_AGE="${BONUS_USAGE_MAX_AGE:-3600}"

# Router subprocesses resolve concrete provider binaries by bare name.  Add only existing
# override directories and never launch a provider here.
bonus_ensure_provider_path() {
  local bin dir
  for bin in "$AGENT_ROUTER_BIN" "$CLAUDE_BIN" "$CODEX_BIN" "$GROK_BIN"; do
    [ -n "$bin" ] || continue
    dir="$(dirname -- "$bin")"
    [ -d "$dir" ] || continue
    case ":$PATH:" in *":$dir:"*) continue ;; esac
    PATH="$dir:$PATH"
  done
  export PATH
}
bonus_ensure_provider_path

unset _BONUS_DRAIN_SOURCE_DIR
