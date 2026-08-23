#!/usr/bin/env sh
# Install this Bonus Drain skill into a selected HOME without enabling services.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
skill_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
target_home=${HOME:?HOME is required}

if [ "${1:-}" = "--home" ]; then
    [ "$#" -eq 2 ] || { echo "usage: install.sh [--home DIR]" >&2; exit 2; }
    target_home=$2
elif [ "$#" -ne 0 ]; then
    echo "usage: install.sh [--home DIR]" >&2
    exit 2
fi

PYTHONPATH="$skill_root${PYTHONPATH:+:$PYTHONPATH}" \
python3 - "$skill_root" "$target_home" <<'PY'
import json
import sys
from bonus_drain import lifecycle

installed = lifecycle.install(sys.argv[1], sys.argv[2])
print(json.dumps({
    "version": installed.version,
    "version_dir": str(installed.version_dir),
    "current": str(installed.current),
    "wrapper": str(installed.wrapper),
    "units": [str(path) for path in installed.units],
}, sort_keys=True, separators=(",", ":")))
PY
