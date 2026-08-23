#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=skills/bonus-drain/config.sh
source "$HERE/config.sh"
exec "$BONUS_DRAIN_BIN" usage --legacy-index "${BONUS_USAGE_LEGACY_INDEX:-0}" --legacy-line "$@"
