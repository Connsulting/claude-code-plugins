#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=skills/bonus-drain/config.sh
source "$HERE/config.sh"
args=(scout --json)
[ "${BONUS_DRAIN_SCOUT_DRY_RUN:-0}" = 1 ] && args+=(--dry-run)
exec "$BONUS_DRAIN_BIN" "${args[@]}" "$@"
