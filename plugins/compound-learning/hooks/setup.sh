#!/bin/bash

# Ensure lightweight SQLite dependencies on SessionStart.
# Heavy embedding dependencies install lazily on first semantic use.
# Exits 0 always to avoid blocking Claude session startup on bootstrap errors.

if [ -z "$CLAUDE_PLUGIN_ROOT" ]; then
  CLAUDE_PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi

python3 "${CLAUDE_PLUGIN_ROOT}/lib/bootstrap.py" ensure core --quiet >/dev/null 2>&1 || true

exit 0
