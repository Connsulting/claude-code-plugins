import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

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


def _write_python_wrapper(bin_dir: Path) -> None:
    python_path = bin_dir / "python3"
    real_python = shutil.which("python3") or sys.executable
    quoted_real_python = shlex.quote(real_python)
    python_path.write_text(
        f"""#!/bin/bash
set -euo pipefail

printf '%s\\n' "$*" >> "${{PYTHON_CALL_LOG}}"
exec {quoted_real_python} "$@"
""",
        encoding="utf-8",
    )
    python_path.chmod(0o755)


def _build_env(tmp_path: Path, plugin_root: Path, module_dir: Path, bin_dir: Path) -> Tuple[Dict[str, str], Path, Path, Path, Path]:
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    pip_log = tmp_path / "pip.log"
    python_call_log = tmp_path / "python-calls.log"
    obs_log = tmp_path / "observability.jsonl"

    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    env["HOME"] = str(home_dir)
    env["PIP_LOG"] = str(pip_log)
    env["PYTHON_CALL_LOG"] = str(python_call_log)
    env["MODULE_DIR"] = str(module_dir)
    env["LEARNINGS_OBS_ENABLED"] = "false"
    env["LEARNINGS_OBS_LOG_PATH"] = str(obs_log)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{module_dir}:{existing_pythonpath}" if existing_pythonpath else str(module_dir)

    return env, home_dir, pip_log, python_call_log, obs_log


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


def _read_python_calls(python_call_log: Path) -> List[str]:
    if not python_call_log.exists():
        return []
    return [line for line in python_call_log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_obs_events(obs_log: Path) -> List[Dict[str, Any]]:
    if not obs_log.exists():
        return []
    return [json.loads(line) for line in obs_log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_setup_cache_hit_skips_import_probe_and_recovers_from_dependency_drift(tmp_path):
    plugin_root = tmp_path / "plugin"
    module_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    module_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    _write_runtime_manifest(plugin_root, ["alpha-pkg", "beta-pkg"])
    (module_dir / "alpha_pkg.py").write_text("ALPHA = True\n", encoding="utf-8")
    _write_fake_pip(bin_dir)
    _write_python_wrapper(bin_dir)

    env, home_dir, pip_log, python_call_log, _ = _build_env(tmp_path, plugin_root, module_dir, bin_dir)

    first_run = _run_setup(env)
    assert first_run.returncode == 0

    first_calls = _read_pip_calls(pip_log)
    assert len(first_calls) == 1
    assert "beta-pkg" in first_calls[0]
    assert "alpha-pkg" not in first_calls[0]
    assert len(_cache_stamp_files(home_dir)) == 1

    python_call_log.write_text("", encoding="utf-8")
    warm_run = _run_setup(env)
    assert warm_run.returncode == 0
    assert len(_read_pip_calls(pip_log)) == 1
    warm_python_calls = _read_python_calls(python_call_log)
    assert not any("alpha_pkg" in call or "beta_pkg" in call for call in warm_python_calls)

    (module_dir / "beta_pkg.py").unlink(missing_ok=True)

    python_call_log.write_text("", encoding="utf-8")
    drift_run = _run_setup(env)
    assert drift_run.returncode == 0

    calls = _read_pip_calls(pip_log)
    assert len(calls) == 2
    assert "beta-pkg" in calls[1]
    drift_python_calls = _read_python_calls(python_call_log)
    assert any("alpha_pkg" in call and "beta_pkg" in call for call in drift_python_calls)
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
    _write_python_wrapper(bin_dir)

    env, home_dir, pip_log, _, _ = _build_env(tmp_path, plugin_root, module_dir, bin_dir)

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


def test_setup_missing_dependencies_do_not_emit_fake_python_import_failure(tmp_path):
    if shutil.which("jq") is None:
        pytest.skip("jq is required for hook observability assertions")

    plugin_root = tmp_path / "plugin"
    module_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    module_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    _write_runtime_manifest(plugin_root, ["alpha-pkg", "beta-pkg"])
    (module_dir / "alpha_pkg.py").write_text("ALPHA = True\n", encoding="utf-8")
    _write_fake_pip(bin_dir)
    _write_python_wrapper(bin_dir)

    env, _, _, _, obs_log = _build_env(tmp_path, plugin_root, module_dir, bin_dir)
    env["LEARNINGS_OBS_ENABLED"] = "true"

    result = _run_setup(env)
    assert result.returncode == 0

    events = _read_obs_events(obs_log)
    python_import_events = [
        event
        for event in events
        if event.get("operation") == "subprocess_exit"
        and isinstance(event.get("counts"), dict)
        and event["counts"].get("command") == "python_import_check"
    ]

    assert python_import_events
    assert all(event.get("status") == "success" for event in python_import_events)
