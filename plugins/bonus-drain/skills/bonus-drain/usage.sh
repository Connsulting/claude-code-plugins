#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=skills/bonus-drain/config.sh
source "$HERE/config.sh"
provider=""
account=""
previous=""
for token in "$@"; do
  case "$previous" in
    provider) provider="$token" ;;
    account) account="$token" ;;
  esac
  previous=""
  case "$token" in
    --provider) previous="provider" ;;
    --account) previous="account" ;;
  esac
done
if [ -z "$provider" ] || [ -z "$account" ]; then
  echo "usage.sh requires explicit --provider ID and --account ID" >&2
  exit 2
fi
exec "$BONUS_DRAIN_BIN" usage --legacy-line "$@"
