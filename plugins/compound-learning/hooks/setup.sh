#!/bin/bash

# Auto-install Python dependencies for compound-learning plugin
# Runs on SessionStart to ensure deps are ready before other hooks fire
# Manifest-driven and cache-aware:
# - Warm starts validate a dependency cache stamp and skip pip
# - Cold starts install only missing requirements
# - Stale cache entries self-heal by reinstalling missing requirements
# Exits 0 always to avoid blocking session start

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/observability.sh" || exit 0

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$HOOK_DIR/.." && pwd)}"
REQUIREMENTS_FILE="${LEARNINGS_SETUP_REQUIREMENTS_FILE:-$PLUGIN_ROOT/requirements-runtime.txt}"
PYTHON_BIN="${LEARNINGS_SETUP_PYTHON_BIN:-python3}"
PIP_BIN="${LEARNINGS_SETUP_PIP_BIN:-pip}"
CACHE_DIR="${LEARNINGS_SETUP_CACHE_DIR:-$HOME/.claude/plugins/compound-learning/setup-cache}"

hook_log_init "setup"
SESSION_ID="${CLAUDE_SESSION_ID:-}"
hook_set_session_context "$SESSION_ID"
HOOK_OUTCOME="success"
HOOK_OUTCOME_MESSAGE=""
SETUP_TMP_DIR=""

is_truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

setup_mktemp_dir() {
  local dir
  dir="$(mktemp -d 2>/dev/null)"
  if [ -z "$dir" ]; then
    dir="$(mktemp -d -t compound-learning-setup-XXXXXX 2>/dev/null)"
  fi
  echo "$dir"
}

emit_subprocess_exit_event() {
  local command_name="$1"
  local exit_code="$2"
  local level="warn"
  local status="failure"
  if [ "$exit_code" -eq 0 ]; then
    level="info"
    status="success"
  fi

  hook_obs_event "$level" "subprocess_exit" "$status" \
    --session-id "$SESSION_ID" \
    --counts-json "{\"command\":\"$command_name\",\"exit_code\":$exit_code}"
}

run_manifest_parse() {
  local output_file="$1"
  "$PYTHON_BIN" - "$REQUIREMENTS_FILE" "$output_file" <<'PY'
import hashlib
import json
import pathlib
import re
import sys

requirements_path = pathlib.Path(sys.argv[1])
output_path = pathlib.Path(sys.argv[2])

if not requirements_path.exists():
    raise SystemExit(f"missing requirements file: {requirements_path}")

dependencies = []
seen = set()
for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if "#" in line:
        line = line.split("#", 1)[0].strip()
    if not line:
        continue

    requirement = line
    import_name = ""
    if "|" in line:
        requirement, import_name = line.split("|", 1)
        requirement = requirement.strip()
        import_name = import_name.strip()

    if not requirement:
        continue

    requirement_base = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip()
    requirement_base = requirement_base.split()[0].strip()
    if not requirement_base:
        continue

    if not import_name:
        import_name = requirement_base.lower().replace("-", "_").replace(".", "_")

    dep_key = (requirement, import_name)
    if dep_key in seen:
        continue
    seen.add(dep_key)
    dependencies.append(
        {
            "requirement": requirement,
            "requirement_base": requirement_base,
            "import": import_name,
        }
    )

if not dependencies:
    raise SystemExit("requirements file has no valid dependencies")

canonical_state = "\n".join(
    f"{dep['requirement']}|{dep['import']}" for dep in dependencies
) + "\n"
manifest_sha = hashlib.sha256(canonical_state.encode("utf-8")).hexdigest()
python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

state = {
    "requirements_file": str(requirements_path.resolve()),
    "manifest_sha": manifest_sha,
    "python_version": python_version,
    "dependency_count": len(dependencies),
    "dependencies": dependencies,
}
output_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
PY
}

run_import_probe() {
  local deps_file="$1"
  local output_file="$2"
  local command_name="$3"
  local validate_origin="$4"
  local exit_code=0

  if "$PYTHON_BIN" - "$deps_file" "$output_file" "$validate_origin" <<'PY'
import importlib.util
import json
import pathlib
import sys

deps_path = pathlib.Path(sys.argv[1])
out_path = pathlib.Path(sys.argv[2])
validate_origin = sys.argv[3] == "1"

dependencies = json.loads(deps_path.read_text(encoding="utf-8"))
present = []
missing = []
stale = []

for dep in dependencies:
    requirement = dep.get("requirement", "").strip()
    import_name = dep.get("import", "").strip()
    expected_origin = dep.get("origin", "").strip()
    if not requirement or not import_name:
        continue

    spec = importlib.util.find_spec(import_name)
    if spec is None:
        missing.append(
            {
                "requirement": requirement,
                "import": import_name,
                "reason": "missing_spec",
            }
        )
        continue

    if spec.origin and spec.origin != "namespace":
        resolved_origin = spec.origin
    elif spec.submodule_search_locations:
        resolved_origin = next(iter(spec.submodule_search_locations), "")
    else:
        resolved_origin = "<built-in>"

    if validate_origin and expected_origin and expected_origin != resolved_origin:
        stale.append(
            {
                "requirement": requirement,
                "import": import_name,
                "expected_origin": expected_origin,
                "origin": resolved_origin,
            }
        )
        missing.append(
            {
                "requirement": requirement,
                "import": import_name,
                "reason": "origin_mismatch",
                "expected_origin": expected_origin,
                "origin": resolved_origin,
            }
        )
        continue

    present.append(
        {
            "requirement": requirement,
            "import": import_name,
            "origin": resolved_origin,
        }
    )

result = {
    "checked": len(dependencies),
    "missing": missing,
    "present": present,
    "stale": stale,
}
out_path.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
sys.exit(0 if not missing else 1)
PY
  then
    exit_code=0
  else
    exit_code=$?
  fi

  emit_subprocess_exit_event "$command_name" "$exit_code"
  return "$exit_code"
}

extract_manifest_metadata() {
  local manifest_state_file="$1"
  local deps_output_file="$2"
  "$PYTHON_BIN" - "$manifest_state_file" "$deps_output_file" <<'PY'
import json
import pathlib
import sys

state_path = pathlib.Path(sys.argv[1])
deps_path = pathlib.Path(sys.argv[2])
state = json.loads(state_path.read_text(encoding="utf-8"))
dependencies = state.get("dependencies", []) or []

deps_path.write_text(
    json.dumps(dependencies, sort_keys=True), encoding="utf-8"
)

print(state.get("manifest_sha", ""))
print(state.get("python_version", ""))
print(state.get("dependency_count", len(dependencies)))
PY
}

write_dependencies_from_stamp() {
  local stamp_file="$1"
  local output_file="$2"
  "$PYTHON_BIN" - "$stamp_file" "$output_file" <<'PY'
import json
import pathlib
import sys

stamp = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
dependencies = stamp.get("dependencies", [])
if not dependencies:
    raise SystemExit("stamp file has no dependencies")
pathlib.Path(sys.argv[2]).write_text(
    json.dumps(dependencies, sort_keys=True), encoding="utf-8"
)
PY
}

write_install_list_from_probe() {
  local probe_file="$1"
  local output_file="$2"
  "$PYTHON_BIN" - "$probe_file" "$output_file" <<'PY'
import json
import pathlib
import sys

probe = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
requirements = []
seen = set()
for entry in probe.get("missing", []):
    requirement = entry.get("requirement", "").strip()
    if not requirement or requirement in seen:
        continue
    seen.add(requirement)
    requirements.append(requirement)
pathlib.Path(sys.argv[2]).write_text("\n".join(requirements), encoding="utf-8")
PY
}

count_probe_missing() {
  local probe_file="$1"
  "$PYTHON_BIN" - "$probe_file" <<'PY'
import json
import pathlib
import sys

probe = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(len(probe.get("missing", [])))
PY
}

write_install_list_from_manifest() {
  local deps_file="$1"
  local output_file="$2"
  "$PYTHON_BIN" - "$deps_file" "$output_file" <<'PY'
import json
import pathlib
import sys

dependencies = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
requirements = []
seen = set()
for dep in dependencies:
    requirement = dep.get("requirement", "").strip()
    if not requirement or requirement in seen:
        continue
    seen.add(requirement)
    requirements.append(requirement)
pathlib.Path(sys.argv[2]).write_text("\n".join(requirements), encoding="utf-8")
PY
}

write_cache_stamp() {
  local manifest_state_file="$1"
  local probe_file="$2"
  local cache_key="$3"
  local stamp_file="$4"
  "$PYTHON_BIN" - "$manifest_state_file" "$probe_file" "$cache_key" "$stamp_file" <<'PY'
import datetime
import json
import pathlib
import sys

manifest_state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
probe = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
cache_key = sys.argv[3]
stamp_path = pathlib.Path(sys.argv[4])

present_by_key = {
    (entry.get("requirement", ""), entry.get("import", "")): entry.get("origin", "")
    for entry in probe.get("present", [])
}

dependencies = []
for dep in manifest_state.get("dependencies", []):
    requirement = dep.get("requirement", "")
    import_name = dep.get("import", "")
    origin = present_by_key.get((requirement, import_name), "")
    if not requirement or not import_name:
        continue
    dependencies.append(
        {
            "requirement": requirement,
            "import": import_name,
            "origin": origin,
        }
    )

stamp = {
    "cache_key": cache_key,
    "manifest_sha": manifest_state.get("manifest_sha"),
    "python_version": manifest_state.get("python_version"),
    "dependency_count": len(dependencies),
    "generated_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "dependencies": dependencies,
}
stamp_path.write_text(json.dumps(stamp, sort_keys=True), encoding="utf-8")
PY
}

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

  if [ -n "$SETUP_TMP_DIR" ] && [ -d "$SETUP_TMP_DIR" ]; then
    rm -rf "$SETUP_TMP_DIR" 2>/dev/null || true
  fi
}
trap setup_hook_finalize EXIT

hook_obs_event "info" "hook_start" "start" --session-id "$SESSION_ID"
hook_log_activity "[setup] Bootstrap started"

if [ ! -f "$REQUIREMENTS_FILE" ]; then
  hook_log_activity "[setup] Requirements file not found: $REQUIREMENTS_FILE"
  hook_obs_event "error" "dependency_manifest" "failure" \
    --session-id "$SESSION_ID" \
    --message "requirements file not found"
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="Requirements file missing"
  exit 0
fi

SETUP_TMP_DIR="$(setup_mktemp_dir)"
if [ -z "$SETUP_TMP_DIR" ] || [ ! -d "$SETUP_TMP_DIR" ]; then
  hook_log_activity "[setup] Failed to create temporary directory"
  hook_obs_event "error" "dependency_bootstrap" "failure" \
    --session-id "$SESSION_ID" \
    --message "failed to create temp directory"
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="Failed to create temp directory"
  exit 0
fi

MANIFEST_STATE_FILE="$SETUP_TMP_DIR/manifest-state.json"
MANIFEST_DEPS_FILE="$SETUP_TMP_DIR/manifest-dependencies.json"
CACHE_PROBE_FILE="$SETUP_TMP_DIR/cache-probe.json"
IMPORT_PROBE_FILE="$SETUP_TMP_DIR/import-probe.json"
POST_INSTALL_PROBE_FILE="$SETUP_TMP_DIR/post-install-probe.json"
INSTALL_LIST_FILE="$SETUP_TMP_DIR/install-requirements.txt"
STAMP_DEPS_FILE="$SETUP_TMP_DIR/stamp-dependencies.json"

if run_manifest_parse "$MANIFEST_STATE_FILE"; then
  emit_subprocess_exit_event "python_manifest_parse" 0
else
  parse_exit=$?
  emit_subprocess_exit_event "python_manifest_parse" "$parse_exit"
  hook_log_activity "[setup] Failed to parse dependency manifest (exit $parse_exit)"
  hook_obs_event "error" "dependency_manifest" "failure" \
    --session-id "$SESSION_ID" \
    --message "failed to parse requirements manifest"
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="Dependency manifest parse failed (exit $parse_exit)"
  exit 0
fi

readarray -t MANIFEST_META < <(extract_manifest_metadata "$MANIFEST_STATE_FILE" "$MANIFEST_DEPS_FILE")
MANIFEST_SHA="${MANIFEST_META[0]:-}"
PYTHON_VERSION="${MANIFEST_META[1]:-}"
DEPENDENCY_COUNT="${MANIFEST_META[2]:-0}"

if [ -z "$MANIFEST_SHA" ] || [ -z "$PYTHON_VERSION" ]; then
  hook_log_activity "[setup] Missing manifest metadata after parse"
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="Manifest metadata incomplete"
  exit 0
fi

mkdir -p "$CACHE_DIR" 2>/dev/null || true
CACHE_KEY="${PYTHON_VERSION}_${MANIFEST_SHA}"
CACHE_KEY="${CACHE_KEY//[^a-zA-Z0-9._-]/_}"
STAMP_FILE="$CACHE_DIR/setup-${CACHE_KEY}.stamp.json"

FORCE_REFRESH_VALUE="${LEARNINGS_SETUP_FORCE_REFRESH:-}"
FORCE_REFRESH_SOURCE=""
if [ -n "$FORCE_REFRESH_VALUE" ]; then
  FORCE_REFRESH_SOURCE="LEARNINGS_SETUP_FORCE_REFRESH"
else
  FORCE_REFRESH_VALUE="${LEARNINGS_FORCE_SETUP_REFRESH:-}"
  if [ -n "$FORCE_REFRESH_VALUE" ]; then
    FORCE_REFRESH_SOURCE="LEARNINGS_FORCE_SETUP_REFRESH"
  fi
fi

FORCE_REFRESH=0
if is_truthy "${FORCE_REFRESH_VALUE:-}"; then
  FORCE_REFRESH=1
fi

if [ "$FORCE_REFRESH" -eq 1 ]; then
  hook_log_activity "[setup] Force refresh requested via $FORCE_REFRESH_SOURCE"
  hook_obs_event "info" "dependency_cache" "refresh" \
    --session-id "$SESSION_ID" \
    --message "force refresh requested"
  rm -f "$STAMP_FILE" 2>/dev/null || true
  if ! write_install_list_from_manifest "$MANIFEST_DEPS_FILE" "$INSTALL_LIST_FILE"; then
    hook_log_activity "[setup] Failed to build install list for force refresh"
    HOOK_OUTCOME="error"
    HOOK_OUTCOME_MESSAGE="Failed to build force refresh install list"
    exit 0
  fi
fi

if [ "$FORCE_REFRESH" -eq 0 ] && [ -f "$STAMP_FILE" ]; then
  if write_dependencies_from_stamp "$STAMP_FILE" "$STAMP_DEPS_FILE"; then
    if run_import_probe "$STAMP_DEPS_FILE" "$CACHE_PROBE_FILE" "python_cache_validate" 1; then
      hook_obs_event "info" "dependency_cache" "hit" \
        --session-id "$SESSION_ID" \
        --counts-json "{\"dependency_count\":$DEPENDENCY_COUNT}"
      HOOK_OUTCOME="skipped"
      HOOK_OUTCOME_MESSAGE="Dependencies cache hit"
      hook_obs_event "info" "skip_reason" "skipped" \
        --session-id "$SESSION_ID" \
        --message "dependency cache hit"
      exit 0
    fi

    cache_missing_count="$(count_probe_missing "$CACHE_PROBE_FILE")"
    hook_log_activity "[setup] Dependency cache stale; missing=$cache_missing_count"
    hook_obs_event "warn" "dependency_cache" "stale" \
      --session-id "$SESSION_ID" \
      --counts-json "{\"missing_packages\":$cache_missing_count}"
    rm -f "$STAMP_FILE" 2>/dev/null || true
    if ! write_install_list_from_probe "$CACHE_PROBE_FILE" "$INSTALL_LIST_FILE"; then
      hook_log_activity "[setup] Failed to derive install list from stale cache probe"
      HOOK_OUTCOME="error"
      HOOK_OUTCOME_MESSAGE="Failed to derive stale cache install list"
      exit 0
    fi
  else
    hook_log_activity "[setup] Dependency cache stamp unreadable, invalidating"
    hook_obs_event "warn" "dependency_cache" "stale" \
      --session-id "$SESSION_ID" \
      --message "unreadable dependency cache stamp"
    rm -f "$STAMP_FILE" 2>/dev/null || true
  fi
elif [ "$FORCE_REFRESH" -eq 0 ]; then
  hook_obs_event "info" "dependency_cache" "miss" \
    --session-id "$SESSION_ID" \
    --counts-json "{\"dependency_count\":$DEPENDENCY_COUNT}"
fi

if [ ! -f "$INSTALL_LIST_FILE" ]; then
  if run_import_probe "$MANIFEST_DEPS_FILE" "$IMPORT_PROBE_FILE" "python_import_check" 0; then
    hook_obs_event "info" "dependency_check" "success" \
      --session-id "$SESSION_ID" \
      --counts-json "{\"checked_dependencies\":$DEPENDENCY_COUNT,\"missing_packages\":0}"
    if write_cache_stamp "$MANIFEST_STATE_FILE" "$IMPORT_PROBE_FILE" "$CACHE_KEY" "$STAMP_FILE"; then
      hook_obs_event "info" "dependency_cache" "updated" \
        --session-id "$SESSION_ID" \
        --counts-json "{\"dependency_count\":$DEPENDENCY_COUNT}"
    else
      hook_obs_event "warn" "dependency_cache" "write_failed" \
        --session-id "$SESSION_ID" \
        --message "failed to write dependency cache stamp"
    fi
    HOOK_OUTCOME="skipped"
    HOOK_OUTCOME_MESSAGE="Dependencies already installed"
    hook_obs_event "info" "skip_reason" "skipped" \
      --session-id "$SESSION_ID" \
      --message "dependencies already installed"
    exit 0
  fi

  missing_count="$(count_probe_missing "$IMPORT_PROBE_FILE")"
  hook_obs_event "info" "dependency_check" "missing" \
    --session-id "$SESSION_ID" \
    --counts-json "{\"checked_dependencies\":$DEPENDENCY_COUNT,\"missing_packages\":$missing_count}"
  if ! write_install_list_from_probe "$IMPORT_PROBE_FILE" "$INSTALL_LIST_FILE"; then
    hook_log_activity "[setup] Failed to derive install list from import probe"
    HOOK_OUTCOME="error"
    HOOK_OUTCOME_MESSAGE="Failed to derive install list"
    exit 0
  fi
fi

mapfile -t INSTALL_REQUIREMENTS < "$INSTALL_LIST_FILE"

if [ "${#INSTALL_REQUIREMENTS[@]}" -eq 0 ]; then
  hook_obs_event "info" "dependency_install" "skipped" \
    --session-id "$SESSION_ID" \
    --message "no missing requirements to install"
  HOOK_OUTCOME="skipped"
  HOOK_OUTCOME_MESSAGE="No dependency installation needed"
  exit 0
fi

hook_log_activity "[setup] Installing ${#INSTALL_REQUIREMENTS[@]} requirements"
if "$PIP_BIN" install --quiet "${INSTALL_REQUIREMENTS[@]}" 2>>"$HOOK_ACTIVITY_LOG"; then
  emit_subprocess_exit_event "pip_install" 0
  if run_import_probe "$MANIFEST_DEPS_FILE" "$POST_INSTALL_PROBE_FILE" "python_post_install_check" 0; then
    if write_cache_stamp "$MANIFEST_STATE_FILE" "$POST_INSTALL_PROBE_FILE" "$CACHE_KEY" "$STAMP_FILE"; then
      hook_obs_event "info" "dependency_cache" "updated" \
        --session-id "$SESSION_ID" \
        --counts-json "{\"dependency_count\":$DEPENDENCY_COUNT}"
    else
      hook_obs_event "warn" "dependency_cache" "write_failed" \
        --session-id "$SESSION_ID" \
        --message "failed to write dependency cache stamp"
    fi
    hook_log_activity "[setup] Python dependencies installed successfully"
    hook_obs_event "info" "dependency_install" "success" \
      --session-id "$SESSION_ID" \
      --counts-json "{\"installed_packages\":${#INSTALL_REQUIREMENTS[@]}}"
    HOOK_OUTCOME="success"
    HOOK_OUTCOME_MESSAGE="Dependencies installed successfully"
  else
    post_check_exit=$?
    hook_log_activity "[setup] Post-install dependency validation failed (exit $post_check_exit)"
    hook_obs_event "error" "dependency_install" "failure" \
      --session-id "$SESSION_ID" \
      --message "post-install validation failed"
    HOOK_OUTCOME="error"
    HOOK_OUTCOME_MESSAGE="Post-install dependency validation failed (exit $post_check_exit)"
  fi
else
  pip_exit=$?
  emit_subprocess_exit_event "pip_install" "$pip_exit"
  hook_log_activity "[setup] pip install failed (exit $pip_exit), plugin may not work correctly"
  hook_obs_event "error" "dependency_install" "failure" \
    --session-id "$SESSION_ID" \
    --counts-json "{\"attempted_packages\":${#INSTALL_REQUIREMENTS[@]}}"
  HOOK_OUTCOME="error"
  HOOK_OUTCOME_MESSAGE="pip install failed (exit $pip_exit)"
fi

exit 0
