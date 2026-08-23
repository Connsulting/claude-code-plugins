#!/usr/bin/env sh
# Remove only lifecycle-owned runtime files. Configuration, cache, and state remain.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
skill_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
target_home=${HOME:?HOME is required}

if [ "${1:-}" = "--home" ]; then
    [ "$#" -eq 2 ] || { echo "usage: uninstall.sh [--home DIR]" >&2; exit 2; }
    target_home=$2
elif [ "$#" -ne 0 ]; then
    echo "usage: uninstall.sh [--home DIR]" >&2
    exit 2
fi

PYTHONPATH="$skill_root${PYTHONPATH:+:$PYTHONPATH}" \
python3 - "$target_home" <<'PY'
import json
import sys
from bonus_drain import lifecycle

removed = lifecycle.uninstall(sys.argv[1])
print(json.dumps({"removed": [str(path) for path in removed], "state_preserved": True},
                 sort_keys=True, separators=(",", ":")))
PY
