#!/bin/bash
# Codex SessionStart -> compound-learning dependency bootstrap.
# Sets CLAUDE_PLUGIN_ROOT (Codex does not) and delegates to the plugin's
# setup.sh, which installs the Python deps into ~/.claude/state/... (idempotent;
# a no-op once Claude or a prior Codex session has bootstrapped them).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

cat >/dev/null  # drain the hook payload; setup.sh needs no input
bash "$CLAUDE_PLUGIN_ROOT/hooks/setup.sh"
exit 0
