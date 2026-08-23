#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=skills/bonus-drain/config.sh
source "$HERE/config.sh"

subcommand="${1:-}"
if [ "$subcommand" = status ]; then
  shift
  exec "$BONUS_DRAIN_BIN" queue-status "$@"
fi
if [ "$subcommand" = migrate ] && [ "$#" -eq 3 ]; then
  exec "$BONUS_DRAIN_BIN" import-legacy --backlog "$2" --runs "$3"
fi
exec "$BONUS_DRAIN_BIN" "$@"
