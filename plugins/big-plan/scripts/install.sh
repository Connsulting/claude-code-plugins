#!/bin/sh
# Stage the copied runtime and install a stable launcher and user unit.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
plugin_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
source_runtime="$plugin_root/skills/big-plan"
target_home=${HOME:?HOME is required}
enable=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --home)
            [ "$#" -ge 2 ] || { echo "usage: install.sh [--home DIR] [--enable]" >&2; exit 2; }
            target_home=$2
            shift 2
            ;;
        --enable)
            enable=1
            shift
            ;;
        *)
            echo "usage: install.sh [--home DIR] [--enable]" >&2
            exit 2
            ;;
    esac
done

if [ "$enable" -eq 1 ] && [ "$target_home" != "$HOME" ]; then
    echo "big-plan: --enable cannot be combined with a different --home" >&2
    exit 2
fi

runtime_parent="$target_home/.local/lib/big-plan"
runtime_dir="$runtime_parent/current"
bin_dir="$target_home/.local/bin"
unit_dir="$target_home/.config/systemd/user"

mkdir -p "$runtime_parent" "$bin_dir" "$unit_dir"
stage=$(mktemp -d "$runtime_parent/.current.XXXXXX")
backup="$runtime_parent/.previous.$$"
cleanup() {
    if [ -n "$stage" ] && [ -d "$stage" ]; then
        rm -rf "$stage"
    fi
}
trap cleanup EXIT HUP INT TERM
cp -a "$source_runtime/." "$stage/"

if [ -e "$runtime_dir" ]; then
    mv "$runtime_dir" "$backup"
fi
mv "$stage" "$runtime_dir"
stage=
rm -rf "$backup"

install -m 0755 "$script_dir/big-plan" "$bin_dir/big-plan"
install -m 0644 "$script_dir/big-plan.service" "$unit_dir/big-plan.service"

if [ "$target_home" = "$HOME" ]; then
    systemctl --user daemon-reload
fi

if [ "$enable" -eq 1 ]; then
    systemctl --user enable big-plan.service
    systemctl --user restart big-plan.service
fi

printf 'Installed Big Plan runtime: %s\n' "$runtime_dir"
printf 'Installed launcher: %s\n' "$bin_dir/big-plan"
printf 'Installed user unit: %s\n' "$unit_dir/big-plan.service"
if [ "$enable" -eq 0 ]; then
    printf 'Service not started. Run this installer again with --enable when ready.\n'
fi
