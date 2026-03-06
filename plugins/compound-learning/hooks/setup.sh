#!/bin/bash

# Auto-install Python dependencies for compound-learning plugin
# Runs on SessionStart to ensure deps are ready before other hooks fire.
# Exits 0 always to avoid blocking session start.

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/observability.sh" || exit 0

hook_log_init "setup"
SESSION_ID="${CLAUDE_SESSION_ID:-}"
hook_set_session_context "$SESSION_ID"
HOOK_OUTCOME="success"
HOOK_OUTCOME_MESSAGE=""

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$HOOK_DIR/.." && pwd)}"
MANIFEST_PATH="$PLUGIN_ROOT/requirements-runtime.txt"
FORCE_REFRESH_RAW="${LEARNINGS_SETUP_FORCE_REFRESH:-${COMPOUND_LEARNING_SETUP_FORCE_REFRESH:-0}}"

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

declare -a REQUIREMENTS

is_truthy() {
  case "${1:-}" in
    1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]) return 0 ;;
    *) return 1 ;;
  esac
}

trim_line() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

requirement_to_import() {
  local requirement="$1"
  local package="$requirement"

  package="${package%%;*}"
  package="${package%%\[*}"
  package="${package%%==*}"
  package="${package%%~=*}"
  package="${package%%!=*}"
  package="${package%%<=*}"
  package="${package%%>=*}"
  package="${package%%<*}"
  package="${package%%>*}"
  package="$(trim_line "$package")"

  case "$package" in
    pysqlite3-binary) echo "pysqlite3" ;;
    sqlite-vec) echo "sqlite_vec" ;;
    sentence-transformers) echo "sentence_transformers" ;;
    *) echo "${package//-/_}" ;;
  esac
}

load_manifest_requirements() {
  REQUIREMENTS=()

  if [ ! -f "$MANIFEST_PATH" ]; then
    return 1
  fi

  while IFS= read -r raw_line || [ -n "$raw_line" ]; do
    raw_line="${raw_line%$'\r'}"
    local line="${raw_line%%#*}"
    line="$(trim_line "$line")"
    [ -z "$line" ] && continue
    REQUIREMENTS+=("$line")
  done < "$MANIFEST_PATH"

  [ "${#REQUIREMENTS[@]}" -gt 0 ]
}

manifest_checksum() {
  local file_path="$1"

  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file_path" | awk '{print $1}'
    return
  fi

  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file_path" | awk '{print $1}'
    return
  fi

  python3 - "$file_path" <<'PY'
import hashlib
import pathlib
import sys

payload = pathlib.Path(sys.argv[1]).read_bytes()
print(hashlib.sha256(payload).hexdigest())
PY
}

write_cache_stamp() {
  mkdir -p "$CACHE_DIR" 2>/dev/null || return 1

  {
    printf 'python_version=%s\n' "$PYTHON_VERSION"
    printf 'manifest_sha256=%s\n' "$MANIFEST_SHA256"
    printf 'generated_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  } > "$STAMP_FILE" 2>/dev/null || return 1

  find "$CACHE_DIR" -maxdepth 1 -type f -name 'setup-*.stamp' ! -name "$(basename "$STAMP_FILE")" -delete 2>/dev/null || true
  return 0
}

if ! command -v python3 >/dev/null 2>&1; then
  hook_log_activity "[setup] python3 not found; skipping dependency bootstrap"
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="python3 not found"
  hook_obs_event "error" "dependency_check" "failure" \
    --session-id "$SESSION_ID" \
    --message "python3 not found"
  exit 0
fi

declare -a PIP_COMMAND
if command -v pip >/dev/null 2>&1; then
  PIP_COMMAND=(pip)
elif python3 -m pip --version >/dev/null 2>&1; then
  PIP_COMMAND=(python3 -m pip)
else
  hook_log_activity "[setup] pip not found; skipping dependency bootstrap"
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="pip not found"
  hook_obs_event "error" "dependency_check" "failure" \
    --session-id "$SESSION_ID" \
    --message "pip not found"
  exit 0
fi

if ! load_manifest_requirements; then
  hook_log_activity "[setup] Missing or empty requirements manifest at $MANIFEST_PATH"
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="Missing requirements-runtime.txt"
  hook_obs_event "error" "dependency_manifest" "failure" \
    --session-id "$SESSION_ID" \
    --message "Missing or empty requirements manifest" \
    --counts-json '{"total_packages":0}'
  exit 0
fi

hook_obs_event "info" "dependency_manifest" "loaded" \
  --session-id "$SESSION_ID" \
  --counts-json "{\"total_packages\":${#REQUIREMENTS[@]}}"

PYTHON_VERSION="$(python3 - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
)"
MANIFEST_SHA256="$(manifest_checksum "$MANIFEST_PATH")"
CACHE_DIR="$HOOK_LOG_DIR/cache"
CACHE_KEY="${PYTHON_VERSION}_${MANIFEST_SHA256}"
STAMP_FILE="$CACHE_DIR/setup-${CACHE_KEY}.stamp"

FORCE_REFRESH=0
if is_truthy "$FORCE_REFRESH_RAW"; then
  FORCE_REFRESH=1
fi

if [ "$FORCE_REFRESH" -eq 1 ]; then
  hook_log_activity "[setup] Force-refresh requested; bypassing cache stamp"
  hook_obs_event "info" "dependency_cache" "bypass" \
    --session-id "$SESSION_ID" \
    --message "force refresh requested"
elif [ -f "$STAMP_FILE" ]; then
  hook_obs_event "info" "dependency_cache" "hit" \
    --session-id "$SESSION_ID" \
    --counts-json '{"cache_hit":1}'
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="Dependency cache hit"
  hook_obs_event "info" "skip_reason" "skipped" \
    --session-id "$SESSION_ID" \
    --message "dependency cache hit"
  exit 0
else
  hook_obs_event "info" "dependency_cache" "miss" \
    --session-id "$SESSION_ID" \
    --counts-json '{"cache_hit":0}'
fi

declare -a IMPORT_NAMES
IMPORT_NAMES=()
for requirement in "${REQUIREMENTS[@]}"; do
  IMPORT_NAMES+=("$(requirement_to_import "$requirement")")
done

missing_output="$(python3 - "${IMPORT_NAMES[@]}" <<'PY'
import importlib.util
import sys

for module_name in sys.argv[1:]:
    if importlib.util.find_spec(module_name) is None:
        print(module_name)
PY
)"
missing_check_exit=$?

if [ "$missing_check_exit" -ne 0 ]; then
  hook_log_activity "[setup] Python import check failed (exit $missing_check_exit)"
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="Python import check failed (exit $missing_check_exit)"
  hook_obs_event "error" "subprocess_exit" "failure" \
    --session-id "$SESSION_ID" \
    --counts-json "{\"command\":\"python_import_check\",\"exit_code\":$missing_check_exit}"
  exit 0
fi

declare -a MISSING_IMPORTS
MISSING_IMPORTS=()
if [ -n "$missing_output" ]; then
  while IFS= read -r module_name; do
    [ -n "$module_name" ] && MISSING_IMPORTS+=("$module_name")
  done <<< "$missing_output"
fi

declare -a MISSING_REQUIREMENTS
MISSING_REQUIREMENTS=()
for idx in "${!IMPORT_NAMES[@]}"; do
  module_name="${IMPORT_NAMES[$idx]}"
  for missing_module in "${MISSING_IMPORTS[@]}"; do
    if [ "$module_name" = "$missing_module" ]; then
      MISSING_REQUIREMENTS+=("${REQUIREMENTS[$idx]}")
      break
    fi
  done
done

if [ "${#MISSING_REQUIREMENTS[@]}" -eq 0 ]; then
  hook_obs_event "info" "dependency_check" "success" \
    --session-id "$SESSION_ID" \
    --counts-json "{\"missing_packages\":0,\"total_packages\":${#REQUIREMENTS[@]}}"
  hook_obs_event "info" "subprocess_exit" "success" \
    --session-id "$SESSION_ID" \
    --counts-json '{"command":"python_import_check","exit_code":0}'

  if write_cache_stamp; then
    hook_log_activity "[setup] Dependency check passed; cache stamp updated"
  else
    hook_log_activity "[setup] Dependency check passed; failed to write cache stamp"
  fi

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
  --session-id "$SESSION_ID" \
  --counts-json "{\"missing_packages\":${#MISSING_REQUIREMENTS[@]},\"total_packages\":${#REQUIREMENTS[@]}}"

missing_csv="$(IFS=,; echo "${MISSING_REQUIREMENTS[*]}")"
hook_log_activity "[setup] Missing Python dependencies (${missing_csv}); installing only missing packages"

if "${PIP_COMMAND[@]}" install --quiet --disable-pip-version-check "${MISSING_REQUIREMENTS[@]}" 2>>"$HOOK_ACTIVITY_LOG"; then
  hook_log_activity "[setup] Python dependencies installed successfully"
  hook_obs_event "info" "subprocess_exit" "success" \
    --session-id "$SESSION_ID" \
    --counts-json '{"command":"pip_install","exit_code":0}'

  if write_cache_stamp; then
    hook_obs_event "info" "dependency_cache" "write" \
      --session-id "$SESSION_ID" \
      --counts-json '{"cache_stamp_written":1}'
  else
    hook_log_activity "[setup] Failed to write cache stamp after install"
    hook_obs_event "warn" "dependency_cache" "write_failed" \
      --session-id "$SESSION_ID"
  fi

  HOOK_OUTCOME="success"
  HOOK_OUTCOME_MESSAGE="Installed missing dependencies"
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
