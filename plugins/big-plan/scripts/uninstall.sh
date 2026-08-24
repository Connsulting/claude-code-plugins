#!/bin/sh
# Remove lifecycle-owned runtime files; preserve plans, sidecars, config, and state.
set -eu

target_home=${HOME:?HOME is required}
if [ "${1:-}" = "--home" ]; then
    [ "$#" -eq 2 ] || { echo "usage: uninstall.sh [--home DIR]" >&2; exit 2; }
    target_home=$2
elif [ "$#" -ne 0 ]; then
    echo "usage: uninstall.sh [--home DIR]" >&2
    exit 2
fi

if [ "$target_home" = "$HOME" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now big-plan.service >/dev/null 2>&1 || true
fi

rm -rf "$target_home/.local/lib/big-plan/current"
rm -f "$target_home/.local/bin/big-plan"
rm -f "$target_home/.config/systemd/user/big-plan.service"

if [ "$target_home" = "$HOME" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

printf 'Removed Big Plan runtime, launcher, and user unit. Plans, sidecars, config, and state were preserved.\n'
