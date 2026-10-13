#!/usr/bin/env sh
# Install this Bonus Drain skill into a selected HOME without enabling services.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
skill_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
: "${HOME:?HOME is required}"
target_home=
install_version=

usage_error() {
    echo "$1" >&2
    echo "usage: install.sh [--home DIR] [--install-version VERSION]" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --home|--install-version)
            [ "$#" -ge 2 ] || usage_error "$1 requires a value"
            [ -n "$2" ] || usage_error "$1 requires a nonempty value"
            case "$2" in
                -*) usage_error "$1 requires a value" ;;
            esac
            case "$1" in
                --home) target_home=$2 ;;
                --install-version) install_version=$2 ;;
            esac
            shift 2
            ;;
        *) usage_error "unknown argument: $1" ;;
    esac
done

python3 -I -B - "$skill_root" "$target_home" "$install_version" <<'PY'
import json
import sys
sys.path.insert(0, sys.argv[1])
from bonus_drain import lifecycle

# No --home is the caller's live install, which keeps its own XDG and config overrides.
installed = lifecycle.install(sys.argv[1], sys.argv[2] or None, version=sys.argv[3] or None)
print(json.dumps({
    "version": installed.version,
    "version_dir": str(installed.version_dir),
    "current": str(installed.current),
    "wrapper": str(installed.wrapper),
    "units": [str(path) for path in installed.units],
}, sort_keys=True, separators=(",", ":")))
PY
