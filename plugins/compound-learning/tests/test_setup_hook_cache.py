import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PLUGIN_ROOT = Path(__file__).parent.parent
SETUP_SCRIPT = PLUGIN_ROOT / "hooks" / "setup.sh"


def _write_runtime_manifest(plugin_root: Path, requirements: List[str]) -> None:
    plugin_root.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(requirements) + "\n"
    (plugin_root / "requirements-runtime.txt").write_text(payload, encoding="utf-8")


def _write_fake_pip(bin_dir: Path) -> None:
    pip_path = bin_dir / "pip"
    pip_path.write_text(
        """#!/bin/bash
set -euo pipefail

printf '%s\\n' "$*" >> "${PIP_LOG}"
for arg in "$@"; do
  case "$arg" in
    alpha-pkg|beta-pkg|gamma-pkg)
      touch "${MODULE_DIR}/${arg//-/_}.py"
      ;;
  esac
done
""",
        encoding="utf-8",
    )
    pip_path.chmod(0o755)


def _build_env(
    tmp_path: Path,
    plugin_root: Path,
    module_dir: Path,
    bin_dir: Path,
    *,
    observability_enabled: bool = False,
    observability_level: str = "info",
    observability_log_path: Optional[Path] = None,
) -> Tuple[Dict[str, str], Path, Path]:
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    pip_log = tmp_path / "pip.log"

    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    env["HOME"] = str(home_dir)
    env["PIP_LOG"] = str(pip_log)
    env["MODULE_DIR"] = str(module_dir)
    env["LEARNINGS_OBS_ENABLED"] = "true" if observability_enabled else "false"
    env["LEARNINGS_OBS_LEVEL"] = observability_level
    if observability_log_path is not None:
        env["LEARNINGS_OBS_LOG_PATH"] = str(observability_log_path)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{module_dir}:{existing_pythonpath}" if existing_pythonpath else str(module_dir)

    return env, home_dir, pip_log


def _run_setup(env: Dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SETUP_SCRIPT)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )


def _cache_stamp_files(home_dir: Path) -> List[Path]:
    cache_dir = home_dir / ".claude" / "plugins" / "compound-learning" / "cache"
    return sorted(cache_dir.glob("setup-*.stamp")) if cache_dir.exists() else []


def _read_pip_calls(pip_log: Path) -> List[str]:
    if not pip_log.exists():
        return []
    return [line for line in pip_log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_observability_events(log_path: Path) -> List[Dict[str, object]]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _emit_hook_event(
    tmp_path: Path,
    *,
    operation: str,
    status: str,
    extra_json: Optional[str] = None,
) -> List[Dict[str, object]]:
    home_dir = tmp_path / "hook-home"
    home_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "hook-observability.jsonl"

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    env["LEARNINGS_OBS_ENABLED"] = "true"
    env["LEARNINGS_OBS_LEVEL"] = "debug"
    env["LEARNINGS_OBS_LOG_PATH"] = str(log_path)
    env["HOOK_EVENT_OPERATION"] = operation
    env["HOOK_EVENT_STATUS"] = status
    if extra_json is not None:
        env["HOOK_EVENT_EXTRA_JSON"] = extra_json
    else:
        env.pop("HOOK_EVENT_EXTRA_JSON", None)

    script = f"""
set -euo pipefail
source "{PLUGIN_ROOT / "hooks" / "observability.sh"}"
hook_log_init "setup"
if [ -n "${{HOOK_EVENT_EXTRA_JSON:-}}" ]; then
  hook_obs_event "info" "$HOOK_EVENT_OPERATION" "$HOOK_EVENT_STATUS" --session-id "sess-test" --extra-json "$HOOK_EVENT_EXTRA_JSON"
else
  hook_obs_event "info" "$HOOK_EVENT_OPERATION" "$HOOK_EVENT_STATUS" --session-id "sess-test"
fi
"""
    run = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    return _read_observability_events(log_path)


def test_setup_cache_hit_validates_and_recovers_from_dependency_drift(tmp_path):
    plugin_root = tmp_path / "plugin"
    module_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    module_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    _write_runtime_manifest(plugin_root, ["alpha-pkg", "beta-pkg"])
    (module_dir / "alpha_pkg.py").write_text("ALPHA = True\n", encoding="utf-8")
    _write_fake_pip(bin_dir)

    env, home_dir, pip_log = _build_env(tmp_path, plugin_root, module_dir, bin_dir)

    first_run = _run_setup(env)
    assert first_run.returncode == 0

    first_calls = _read_pip_calls(pip_log)
    assert len(first_calls) == 1
    assert "beta-pkg" in first_calls[0]
    assert "alpha-pkg" not in first_calls[0]
    assert len(_cache_stamp_files(home_dir)) == 1

    warm_run = _run_setup(env)
    assert warm_run.returncode == 0
    assert len(_read_pip_calls(pip_log)) == 1

    (module_dir / "beta_pkg.py").unlink(missing_ok=True)

    drift_run = _run_setup(env)
    assert drift_run.returncode == 0

    calls = _read_pip_calls(pip_log)
    assert len(calls) == 2
    assert "beta-pkg" in calls[1]
    assert len(_cache_stamp_files(home_dir)) == 1


def test_setup_cache_invalidates_when_manifest_checksum_changes(tmp_path):
    plugin_root = tmp_path / "plugin"
    module_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    module_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    _write_runtime_manifest(plugin_root, ["alpha-pkg", "beta-pkg"])
    (module_dir / "alpha_pkg.py").write_text("ALPHA = True\n", encoding="utf-8")
    (module_dir / "beta_pkg.py").write_text("BETA = True\n", encoding="utf-8")
    _write_fake_pip(bin_dir)

    env, home_dir, pip_log = _build_env(tmp_path, plugin_root, module_dir, bin_dir)

    warm_run = _run_setup(env)
    assert warm_run.returncode == 0
    assert _read_pip_calls(pip_log) == []
    assert len(_cache_stamp_files(home_dir)) == 1

    _write_runtime_manifest(plugin_root, ["alpha-pkg", "beta-pkg", "gamma-pkg"])

    invalidate_run = _run_setup(env)
    assert invalidate_run.returncode == 0

    calls = _read_pip_calls(pip_log)
    assert len(calls) == 1
    assert "gamma-pkg" in calls[0]
    assert "alpha-pkg" not in calls[0]
    assert "beta-pkg" not in calls[0]
    assert len(_cache_stamp_files(home_dir)) == 1


def test_setup_hook_observability_uses_canonical_taxonomy(tmp_path):
    plugin_root = tmp_path / "plugin"
    module_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    module_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    _write_runtime_manifest(plugin_root, ["alpha-pkg", "beta-pkg"])
    (module_dir / "alpha_pkg.py").write_text("ALPHA = True\n", encoding="utf-8")
    _write_fake_pip(bin_dir)

    observability_log_path = tmp_path / "observability.jsonl"
    env, _home_dir, _pip_log = _build_env(
        tmp_path,
        plugin_root,
        module_dir,
        bin_dir,
        observability_enabled=True,
        observability_level="debug",
        observability_log_path=observability_log_path,
    )

    run = _run_setup(env)
    assert run.returncode == 0

    events = _read_observability_events(observability_log_path)
    assert events

    required_keys = {"timestamp", "level", "component", "operation", "status"}
    canonical_statuses = {"start", "success", "error", "skipped", "empty", "degraded"}
    legacy_statuses = {"failure", "found", "loaded", "bypass", "hit", "miss", "missing", "stale", "write", "write_failed", "fallback"}

    assert all(required_keys.issubset(event.keys()) for event in events)
    assert all(event["component"] == "hook" for event in events)
    assert all(event.get("hook") == "setup" for event in events)
    assert all(event["status"] in canonical_statuses for event in events)
    assert all(event["status"] not in legacy_statuses for event in events)
    assert any(event["operation"] == "hook" and event.get("operation_alias") in {"hook_start", "hook_end"} for event in events)
    assert any(event.get("status_alias") for event in events)


def test_hook_observability_extra_json_cannot_override_taxonomy(tmp_path):
    events = _emit_hook_event(
        tmp_path,
        operation="hook_end",
        status="failure",
        extra_json='{"operation":"override","status":"success","operation_alias":"bad","status_alias":"bad","detail":"kept"}',
    )

    assert len(events) == 1
    event = events[0]
    assert event["operation"] == "hook"
    assert event["status"] == "error"
    assert event["operation_alias"] == "hook_end"
    assert event["status_alias"] == "failure"
    assert event["detail"] == "kept"


def test_hook_observability_maps_no_results_status_to_empty(tmp_path):
    events = _emit_hook_event(
        tmp_path,
        operation="search_complete",
        status="no_results",
    )

    assert len(events) == 1
    event = events[0]
    assert event["operation"] == "search"
    assert event["status"] == "empty"
    assert event["operation_alias"] == "search_complete"
    assert event["status_alias"] == "no_results"
