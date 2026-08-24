#!/bin/sh
# Stable top-level installation helper. Installation does not start the service.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$script_dir/scripts/install.sh" "$@"
