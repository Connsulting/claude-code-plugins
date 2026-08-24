#!/bin/sh
# Remove installed runtime files while preserving Big Plan plans and sidecars.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$script_dir/scripts/uninstall.sh" "$@"
