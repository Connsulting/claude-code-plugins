#!/usr/bin/env python3
"""
Shared dependency bootstrap helpers for the compound-learning plugin.

This module centralizes dependency detection, installation, and status/logging
for both shell hooks and Python entry points.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib
import importlib.util
import json
import os
import platform
import shutil
import sqlite3 as stdlib_sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

CORE = "core"
EMBEDDING = "embedding"

STATE_READY = "ready"
STATE_INSTALLING = "installing"
STATE_FAILED = "failed"
STATE_MISSING = "missing"

DEFAULT_WAIT_TIMEOUT = 900


class BootstrapError(RuntimeError):
    """Raised when dependency bootstrap fails."""


@dataclass
class BootstrapResult:
    dependency: str
    state: str
    changed: bool = False
    message: str = ""
    backend: str | None = None
    pid: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "dependency": self.dependency,
            "state": self.state,
            "changed": self.changed,
            "message": self.message,
        }
        if self.backend:
            data["backend"] = self.backend
        if self.pid:
            data["pid"] = self.pid
        if self.error:
            data["error"] = self.error
        return data


def state_dir_path() -> Path:
    override = os.environ.get("CLAUDE_PLUGIN_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "compound-learning"


def state_dir() -> Path:
    path = state_dir_path()
    _ensure_directory(path)
    return path


def managed_site_dir_path() -> Path:
    return state_dir_path() / "site-packages"


def managed_site_dir() -> Path:
    path = managed_site_dir_path()
    _ensure_directory(path)
    return path


def legacy_managed_site_dir() -> Path:
    return Path.home() / ".claude" / "plugins" / "compound-learning" / "site-packages"


def log_file() -> Path:
    return state_dir() / "activity.log"


def status_file() -> Path:
    return state_dir() / "bootstrap-status.json"


def lock_file() -> Path:
    return state_dir() / "bootstrap-status.lock"


def plugin_root() -> Path:
    return Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))


def _ensure_directory(path: Path) -> None:
    if path.is_dir():
        return
    if path.is_symlink() and not path.exists():
        raise BootstrapError(f"{path} is a broken symlink")
    if path.exists():
        raise BootstrapError(f"{path} exists and is not a directory")
    path.mkdir(parents=True, exist_ok=True)


def ensure_managed_site_dir_on_path() -> None:
    # Create the managed site-packages directory before probing it so Python
    # does not cache the path as missing on first-run bootstrap.
    site_dirs = [managed_site_dir()]
    legacy_site_dir = legacy_managed_site_dir()
    if legacy_site_dir.is_dir() and legacy_site_dir not in site_dirs:
        site_dirs.append(legacy_site_dir)

    for site_dir in reversed(site_dirs):
        site_dir_str = str(site_dir)
        if site_dir_str not in sys.path:
            sys.path.insert(0, site_dir_str)


def log_activity(message: str) -> None:
    with open(log_file(), "a", encoding="utf-8") as handle:
        handle.write(f"[{_timestamp()}] {message}\n")

def ensure_core_dependencies(wait: bool = True) -> BootstrapResult:
    return ensure_dependency(CORE, wait=wait, background=False)


def ensure_embedding_dependencies(
    wait: bool = True,
    background: bool = False,
) -> BootstrapResult:
    return ensure_dependency(EMBEDDING, wait=wait, background=background)


def prepare_embedding_for_auto_peek() -> BootstrapResult:
    return ensure_embedding_dependencies(wait=False, background=True)


def dependency_ready(dependency: str) -> bool:
    if dependency == CORE:
        return _core_runtime_ready()
    if dependency == EMBEDDING:
        return _module_available("sentence_transformers")
    raise ValueError(f"Unknown dependency group: {dependency}")


def missing_packages(dependency: str) -> list[str]:
    if dependency == CORE:
        packages: list[str] = []
        if not _sqlite_runtime_ready():
            packages.append(_pysqlite_package_name())
        if not _module_available("sqlite_vec"):
            packages.append("sqlite-vec")
        return packages
    if dependency == EMBEDDING:
        return [] if _module_available("sentence_transformers") else ["sentence-transformers"]
    raise ValueError(f"Unknown dependency group: {dependency}")


def read_status() -> dict[str, Any]:
    with _locked_status() as status:
        return json.loads(json.dumps(status))


def probe_dependency(dependency: str) -> BootstrapResult:
    with open(lock_file(), "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            status, source = _load_persisted_status_unlocked()
            if source == "missing":
                return BootstrapResult(
                    dependency=dependency,
                    state=STATE_MISSING,
                    message=f"No persisted bootstrap status for {dependency} dependencies.",
                )
            if source == "corrupt":
                return BootstrapResult(
                    dependency=dependency,
                    state=STATE_MISSING,
                    message=f"Persisted bootstrap status for {dependency} dependencies is unreadable.",
                )

            dependencies = status.get("dependencies")
            dep_state = dependencies.get(dependency) if isinstance(dependencies, dict) else None
            if not isinstance(dep_state, dict):
                return BootstrapResult(
                    dependency=dependency,
                    state=STATE_MISSING,
                    message=f"Persisted bootstrap status for {dependency} dependencies is incomplete.",
                )

            changed = _normalize_probe_state_unlocked(dependency, dep_state)
            if changed:
                _write_status_unlocked(status)

            state = dep_state.get("state", STATE_MISSING)
            if state == STATE_READY:
                return BootstrapResult(
                    dependency=dependency,
                    state=STATE_READY,
                    changed=changed,
                    message=f"{dependency} dependencies are ready",
                )
            if state == STATE_INSTALLING:
                return BootstrapResult(
                    dependency=dependency,
                    state=STATE_INSTALLING,
                    changed=changed,
                    message=f"{dependency} dependency bootstrap is already running",
                    pid=dep_state.get("pid"),
                )
            if state == STATE_FAILED:
                error = dep_state.get("error") or _manual_install_message(
                    dependency,
                    f"{dependency} dependency bootstrap failed.",
                )
                return BootstrapResult(
                    dependency=dependency,
                    state=STATE_FAILED,
                    changed=changed,
                    message=error,
                    error=error,
                )
            return BootstrapResult(
                dependency=dependency,
                state=STATE_MISSING,
                changed=changed,
                message=f"{dependency} dependency bootstrap is required",
            )
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ensure_dependency(
    dependency: str,
    wait: bool = True,
    background: bool = False,
    timeout_seconds: int = DEFAULT_WAIT_TIMEOUT,
) -> BootstrapResult:
    if dependency_ready(dependency):
        with _locked_status() as status:
            dep_state = status["dependencies"][dependency]
            dep_state.update(
                {
                    "state": STATE_READY,
                    "updated_at": _timestamp(),
                    "finished_at": dep_state.get("finished_at") or _timestamp(),
                    "pid": None,
                    "error": None,
                }
            )
        return BootstrapResult(
            dependency=dependency,
            state=STATE_READY,
            message=f"{dependency} dependencies are ready",
        )

    wait_for_existing = False
    with _locked_status() as status:
        dep_state = status["dependencies"][dependency]
        if dep_state["state"] == STATE_READY and dependency_ready(dependency):
            return BootstrapResult(
                dependency=dependency,
                state=STATE_READY,
                message=f"{dependency} dependencies are ready",
            )

        owner_pid = dep_state.get("pid")
        if dep_state["state"] == STATE_INSTALLING and owner_pid == os.getpid():
            pass
        elif dep_state["state"] == STATE_INSTALLING:
            if background or not wait:
                return BootstrapResult(
                    dependency=dependency,
                    state=STATE_INSTALLING,
                    message=f"{dependency} dependency bootstrap is already running",
                    pid=owner_pid,
                )
            wait_for_existing = True
        elif background:
            child_pid = _spawn_background_install(dependency)
            dep_state.update(
                {
                    "state": STATE_INSTALLING,
                    "updated_at": _timestamp(),
                    "started_at": dep_state.get("started_at") or _timestamp(),
                    "finished_at": None,
                    "pid": child_pid,
                    "error": None,
                }
            )
            log_activity(
                f"[bootstrap] Started background install for {dependency} dependencies (pid {child_pid})"
            )
            return BootstrapResult(
                dependency=dependency,
                state="started",
                changed=True,
                message=f"Started background install for {dependency} dependencies",
                pid=child_pid,
            )
        else:
            dep_state.update(
                {
                    "state": STATE_INSTALLING,
                    "updated_at": _timestamp(),
                    "started_at": dep_state.get("started_at") or _timestamp(),
                    "finished_at": None,
                    "pid": os.getpid(),
                    "error": None,
                }
            )

    if wait_for_existing:
        return _wait_for_dependency(dependency, timeout_seconds)

    packages = missing_packages(dependency)
    if not packages and dependency_ready(dependency):
        return BootstrapResult(
            dependency=dependency,
            state=STATE_READY,
            message=f"{dependency} dependencies are ready",
        )
    if not packages:
        error = _manual_install_message(
            dependency,
            "Automatic dependency detection found no installable packages for this missing runtime.",
        )
        _mark_failed(dependency, error)
        raise BootstrapError(error)

    backend: str | None = None
    try:
        backend = _install_packages(dependency, packages)
        if not dependency_ready(dependency):
            raise BootstrapError(
                _manual_install_message(
                    dependency,
                    f"{dependency} packages installed via {backend}, but the runtime is still unavailable.",
                )
            )
    except BootstrapError as exc:
        _mark_failed(dependency, str(exc))
        log_activity(f"[bootstrap] Failed to install {dependency} dependencies: {exc}")
        raise

    with _locked_status() as status:
        dep_state = status["dependencies"][dependency]
        dep_state.update(
            {
                "state": STATE_READY,
                "updated_at": _timestamp(),
                "finished_at": _timestamp(),
                "pid": None,
                "error": None,
            }
        )
    log_activity(
        f"[bootstrap] {dependency} dependencies are ready"
        + (f" via {backend}" if backend else "")
    )
    return BootstrapResult(
        dependency=dependency,
        state=STATE_READY,
        changed=True,
        message=f"{dependency} dependencies are ready",
        backend=backend,
    )


def _wait_for_dependency(dependency: str, timeout_seconds: int) -> BootstrapResult:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = read_status()
        dep_state = status["dependencies"][dependency]
        if dep_state["state"] == STATE_READY or dependency_ready(dependency):
            return BootstrapResult(
                dependency=dependency,
                state=STATE_READY,
                message=f"{dependency} dependencies are ready",
            )
        if dep_state["state"] == STATE_FAILED:
            error = dep_state.get("error") or _manual_install_message(
                dependency,
                f"{dependency} dependency bootstrap failed.",
            )
            raise BootstrapError(error)
        time.sleep(1)

    error = _manual_install_message(
        dependency,
        f"Timed out waiting for {dependency} dependency bootstrap to finish.",
    )
    _mark_failed(dependency, error)
    raise BootstrapError(error)


def _install_packages(dependency: str, packages: list[str]) -> str:
    backend_name, install_cmd = _build_install_command(dependency, packages)
    log_activity(
        f"[bootstrap] Installing {dependency} dependencies via {backend_name}: {', '.join(packages)}"
    )
    with open(log_file(), "a", encoding="utf-8") as handle:
        process = subprocess.run(
            install_cmd,
            stdout=handle,
            stderr=handle,
            check=False,
        )
    if process.returncode != 0:
        raise BootstrapError(
            _manual_install_message(
                dependency,
                f"{backend_name} exited with code {process.returncode} while installing {', '.join(packages)}.",
            )
        )
    _invalidate_import_caches()
    return backend_name


def _build_install_command(dependency: str, packages: list[str]) -> tuple[str, list[str]]:
    target_dir = str(managed_site_dir())

    if _python_has_pip():
        command = [sys.executable, "-m", "pip", "install", "--quiet", "--target", target_dir]
        return "pip", command + packages

    uv_path = shutil.which("uv")
    if uv_path:
        command = [
            uv_path,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--quiet",
            "--no-progress",
            "--target",
            target_dir,
        ]
        return "uv", command + packages

    pip_path = shutil.which("pip")
    if pip_path:
        command = [pip_path, "install", "--quiet", "--target", target_dir]
        return "pip", command + packages

    raise BootstrapError(
        _manual_install_message(
            dependency,
            "Neither pip nor uv is available for automatic dependency bootstrap.",
        )
    )


def _spawn_background_install(dependency: str) -> int:
    with open(log_file(), "a", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "ensure",
                dependency,
                "--foreground",
                "--quiet",
            ],
            stdout=handle,
            stderr=handle,
            start_new_session=True,
            close_fds=True,
        )
    return process.pid


def _mark_failed(dependency: str, error: str) -> None:
    with _locked_status() as status:
        dep_state = status["dependencies"][dependency]
        dep_state.update(
            {
                "state": STATE_FAILED,
                "updated_at": _timestamp(),
                "finished_at": _timestamp(),
                "pid": None,
                "error": error,
            }
        )


@contextmanager
def _locked_status() -> Any:
    with open(lock_file(), "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        status = _load_status_unlocked()
        if _normalize_status_unlocked(status):
            _write_status_unlocked(status)
        try:
            yield status
        finally:
            _write_status_unlocked(status)
            fcntl.flock(handle, fcntl.LOCK_UN)


def _load_status_unlocked() -> dict[str, Any]:
    status, source = _load_persisted_status_unlocked()
    if source != "current":
        return {"dependencies": _default_dependencies_status()}

    status.setdefault("dependencies", {})
    for dependency, dep_state in _default_dependencies_status().items():
        status["dependencies"].setdefault(dependency, dep_state)
    return status


def _load_persisted_status_unlocked() -> tuple[dict[str, Any], str]:
    path = status_file()
    if not path.exists():
        return {}, "missing"

    try:
        with open(path, "r", encoding="utf-8") as handle:
            status = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}, "corrupt"

    if not isinstance(status, dict):
        return {}, "corrupt"

    return status, "current"


def _write_status_unlocked(status: dict[str, Any]) -> None:
    path = status_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(status, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def _default_dependencies_status() -> dict[str, dict[str, Any]]:
    timestamp = _timestamp()
    return {
        CORE: {
            "state": STATE_READY if dependency_ready(CORE) else STATE_MISSING,
            "updated_at": timestamp,
            "started_at": None,
            "finished_at": None,
            "pid": None,
            "error": None,
        },
        EMBEDDING: {
            "state": STATE_READY if dependency_ready(EMBEDDING) else STATE_MISSING,
            "updated_at": timestamp,
            "started_at": None,
            "finished_at": None,
            "pid": None,
            "error": None,
        },
    }


def _normalize_status_unlocked(status: dict[str, Any]) -> bool:
    changed = False
    for dependency in (CORE, EMBEDDING):
        dep_state = status["dependencies"][dependency]
        runtime_ready = dependency_ready(dependency)
        current_state = dep_state.get("state", STATE_MISSING)

        if runtime_ready and current_state != STATE_READY:
            dep_state.update(
                {
                    "state": STATE_READY,
                    "updated_at": _timestamp(),
                    "finished_at": dep_state.get("finished_at") or _timestamp(),
                    "pid": None,
                    "error": None,
                }
            )
            changed = True
            continue

        if runtime_ready or current_state != STATE_INSTALLING:
            if not runtime_ready and current_state == STATE_READY:
                dep_state.update(
                    {
                        "state": STATE_MISSING,
                        "updated_at": _timestamp(),
                        "finished_at": None,
                        "pid": None,
                        "error": None,
                    }
                )
                changed = True
            continue

        if dep_state.get("pid") and _pid_is_running(dep_state["pid"]):
            continue

        dep_state.update(
            {
                "state": STATE_FAILED,
                "updated_at": _timestamp(),
                "finished_at": _timestamp(),
                "pid": None,
                "error": dep_state.get("error")
                or _manual_install_message(
                    dependency,
                    f"{dependency} dependency bootstrap exited before the runtime became available.",
                ),
            }
        )
        changed = True
    return changed


def _normalize_probe_state_unlocked(dependency: str, dep_state: dict[str, Any]) -> bool:
    changed = False
    runtime_ready = dependency_ready(dependency)
    current_state = dep_state.get("state", STATE_MISSING)

    if current_state == STATE_READY and not runtime_ready:
        dep_state.update(
            {
                "state": STATE_MISSING,
                "updated_at": _timestamp(),
                "finished_at": None,
                "pid": None,
                "error": None,
            }
        )
        return True

    if current_state != STATE_INSTALLING:
        return changed

    if runtime_ready:
        dep_state.update(
            {
                "state": STATE_READY,
                "updated_at": _timestamp(),
                "finished_at": dep_state.get("finished_at") or _timestamp(),
                "pid": None,
                "error": None,
            }
        )
        return True

    if dep_state.get("pid") and _pid_is_running(dep_state["pid"]):
        return changed

    dep_state.update(
        {
            "state": STATE_FAILED,
            "updated_at": _timestamp(),
            "finished_at": _timestamp(),
            "pid": None,
            "error": dep_state.get("error")
            or _manual_install_message(
                dependency,
                f"{dependency} dependency bootstrap exited before the runtime became available.",
            ),
        }
    )
    return True


@lru_cache(maxsize=1)
def _python_has_pip() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _module_available(module_name: str) -> bool:
    ensure_managed_site_dir_on_path()
    return importlib.util.find_spec(module_name) is not None


def _invalidate_import_caches() -> None:
    importlib.invalidate_caches()
    for site_dir in (managed_site_dir_path(), legacy_managed_site_dir()):
        sys.path_importer_cache.pop(str(site_dir), None)


def _core_runtime_ready() -> bool:
    return _module_available("sqlite_vec") and _sqlite_runtime_ready()


def _sqlite_runtime_ready() -> bool:
    if _sqlite_module_supports_extensions(stdlib_sqlite3):
        return True
    if not _module_available("pysqlite3"):
        return False
    try:
        import pysqlite3 as pysqlite3_sqlite
    except ImportError:
        return False
    return _sqlite_module_supports_extensions(pysqlite3_sqlite)


def _sqlite_module_supports_extensions(sqlite_module: Any) -> bool:
    try:
        conn = sqlite_module.connect(":memory:")
    except Exception:
        return False
    try:
        return hasattr(conn, "enable_load_extension")
    finally:
        conn.close()


def _pysqlite_package_name() -> str:
    machine = platform.machine().lower()
    if machine.startswith("arm") or machine == "aarch64":
        return "pysqlite3"
    return "pysqlite3-binary"


def _pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _manual_install_message(dependency: str, detail: str) -> str:
    packages = missing_packages(dependency)
    package_list = packages or (
        ["sentence-transformers"] if dependency == EMBEDDING else [_pysqlite_package_name(), "sqlite-vec"]
    )
    target_dir = managed_site_dir()
    return (
        f"{detail} Install manually with: "
        f"pip install --target {target_dir} {' '.join(package_list)}"
    )


def _result_to_text(result: BootstrapResult) -> str:
    if result.message:
        return result.message
    return f"{result.dependency} dependencies: {result.state}"


def _status_exit_code(result: BootstrapResult) -> int:
    return 0 if result.state in {STATE_READY, STATE_INSTALLING, "started"} else 1


def _probe_exit_code(result: BootstrapResult) -> int:
    return 0 if result.state == STATE_READY else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compound-learning dependency bootstrap")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure_parser = subparsers.add_parser("ensure", help="Ensure a dependency group is ready")
    ensure_parser.add_argument("dependency", choices=[CORE, EMBEDDING])
    ensure_parser.add_argument("--background", action="store_true")
    ensure_parser.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    ensure_parser.add_argument("--json", action="store_true")
    ensure_parser.add_argument("--quiet", action="store_true")

    probe_parser = subparsers.add_parser(
        "probe",
        help="Check whether a dependency group can skip bootstrap on warm startup",
    )
    probe_parser.add_argument("dependency", choices=[CORE, EMBEDDING])
    probe_parser.add_argument("--json", action="store_true")
    probe_parser.add_argument("--quiet", action="store_true")

    status_parser = subparsers.add_parser("status", help="Print bootstrap status")
    status_parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "status":
        status = read_status()
        if args.json:
            print(json.dumps(status))
        else:
            print(json.dumps(status, indent=2))
        return 0

    if args.command == "probe":
        result = probe_dependency(args.dependency)
        if args.json:
            print(json.dumps(result.to_dict()))
        elif not args.quiet:
            print(_result_to_text(result))
        return _probe_exit_code(result)

    wait = not args.background or args.foreground
    background = args.background and not args.foreground
    try:
        result = ensure_dependency(args.dependency, wait=wait, background=background)
    except BootstrapError as exc:
        result = BootstrapResult(
            dependency=args.dependency,
            state=STATE_FAILED,
            error=str(exc),
            message=str(exc),
        )

    if args.json:
        print(json.dumps(result.to_dict()))
    elif not args.quiet:
        print(_result_to_text(result))

    return _status_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
