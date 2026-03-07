import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SETUP_SCRIPT = PLUGIN_ROOT / "hooks" / "setup.sh"
OBSERVABILITY_SCRIPT = PLUGIN_ROOT / "hooks" / "observability.sh"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def _write_fake_pip(path: Path) -> None:
    _write(
        path,
        """
        #!/usr/bin/env python3
        import json
        import os
        import pathlib
        import re
        import sys

        log_path = os.environ.get("FAKE_PIP_LOG")
        install_map = json.loads(os.environ.get("FAKE_PIP_IMPORT_MAP_JSON", "{}"))
        fail_for = {
            token.strip()
            for token in os.environ.get("FAKE_PIP_FAIL_FOR", "").split(",")
            if token.strip()
        }

        def normalize_requirement(req: str) -> str:
            return re.split(r"[<>=!~;\\[]", req, maxsplit=1)[0].strip()

        argv = sys.argv[1:]
        if not argv or argv[0] != "install":
            sys.exit(0)

        requested = [arg for arg in argv[1:] if not arg.startswith("-")]
        requirements = []
        for raw in requested:
            normalized = normalize_requirement(raw)
            if normalized:
                requirements.append(normalized)

        if log_path:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"requirements": requirements}) + "\\n")

        if fail_for.intersection(requirements):
            sys.exit(2)

        site_packages = pathlib.Path(os.environ["FAKE_SITE_PACKAGES"])
        site_packages.mkdir(parents=True, exist_ok=True)

        for requirement in requirements:
            import_name = install_map.get(requirement, requirement.replace("-", "_").replace(".", "_"))
            package_dir = site_packages / import_name
            package_dir.mkdir(parents=True, exist_ok=True)
            (package_dir / "__init__.py").write_text(
                f"# generated for {requirement}\\n", encoding="utf-8"
            )

        sys.exit(0)
        """,
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _prepare_plugin_copy(tmp_path: Path, requirements_runtime: str) -> Path:
    plugin_dir = tmp_path / "compound-learning"
    hooks_dir = plugin_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(SETUP_SCRIPT, hooks_dir / "setup.sh")
    shutil.copy2(OBSERVABILITY_SCRIPT, hooks_dir / "observability.sh")
    _write(plugin_dir / "requirements-runtime.txt", requirements_runtime)

    return plugin_dir


def _build_env(tmp_path: Path, plugin_dir: Path, import_map: dict[str, str]) -> tuple[dict[str, str], Path, Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    fake_pip = fake_bin / "pip"
    _write_fake_pip(fake_pip)

    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)

    obs_log = tmp_path / "observability.jsonl"
    pip_log = tmp_path / "pip.log"

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home_dir),
            "CLAUDE_PLUGIN_ROOT": str(plugin_dir),
            "LEARNINGS_OBS_ENABLED": "true",
            "LEARNINGS_OBS_LEVEL": "debug",
            "LEARNINGS_OBS_LOG_PATH": str(obs_log),
            "PYTHONPATH": str(site_packages),
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "FAKE_SITE_PACKAGES": str(site_packages),
            "FAKE_PIP_LOG": str(pip_log),
            "FAKE_PIP_IMPORT_MAP_JSON": json.dumps(import_map),
        }
    )
    return env, obs_log, pip_log, site_packages


def _run_setup(plugin_dir: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["bash", str(plugin_dir / "hooks" / "setup.sh")],
        cwd=plugin_dir,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def _read_pip_log(pip_log: Path) -> list[list[str]]:
    if not pip_log.exists():
        return []
    entries = []
    for line in pip_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        entries.append(payload.get("requirements", []))
    return entries


def _read_obs_events(obs_log: Path) -> list[dict]:
    if not obs_log.exists():
        return []
    events = []
    for line in obs_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events


def _find_subprocess_exit(events: list[dict], command: str, exit_code: int) -> bool:
    for event in events:
        if event.get("operation") != "subprocess_exit":
            continue
        counts = event.get("counts") or {}
        if counts.get("command") == command and counts.get("exit_code") == exit_code:
            return True
    return False


def test_cache_hit_skips_reinstall(tmp_path):
    plugin_dir = _prepare_plugin_copy(
        tmp_path,
        """
        alpha-pkg
        beta-pkg
        """,
    )
    env, obs_log, pip_log, _site_packages = _build_env(tmp_path, plugin_dir, import_map={})

    _run_setup(plugin_dir, env)
    _run_setup(plugin_dir, env)

    installs = _read_pip_log(pip_log)
    assert installs == [["alpha-pkg", "beta-pkg"]]

    events = _read_obs_events(obs_log)
    assert any(e.get("operation") == "dependency_cache" and e.get("status") == "miss" for e in events)
    assert any(e.get("operation") == "dependency_cache" and e.get("status") == "hit" for e in events)
    assert _find_subprocess_exit(events, "python_cache_validate", 0)


def test_manifest_checksum_change_installs_only_new_dependencies(tmp_path):
    plugin_dir = _prepare_plugin_copy(
        tmp_path,
        """
        alpha-pkg
        """,
    )
    env, _obs_log, pip_log, _site_packages = _build_env(tmp_path, plugin_dir, import_map={})

    _run_setup(plugin_dir, env)

    _write(
        plugin_dir / "requirements-runtime.txt",
        """
        alpha-pkg
        beta-pkg
        """,
    )
    _run_setup(plugin_dir, env)

    installs = _read_pip_log(pip_log)
    assert installs[0] == ["alpha-pkg"]
    assert installs[1] == ["beta-pkg"]


def test_stale_cache_recovers_missing_dependency(tmp_path):
    plugin_dir = _prepare_plugin_copy(
        tmp_path,
        """
        alpha-pkg
        beta-pkg
        """,
    )
    env, obs_log, pip_log, site_packages = _build_env(tmp_path, plugin_dir, import_map={})

    _run_setup(plugin_dir, env)

    shutil.rmtree(site_packages / "beta_pkg")

    _run_setup(plugin_dir, env)

    installs = _read_pip_log(pip_log)
    assert installs == [["alpha-pkg", "beta-pkg"], ["beta-pkg"]]

    events = _read_obs_events(obs_log)
    assert any(e.get("operation") == "dependency_cache" and e.get("status") == "stale" for e in events)
    assert _find_subprocess_exit(events, "python_cache_validate", 1)


def test_observability_tracks_import_check_and_cache_paths(tmp_path):
    plugin_dir = _prepare_plugin_copy(
        tmp_path,
        """
        gamma-pkg|gamma_mod
        """,
    )
    env, obs_log, _pip_log, _site_packages = _build_env(
        tmp_path,
        plugin_dir,
        import_map={"gamma-pkg": "gamma_mod"},
    )

    _run_setup(plugin_dir, env)
    _run_setup(plugin_dir, env)

    events = _read_obs_events(obs_log)
    assert _find_subprocess_exit(events, "python_import_check", 1)
    assert _find_subprocess_exit(events, "pip_install", 0)
    assert _find_subprocess_exit(events, "python_post_install_check", 0)
    assert _find_subprocess_exit(events, "python_cache_validate", 0)

    missing_events = [
        e for e in events if e.get("operation") == "dependency_check" and e.get("status") == "missing"
    ]
    assert missing_events
    assert missing_events[0].get("counts", {}).get("missing_packages") == 1
