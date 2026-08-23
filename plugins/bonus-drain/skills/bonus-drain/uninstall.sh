#!/bin/sh
# Stable top-level removal helper; runtime state and configuration are preserved.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$script_dir/scripts/uninstall.sh" "$@"
