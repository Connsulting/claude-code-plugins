#!/bin/bash

# Auto-install Python dependencies for compound-learning plugin
# Runs on SessionStart to ensure deps are ready before other hooks fire
# Idempotent: skips pip if all packages already importable
# Exits 0 always to avoid blocking session start

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/observability.sh" || exit 0

hook_log_init "setup"
SESSION_ID="${CLAUDE_SESSION_ID:-}"
HOOK_OUTCOME="success"
HOOK_OUTCOME_MESSAGE=""

setup_hook_finalize() {
  local duration_ms
  duration_ms="$(hook_elapsed_ms)"
  local level="info"
  case "$HOOK_OUTCOME" in
    success) level="info" ;;
    skipped) level="info" ;;
    error) level="error" ;;
    *) level="warn" ;;
  esac

  hook_obs_event "$level" "hook_end" "$HOOK_OUTCOME" \
    --duration-ms "$duration_ms" \
    --session-id "$SESSION_ID" \
    --message "$HOOK_OUTCOME_MESSAGE"
}
trap setup_hook_finalize EXIT

hook_obs_event "info" "hook_start" "start" --session-id "$SESSION_ID"

# Quick check: can we import all required packages?
if python3 -c "
import importlib.util, sys
for pkg in ['pysqlite3', 'sqlite_vec', 'sentence_transformers']:
    if importlib.util.find_spec(pkg) is None:
        sys.exit(1)
" 2>/dev/null; then
  hook_obs_event "info" "dependency_check" "success" \
    --session-id "$SESSION_ID" \
    --counts-json '{"missing_packages":0}'
  hook_obs_event "info" "subprocess_exit" "success" \
    --session-id "$SESSION_ID" \
    --counts-json '{"command":"python_import_check","exit_code":0}'
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="Dependencies already installed"
  hook_obs_event "info" "skip_reason" "skipped" \
    --session-id "$SESSION_ID" \
    --message "dependencies already installed"
  exit 0
fi

hook_obs_event "warn" "subprocess_exit" "failure" \
  --session-id "$SESSION_ID" \
  --counts-json '{"command":"python_import_check","exit_code":1}'
hook_obs_event "info" "dependency_check" "missing" \
  --session-id "$SESSION_ID"
hook_log_activity "[setup] Missing Python dependencies, installing..."

# Install all required packages quietly
if pip install --quiet pysqlite3-binary sqlite-vec sentence-transformers 2>>"$HOOK_ACTIVITY_LOG"; then
  hook_log_activity "[setup] Python dependencies installed successfully"
  hook_obs_event "info" "subprocess_exit" "success" \
    --session-id "$SESSION_ID" \
    --counts-json '{"command":"pip_install","exit_code":0}'
  HOOK_OUTCOME="success"
  HOOK_OUTCOME_MESSAGE="Dependencies installed successfully"
else
  pip_exit=$?
  hook_log_activity "[setup] pip install failed (exit $pip_exit), plugin may not work correctly"
  hook_obs_event "error" "subprocess_exit" "failure" \
    --session-id "$SESSION_ID" \
    --counts-json "{\"command\":\"pip_install\",\"exit_code\":$pip_exit}"
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="pip install failed (exit $pip_exit)"
fi

exit 0
