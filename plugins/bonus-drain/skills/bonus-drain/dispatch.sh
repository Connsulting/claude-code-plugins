#!/usr/bin/env bash
# Thin source compatibility surface; all rendering, claiming, and launch policy lives in Python.
set -uo pipefail
_DISPATCH_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=skills/bonus-drain/config.sh
source "$_DISPATCH_HERE/config.sh"

bonus_wrap_prompt() { # <task_json> <cycle> <provider>
  local task_json="${1:?task json required}" cycle="${2:?cycle required}" provider="${3:?provider required}"
  "$BONUS_DRAIN_BIN" render-prompt-json \
    --database "$BONUS_DB" \
    --task-json "$task_json" \
    --cycle "$cycle" \
    --provider "$provider"
}

bonus_dispatch_task() {
  "$BONUS_DRAIN_BIN" dispatch --database "$BONUS_DB" "$@"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  exec "$BONUS_DRAIN_BIN" dispatch --database "$BONUS_DB" "$@"
fi
