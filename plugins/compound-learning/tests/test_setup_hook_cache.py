import os
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

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


def _build_env(tmp_path: Path, plugin_root: Path, module_dir: Path, bin_dir: Path) -> Tuple[Dict[str, str], Path, Path]:
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    pip_log = tmp_path / "pip.log"

    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    env["HOME"] = str(home_dir)
    env["PIP_LOG"] = str(pip_log)
    env["MODULE_DIR"] = str(module_dir)
    env["LEARNINGS_OBS_ENABLED"] = "false"
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


def test_setup_installs_only_missing_packages_and_reuses_cache_stamp(tmp_path):
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

    (module_dir / "beta_pkg.py").unlink(missing_ok=True)

    second_run = _run_setup(env)
    assert second_run.returncode == 0

    second_calls = _read_pip_calls(pip_log)
    assert len(second_calls) == 1


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
