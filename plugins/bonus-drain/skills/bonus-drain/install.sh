#!/bin/sh
# Stable top-level installation helper for plugin and source checkouts.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$script_dir/scripts/install.sh" "$@"
