"""Safe, versioned installation and operational checks for Bonus Drain.

The lifecycle layer deliberately does not start services or mutate queue state.  It
installs only files it can later prove it owns, leaves XDG state/config/cache alone,
and refuses uninstall when an operator has added or changed an owned installation
file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class LifecycleError(RuntimeError):
    """Base class for lifecycle failures."""


class OwnershipError(LifecycleError):
    """Raised when an operation would overwrite or remove an unowned path."""


UNIT_NAMES = (
    "bonus-drain-refresh.service",
    "bonus-drain-refresh.timer",
    "bonus-drain-scout.service",
    "bonus-drain-scout.timer",
    "bonus-drain-viewer.service",
)
_OWNED_NAME = ".bonus-drain-owned.json"
_INSTALL_NAME = ".bonus-drain-install.json"
_WRAPPER_MARKER = "# managed-by: bonus-drain lifecycle v1"
_DEFAULT_VERSION = "0.3.0"
@dataclass(frozen=True)
class InstalledPaths:
    version: str
    version_dir: Path
    current: Path
    wrapper: Path
    units: tuple[Path, ...]

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {key: str(value) if isinstance(value, Path) else value for key, value in data.items()}


@dataclass(frozen=True)
class InstallStatus:
    installed: bool
    version: str | None
    wrapper: Path
    current: Path
    units: tuple[Path, ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.installed and not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "ok": self.ok,
            "version": self.version,
            "wrapper": str(self.wrapper),
            "current": str(self.current),
            "units": [str(path) for path in self.units],
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    checks: Mapping[str, bool]
    diagnostics: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": dict(self.checks), "diagnostics": list(self.diagnostics)}


@dataclass(frozen=True)
class ConsumerCompatibility:
    ok: bool
    diagnostics: tuple[str, ...]
    database_paths: set[Path]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "diagnostics": list(self.diagnostics),
            "database_paths": sorted(str(path) for path in self.database_paths),
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _paths(home: Path) -> tuple[Path, Path, Path, Path]:
    home = home.expanduser().resolve()
    lib = home / ".local" / "lib" / "bonus-drain"
    wrapper = home / ".local" / "bin" / "bonus-drain"
    unit_dir = home / ".config" / "systemd" / "user"
    return home, lib, wrapper, unit_dir


def _version(source: Path, requested: str | None) -> str:
    if requested is not None:
        candidate = requested
    else:
        candidate = _DEFAULT_VERSION
        init = source / "bonus_drain" / "__init__.py"
        if init.is_file():
            match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", init.read_text(), re.M)
            if match:
                candidate = match.group(1)
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?", candidate):
        raise LifecycleError(f"invalid install version: {candidate!r}")
    return candidate


def _runtime_files(source: Path) -> list[Path]:
    manifest_path = source / "distribution-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = manifest["include"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"invalid distribution manifest: {manifest_path}") from exc
    if not isinstance(entries, list) or not entries or not all(isinstance(item, str) for item in entries):
        raise LifecycleError(f"invalid distribution manifest include list: {manifest_path}")
    files: list[Path] = []
    for item in entries:
        rel = Path(item)
        if rel.is_absolute() or ".." in rel.parts or str(rel) != item:
            raise LifecycleError(f"unsafe distribution manifest path: {item!r}")
        child = source / rel
        if child.is_symlink() or not child.is_file():
            raise OwnershipError(f"manifest runtime file is missing or unsafe: {child}")
        files.append(rel)
    if len(set(files)) != len(files):
        raise LifecycleError(f"distribution manifest contains duplicate paths: {manifest_path}")
    if Path("bonus_drain/__main__.py") not in files:
        raise LifecycleError("source is missing bonus_drain/__main__.py")
    return sorted(files)


def _owned_payload(root: Path) -> dict[str, str]:
    marker = root / _OWNED_NAME
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OwnershipError(f"missing or invalid ownership manifest: {marker}") from exc
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, dict) or not all(
        isinstance(name, str) and isinstance(digest, str) for name, digest in files.items()
    ):
        raise OwnershipError(f"invalid ownership manifest: {marker}")
    return dict(files)


def _verify_version_dir(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise OwnershipError(f"unsafe version directory: {root}")
    owned = _owned_payload(root)
    actual = {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    expected = set(owned) | {_OWNED_NAME}
    unknown = sorted(actual - expected)
    missing = sorted(set(owned) - actual)
    changed = sorted(
        rel for rel, digest in owned.items()
        if rel in actual and ((root / rel).is_symlink() or _sha256(root / rel) != digest)
    )
    if unknown or missing or changed:
        details = []
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        if missing:
            details.append("missing=" + ",".join(missing))
        if changed:
            details.append("changed=" + ",".join(changed))
        raise OwnershipError(f"installation ownership check failed for {root}: {'; '.join(details)}")
    return owned


def _wrapper_text(lib: Path) -> str:
    # The absolute library root is intentional: an installation made into a disposable
    # HOME remains self-contained even if a caller later supplies a different HOME.
    return f"""#!/bin/sh
{_WRAPPER_MARKER}
set -eu
BONUS_DRAIN_CURRENT={json.dumps(str(lib / 'current'))}
export PYTHONDONTWRITEBYTECODE=1
exec python3 -I -B -c '
import runpy
import sys
runtime = sys.argv.pop(1)
sys.path.insert(0, runtime)
runpy.run_module("bonus_drain", run_name="__main__")
' "$BONUS_DRAIN_CURRENT" "$@"
"""


def _write_atomic(path: Path, value: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _refuse_foreign(
    path: Path,
    expected: bytes | None = None,
    *,
    previously_owned_sha256: str | None = None,
) -> None:
    if path.is_symlink():
        raise OwnershipError(f"refusing to replace symlink: {path}")
    if not path.exists():
        return
    if not path.is_file() or expected is None:
        raise OwnershipError(f"refusing to replace unowned path: {path}")
    if path.read_bytes() == expected:
        return
    if previously_owned_sha256 is not None and _sha256(path) == previously_owned_sha256:
        return
    raise OwnershipError(f"refusing to replace unowned path: {path}")


def install(source: str | Path, home: str | Path | None = None, *, version: str | None = None) -> InstalledPaths:
    """Install a version without enabling or starting any services."""

    source_path = Path(source).expanduser().resolve()
    if not source_path.is_dir() or source_path.is_symlink():
        raise LifecycleError(f"invalid source directory: {source_path}")
    home_path, lib, wrapper, unit_dir = _paths(Path(home) if home is not None else Path.home())
    release = _version(source_path, version)
    version_dir = lib / release
    current = lib / "current"
    source_files = _runtime_files(source_path)
    wrapper_bytes = _wrapper_text(lib).encode("utf-8")

    unit_payloads: dict[Path, bytes] = {}
    for name in UNIT_NAMES:
        source_unit = source_path / "systemd" / name
        if not source_unit.is_file() or source_unit.is_symlink():
            raise LifecycleError(f"source is missing safe systemd template: {source_unit}")
        unit_payloads[unit_dir / name] = source_unit.read_bytes()

    previous_install: Mapping[str, Any] = {}
    if (lib / _INSTALL_NAME).exists():
        previous_install = _install_record(lib)
    previous_wrapper = previous_install.get("wrapper", {})
    previous_units = previous_install.get("units", {})
    wrapper_hash = (
        previous_wrapper.get("sha256") if isinstance(previous_wrapper, Mapping) else None
    )
    if not isinstance(previous_units, Mapping):
        previous_units = {}

    _refuse_foreign(wrapper, wrapper_bytes, previously_owned_sha256=wrapper_hash)
    for path, payload in unit_payloads.items():
        owned_hash = previous_units.get(str(path))
        _refuse_foreign(
            path,
            payload,
            previously_owned_sha256=owned_hash if isinstance(owned_hash, str) else None,
        )
    if current.exists() and not current.is_symlink():
        raise OwnershipError(f"refusing non-symlink current path: {current}")
    if current.is_symlink():
        target = (current.parent / os.readlink(current)).resolve()
        if target.parent != lib.resolve():
            raise OwnershipError(f"current points outside the managed library: {current}")

    lib.mkdir(parents=True, exist_ok=True)
    if version_dir.exists() or version_dir.is_symlink():
        _verify_version_dir(version_dir)
    else:
        staging = Path(tempfile.mkdtemp(prefix=f".{release}.", dir=lib))
        try:
            owned: dict[str, str] = {}
            for rel in source_files:
                src = source_path / rel
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest, follow_symlinks=False)
                owned[str(rel)] = _sha256(dest)
            marker = json.dumps(
                {"format": 1, "version": release, "files": owned},
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            _write_atomic(staging / _OWNED_NAME, marker, 0o600)
            os.replace(staging, version_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    temporary_link = lib / f".current.{os.getpid()}"
    try:
        temporary_link.unlink(missing_ok=True)
        temporary_link.symlink_to(release)
        os.replace(temporary_link, current)
    finally:
        temporary_link.unlink(missing_ok=True)

    _write_atomic(wrapper, wrapper_bytes, 0o755)
    for path, payload in unit_payloads.items():
        _write_atomic(path, payload, 0o644)

    install_record = {
        "format": 1,
        "version": release,
        "wrapper": {"path": str(wrapper), "sha256": _sha256(wrapper)},
        "units": {str(path): _sha256(path) for path in unit_payloads},
    }
    _write_atomic(
        lib / _INSTALL_NAME,
        json.dumps(install_record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
        0o600,
    )
    return InstalledPaths(release, version_dir, current, wrapper, tuple(unit_payloads))


def _install_record(lib: Path) -> dict[str, Any]:
    path = lib / _INSTALL_NAME
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OwnershipError(f"missing or invalid install record: {path}") from exc
    if not isinstance(raw, dict) or raw.get("format") != 1:
        raise OwnershipError(f"invalid install record: {path}")
    return raw


def _executable_available(command: str) -> bool:
    """Check one executable path without starting it or resolving credentials."""

    path = Path(command).expanduser()
    if path.is_absolute() or path.parent != Path("."):
        return path.is_file() and os.access(path, os.X_OK)
    resolved = shutil.which(command)
    return resolved is not None and Path(resolved).is_file()


def _configured_rotator(cfg: Any) -> str | None:
    """Return an explicitly named ai-token-rotator argv entry, if configured."""

    for adapter in cfg.adapters:
        if adapter.kind != "activation":
            continue
        for token in adapter.argv:
            if Path(token).name in {"ai-token-rotator", "ai-token-rotator.sh"}:
                return token
    return None


def status(home: str | Path | None = None) -> InstallStatus:
    _home, lib, wrapper, unit_dir = _paths(Path(home) if home is not None else Path.home())
    current = lib / "current"
    problems: list[str] = []
    release: str | None = None
    if not current.is_symlink():
        problems.append("current release symlink is missing")
    else:
        release = Path(os.readlink(current)).name
        try:
            _verify_version_dir((current.parent / os.readlink(current)).resolve())
        except OwnershipError as exc:
            problems.append(str(exc))
    if not wrapper.is_file() or wrapper.is_symlink() or _WRAPPER_MARKER not in wrapper.read_text(errors="replace"):
        problems.append("stable wrapper is missing or not owned")
    elif not os.access(wrapper, os.X_OK):
        problems.append("stable wrapper is not executable")
    units = tuple(unit_dir / name for name in UNIT_NAMES)
    missing_units = [path.name for path in units if not path.is_file() or path.is_symlink()]
    if missing_units:
        problems.append("systemd templates missing: " + ", ".join(missing_units))
    installed = current.is_symlink() or wrapper.exists() or (lib / _INSTALL_NAME).exists()
    return InstallStatus(installed, release, wrapper, current, units, tuple(problems))


def doctor(
    home: str | Path | None = None,
    *,
    config_path: str | Path | None = None,
    consumers: Sequence[str | Path] = (),
) -> DoctorReport:
    """Check installation, dependencies and configuration without executing adapters."""

    current_status = status(home)
    python_ok = sys.version_info >= (3, 10)
    try:
        import sqlite3

        sqlite_ok = bool(sqlite3.sqlite_version)
    except (ImportError, AttributeError):
        sqlite_ok = False
    checks: dict[str, bool] = {
        "installation": current_status.ok,
        "dependency:python>=3.10": python_ok,
        "dependency:sqlite3": sqlite_ok,
    }
    required_checks = {"installation", "dependency:python>=3.10", "dependency:sqlite3"}
    diagnostics = list(current_status.problems)
    if not python_ok:
        diagnostics.append(
            f"Python 3.10 or newer is required; detected {sys.version_info.major}.{sys.version_info.minor}"
        )
    if not sqlite_ok:
        diagnostics.append("Python sqlite3 support is unavailable")

    # These are optional deployment integrations, so absence is reported but does
    # not make an otherwise healthy local installation fail doctor.
    checks["optional:tailscale"] = shutil.which("tailscale") is not None
    cfg = None
    if config_path is not None:
        try:
            from . import config

            cfg = config.load_config(Path(config_path))
            checks["config"] = True
            required_checks.add("config")
        except Exception as exc:  # config publishes its own safe diagnostics
            checks["config"] = False
            required_checks.add("config")
            diagnostics.append(f"config: {exc}")
    if cfg is not None:
        database = Path(cfg.database).expanduser()
        checks["database_parent"] = database.parent.is_dir() and not database.parent.is_symlink()
        required_checks.add("database_parent")
        if not checks["database_parent"]:
            diagnostics.append(f"database parent is missing or unsafe: {database.parent}")

        from . import config as config_module

        for ref in cfg.secret_refs:
            if ref.source != "file":
                continue
            check_name = f"secret:file:{ref.id}"
            try:
                config_module.resolve_secret(ref)
                checks[check_name] = True
            except config_module.ConfigError as exc:
                checks[check_name] = False
                diagnostics.append(f"file secret reference {ref.id}: {exc}")
            required_checks.add(check_name)

        for adapter in cfg.adapters:
            if adapter.kind not in {"agent-router", "usage", "reset", "activation"}:
                continue
            check_name = f"adapter:{adapter.kind}:{adapter.id}"
            available = bool(adapter.argv) and _executable_available(adapter.argv[0])
            checks[check_name] = available
            required_checks.add(check_name)
            if not available:
                executable = adapter.argv[0] if adapter.argv else "<missing>"
                diagnostics.append(
                    f"adapter {adapter.id} ({adapter.kind}) executable is unavailable: {executable}"
                )

        activation_configured = any(adapter.kind == "activation" for adapter in cfg.adapters)
        rotator = _configured_rotator(cfg)
        if activation_configured:
            checks["optional:ai-token-rotator"] = (
                (rotator is not None and _executable_available(rotator))
                or shutil.which("ai-token-rotator") is not None
            )
        if consumers:
            compatibility = check_consumer_compatibility(cfg, consumers)
            checks["consumers"] = compatibility.ok
            required_checks.add("consumers")
            diagnostics.extend(compatibility.diagnostics)
    return DoctorReport(
        all(checks.get(name, False) for name in required_checks),
        checks,
        tuple(diagnostics),
    )


def uninstall(home: str | Path | None = None) -> tuple[Path, ...]:
    """Remove only a proven-owned install; XDG state/config/cache are preserved."""

    _home, lib, wrapper, unit_dir = _paths(Path(home) if home is not None else Path.home())
    if not lib.exists() and not wrapper.exists():
        return ()
    record = _install_record(lib)
    version_dirs = [path for path in lib.iterdir() if path.name not in {"current", _INSTALL_NAME}]
    for version_dir in version_dirs:
        _verify_version_dir(version_dir)
    if lib.joinpath("current").exists() and not lib.joinpath("current").is_symlink():
        raise OwnershipError(f"unsafe current path: {lib / 'current'}")

    expected_wrapper = record.get("wrapper", {})
    if wrapper.exists() or wrapper.is_symlink():
        if wrapper.is_symlink() or not wrapper.is_file() or _sha256(wrapper) != expected_wrapper.get("sha256"):
            raise OwnershipError(f"stable wrapper changed or is unowned: {wrapper}")
    units = record.get("units")
    if not isinstance(units, dict):
        raise OwnershipError("invalid unit ownership record")
    expected_unit_paths = {unit_dir / name for name in UNIT_NAMES}
    if not all(isinstance(raw_path, str) and isinstance(digest, str) for raw_path, digest in units.items()):
        raise OwnershipError("invalid unit ownership record")
    recorded_unit_paths = {Path(raw_path) for raw_path in units}
    if recorded_unit_paths != expected_unit_paths:
        raise OwnershipError("unit ownership record is not confined to managed systemd templates")
    confined_units = {Path(raw_path): digest for raw_path, digest in units.items()}
    for path, digest in confined_units.items():
        if path.parent != unit_dir or path.name not in UNIT_NAMES:
            raise OwnershipError(f"unsafe unit ownership path: {path}")
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
                raise OwnershipError(f"systemd template changed or is unowned: {path}")

    removed: list[Path] = []
    for path in confined_units:
        if path.exists():
            path.unlink()
            removed.append(path)
    if wrapper.exists():
        wrapper.unlink()
        removed.append(wrapper)
    current = lib / "current"
    if current.is_symlink():
        current.unlink()
        removed.append(current)
    for version_dir in version_dirs:
        shutil.rmtree(version_dir)
        removed.append(version_dir)
    (lib / _INSTALL_NAME).unlink()
    removed.append(lib / _INSTALL_NAME)
    try:
        lib.rmdir()
    except OSError:
        pass
    return tuple(removed)


def compatibility_environment(cfg: Any, *, home: str | Path | None = None) -> dict[str, str]:
    home_path = (Path(home) if home is not None else Path.home()).expanduser()
    binary = Path(os.environ.get("BONUS_DRAIN_BIN", str(home_path / ".local/bin/bonus-drain")))
    database = Path(cfg.database).expanduser()
    return {
        "BONUS_DRAIN_CONFIG": str(
            getattr(cfg, "source_path", None) or os.environ.get("BONUS_DRAIN_CONFIG", "")
        ),
        "BONUS_DRAIN_BIN": str(binary),
        "BONUSDB": str(binary),
        "BONUS_DB": str(database),
    }


def check_consumer_compatibility(cfg: Any, assets: Iterable[str | Path]) -> ConsumerCompatibility:
    """Reject known split-brain/private-runtime references in enumerated consumers."""

    diagnostics: list[str] = []
    database = Path(cfg.database).expanduser()
    legacy_patterns = (
        re.compile(r"(?:~|\$HOME)/\.claude/bonus-drain\.db"),
        re.compile(r"Path\.home\(\)\s*/\s*['\"]\.claude['\"]\s*/\s*['\"]bonus-drain\.db['\"]"),
        re.compile(r"(?:~|\$HOME)/\.claude/skills/bonus-drain"),
    )
    for raw_asset in assets:
        asset = Path(raw_asset)
        if not asset.is_file() or asset.is_symlink():
            diagnostics.append(f"consumer missing or unsafe: {asset}")
            continue
        try:
            content = asset.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            diagnostics.append(f"consumer unreadable: {asset}: {exc}")
            continue
        for pattern in legacy_patterns:
            if pattern.search(content):
                diagnostics.append(f"consumer uses legacy private path: {asset}")
                break
        if not any(token in content for token in ("BONUS_DRAIN_BIN", "bonus-drain", "BONUSDB")):
            diagnostics.append(f"consumer does not reference the stable Bonus Drain interface: {asset}")
    return ConsumerCompatibility(not diagnostics, tuple(diagnostics), {database})
