"""Strict, stdlib-only configuration for Bonus Drain.

Providers and pacing limits use configured ids. Account inventory and usage are supplied
by the proxy cache, with no configured account graph.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Raised when configuration cannot be parsed or is unsafe."""


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ALLOWED_PLACEHOLDERS = {
    "account_id",
    "action",
    "cache_path",
    "provider_id",
    "secret_file",
}
_SHELL_META_RE = re.compile(r"(?:\$\(|`|\x00|[\r\n]|[;&|<>])")
_SHELL_PROGRAMS = {"ash", "bash", "csh", "dash", "fish", "ksh", "sh", "tcsh", "zsh"}
_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|access_?token|refresh_?token|id_?token|password|passwd|credential|secret)(?:$|_)",
    re.IGNORECASE,
)
_REFERENCE_KEYS = {
    "secret_ref",
    "proxy_secret_ref",
    "secret_refs",
}


def _xdg_home(environ: Mapping[str, str], key: str, fallback: Path) -> Path:
    value = environ.get(key)
    return Path(value).expanduser() if value else fallback


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get("BONUS_DRAIN_CONFIG"):
        return Path(env["BONUS_DRAIN_CONFIG"]).expanduser()
    home = Path(env.get("HOME", str(Path.home()))).expanduser()
    return _xdg_home(env, "XDG_CONFIG_HOME", home / ".config") / "bonus-drain" / "config.json"


def default_state_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME", str(Path.home()))).expanduser()
    return _xdg_home(env, "XDG_STATE_HOME", home / ".local" / "state") / "bonus-drain"


def default_cache_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME", str(Path.home()))).expanduser()
    return _xdg_home(env, "XDG_CACHE_HOME", home / ".cache") / "bonus-drain"


def _loopback(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if value.lower() == "localhost":
        return True
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _exact_host(value: Any) -> bool:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return False
    if any(marker in value for marker in ("/", "*", "@", "?", "#")):
        return False
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and (port is None or 1 <= port <= 65535)
    )


@dataclass(frozen=True)
class SecretRef:
    id: str
    source: str
    name: str | None = None
    path: Path | None = None


@dataclass(frozen=True)
class AdapterConfig:
    id: str
    kind: str
    argv: tuple[str, ...]
    timeout_seconds: float = 30.0
    max_output_bytes: int = 1_048_576
    env_allowlist: tuple[str, ...] = ()
    secret_refs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchBinding:
    adapter_id: str
    provider: str


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    dispatch: DispatchBinding
    max_size: str
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.max_size not in ("tiny", "small", "medium", "large", "huge"):
            raise ConfigError("provider max_size must be one of: tiny, small, medium, large, huge")


@dataclass(frozen=True)
class LimitConfig:
    id: str
    provider_id: str
    window_seconds: int
    ceiling_percent: float
    lead_seconds: int
    batch_size: int
    max_percent_per_window: float = 0.0
    estimated_percent_per_job: float | None = None
    pacing_window_seconds: int = 3_600
    urgency_seconds: int = 0


@dataclass(frozen=True)
class HostLoadGateConfig:
    """Host thresholds past which the scout launches nothing this tick."""

    enabled: bool = True
    cpu_some_avg60_max: float = 40.0
    mem_available_min_gib: float = 4.0
    load_per_cpu_max: float = 1.5
    disk_free_min_gib: float = 30.0


@dataclass(frozen=True)
class RuntimeConfig:
    schema_version: int
    source_path: Path | None
    database: Path
    record_command: tuple[str, ...]
    secret_refs: tuple[SecretRef, ...]
    adapters: tuple[AdapterConfig, ...]
    providers: tuple[ProviderConfig, ...]
    proxy_secret_ref: str | None
    limits: tuple[LimitConfig, ...]
    viewer: Mapping[str, Any]
    pr_exceptions: tuple[Mapping[str, Any], ...]
    recurrence_timezone: str = "America/New_York"
    usage_max_age_seconds: int = 3_600
    cache_dir: Path = Path(".")
    max_jobs: int | None = None
    unmetered_max_inflight: int = 2
    host_load_gate: HostLoadGateConfig = field(default_factory=HostLoadGateConfig)
    scout_ntfy_url: str | None = None

    @property
    def state_dir(self) -> Path:
        return self.database.parent

    @property
    def usage_cache_dir(self) -> Path:
        return self.cache_dir / "usage"

    def adapter(self, adapter_id: str) -> AdapterConfig:
        return _lookup(self.adapters, adapter_id, "adapter")

    def provider(self, provider_id: str) -> ProviderConfig:
        return _lookup(self.providers, provider_id, "provider")

    def limits_for_provider(self, provider_id: str) -> tuple[LimitConfig, ...]:
        return tuple(limit for limit in self.limits if limit.provider_id == provider_id)


def _lookup(items: Iterable[Any], item_id: str, label: str) -> Any:
    for item in items:
        if item.id == item_id:
            return item
    raise ConfigError(f"unknown {label} id: {item_id}")


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be an array")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"{label} has unknown key: {unknown[0]}")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ConfigError(f"{label} must be a safe id (letters, digits, dot, underscore, dash)")
    if value in {".", ".."}:
        raise ConfigError(f"{label} contains path traversal")
    return value


def _string(value: Any, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ConfigError(f"{label} must be a{' non-empty' if nonempty else ''} string")
    if "\x00" in value:
        raise ConfigError(f"{label} contains a NUL byte")
    return value


def _number(value: Any, label: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a number")
    result = float(value)
    if minimum is not None and result < minimum:
        raise ConfigError(f"{label} must be at least {minimum:g}")
    if maximum is not None and result > maximum:
        raise ConfigError(f"{label} must be at most {maximum:g}")
    return result


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{label} must be an integer of at least {minimum}")
    return value


def _host_load_gate(value: Any) -> HostLoadGateConfig:
    gate = _require_mapping(value, "host_load_gate")
    _reject_unknown(gate, {
        "enabled", "cpu_some_avg60_max", "mem_available_min_gib", "load_per_cpu_max",
        "disk_free_min_gib",
    }, "host_load_gate")
    defaults = HostLoadGateConfig()
    enabled = gate.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        raise ConfigError("host_load_gate.enabled must be boolean")
    load_per_cpu_max = _number(
        gate.get("load_per_cpu_max", defaults.load_per_cpu_max),
        "host_load_gate.load_per_cpu_max",
    )
    if load_per_cpu_max <= 0:
        raise ConfigError("host_load_gate.load_per_cpu_max must be greater than 0")
    return HostLoadGateConfig(
        enabled=enabled,
        cpu_some_avg60_max=_number(
            gate.get("cpu_some_avg60_max", defaults.cpu_some_avg60_max),
            "host_load_gate.cpu_some_avg60_max", minimum=0, maximum=100,
        ),
        mem_available_min_gib=_number(
            gate.get("mem_available_min_gib", defaults.mem_available_min_gib),
            "host_load_gate.mem_available_min_gib", minimum=0,
        ),
        load_per_cpu_max=load_per_cpu_max,
        disk_free_min_gib=_number(
            gate.get("disk_free_min_gib", defaults.disk_free_min_gib),
            "host_load_gate.disk_free_min_gib", minimum=0,
        ),
    )


def _safe_path(value: Any, label: str, source_dir: Path) -> Path:
    text = _string(value, label)
    path = Path(text).expanduser()
    if not path.is_absolute():
        if ".." in path.parts:
            raise ConfigError(f"{label} contains path traversal")
        path = source_dir / path
    return path.resolve(strict=False)


def _reject_inline_secrets(value: Any, trail: str = "config") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.lower().replace("-", "_")
            if (
                normalized not in _REFERENCE_KEYS and _SECRET_KEY_RE.search(normalized)
                and child not in (None, "", {}, [])
            ):
                raise ConfigError(f"inline secret-like field is forbidden: {trail}.{key}")
            if normalized in _REFERENCE_KEYS and isinstance(child, Mapping):
                # A binding maps an exported variable name to a declared ref id, so a
                # credential-shaped name (TYPESAFE_API_KEY) is the point, not an inline value.
                # _ref_map rejects any value that is not a declared ref id.
                for name, ref in child.items():
                    _reject_inline_secrets(ref, f"{trail}.{key}.{name}")
                continue
            _reject_inline_secrets(child, f"{trail}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_inline_secrets(child, f"{trail}[{index}]")


def _unique(rows: Sequence[Mapping[str, Any]], label: str) -> None:
    seen: set[str] = set()
    for index, row in enumerate(rows):
        item_id = _identifier(row.get("id"), f"{label}[{index}].id")
        if item_id in seen:
            raise ConfigError(f"duplicate {label} id: {item_id}")
        seen.add(item_id)


def _argv(value: Any, label: str, source_dir: Path) -> tuple[str, ...]:
    raw = _require_list(value, label)
    if not raw:
        raise ConfigError(f"{label} must not be empty")
    result: list[str] = []
    for index, item in enumerate(raw):
        text = _string(item, f"{label}[{index}]")
        if _SHELL_META_RE.search(text):
            raise ConfigError(f"unsafe argv token in {label}[{index}]")
        unknown = set(_PLACEHOLDER_RE.findall(text)) - _ALLOWED_PLACEHOLDERS
        if unknown:
            raise ConfigError(f"unsafe argv placeholder in {label}[{index}]: {min(unknown)}")
        result.append(text)
    executable = Path(result[0]).expanduser()
    if not executable.is_absolute():
        raise ConfigError(f"{label}[0] executable must be an absolute path")
    if executable.name in _SHELL_PROGRAMS or any(token in {"-c", "--command"} for token in result[1:]):
        raise ConfigError(f"unsafe shell execution is forbidden in {label}")
    result[0] = str(executable.resolve(strict=False))
    return tuple(result)


def _ref_map(value: Any, label: str, known: set[str]) -> Mapping[str, str]:
    if value is None:
        return {}
    raw = _require_mapping(value, label)
    result: dict[str, str] = {}
    for key, ref in raw.items():
        name = _identifier(key, f"{label} key")
        ref_id = _identifier(ref, f"{label}.{name}")
        if ref_id not in known:
            # Never echo the value: a pasted credential lands here once binding names may be
            # credential-shaped.
            raise ConfigError(f"{label}.{name} references an undeclared secret_refs id")
        result[name] = ref_id
    return result


def validate_config(
    raw: Mapping[str, Any],
    *,
    source_dir: str | os.PathLike[str] | Path | None = None,
    source_path: str | os.PathLike[str] | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Validate and normalize one JSON configuration object.

    Secret references are checked structurally but never resolved here.  This keeps commands
    such as ``plan`` and ``doctor`` from reading credentials merely to inspect configuration.
    """

    data = _require_mapping(raw, "config")
    _reject_unknown(data, {
        "schema_version", "database", "cache_dir", "record_command",
        "usage_max_age_seconds", "max_jobs", "recurrence_timezone", "secret_refs", "adapters", "providers", "proxy_secret_ref",
        "unmetered_max_inflight", "limits", "viewer", "pr_exceptions", "host_load_gate", "scout_ntfy_url",
    }, "config")
    _reject_inline_secrets(data)
    env = os.environ if environ is None else environ
    base = Path(source_dir or ".").expanduser().resolve(strict=False)
    schema_version = _integer(data.get("schema_version", 1), "schema_version", minimum=1)
    if schema_version != 1:
        raise ConfigError(f"unsupported schema_version: {schema_version}")
    recurrence_timezone = _string(
        data.get("recurrence_timezone", "America/New_York"),
        "recurrence_timezone",
    )
    try:
        ZoneInfo(recurrence_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown recurrence_timezone: {recurrence_timezone}") from exc

    secret_rows = [_require_mapping(row, f"secret_refs[{i}]") for i, row in enumerate(_require_list(data.get("secret_refs", []), "secret_refs"))]
    adapter_rows = [_require_mapping(row, f"adapters[{i}]") for i, row in enumerate(_require_list(data.get("adapters", []), "adapters"))]
    provider_rows = [_require_mapping(row, f"providers[{i}]") for i, row in enumerate(_require_list(data.get("providers", []), "providers"))]
    limit_rows = [_require_mapping(row, f"limits[{i}]") for i, row in enumerate(_require_list(data.get("limits", []), "limits"))]
    for label, rows in (
        ("secret_refs", secret_rows),
        ("adapters", adapter_rows),
        ("providers", provider_rows),
        ("limits", limit_rows),
    ):
        _unique(rows, label)

    secrets: list[SecretRef] = []
    for index, row in enumerate(secret_rows):
        _reject_unknown(row, {"id", "source", "name", "path"}, f"secret_refs[{index}]")
        item_id = _identifier(row.get("id"), f"secret_refs[{index}].id")
        source = _string(row.get("source"), f"secret_refs[{index}].source")
        if source not in {"env", "file"}:
            raise ConfigError(f"secret_refs[{index}].source must be env or file")
        name: str | None = None
        path: Path | None = None
        if source == "env":
            name = _string(row.get("name"), f"secret_refs[{index}].name")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ConfigError(f"secret_refs[{index}].name is not a safe environment variable")
        else:
            path = _safe_path(row.get("path"), f"secret_refs[{index}].path", base)
        secrets.append(SecretRef(item_id, source, name=name, path=path))
    secret_ids = {item.id for item in secrets}

    adapters: list[AdapterConfig] = []
    allowed_kinds = {"t3-thread"}
    for index, row in enumerate(adapter_rows):
        _reject_unknown(row, {
            "id", "kind", "argv", "timeout_seconds", "max_output_bytes",
            "env_allowlist", "secret_refs",
        }, f"adapters[{index}]")
        item_id = _identifier(row.get("id"), f"adapters[{index}].id")
        kind = _string(row.get("kind"), f"adapters[{index}].kind")
        if kind not in allowed_kinds:
            suffix = "; dispatch adapters must be t3-thread" if kind == "provider" else ""
            raise ConfigError(f"unsupported adapter kind {kind!r}{suffix}")
        timeout = _number(row.get("timeout_seconds", 30), f"adapters[{index}].timeout_seconds", minimum=0.1, maximum=3600)
        maximum = _integer(row.get("max_output_bytes", 1_048_576), f"adapters[{index}].max_output_bytes", minimum=1)
        allowlist_raw = _require_list(row.get("env_allowlist", []), f"adapters[{index}].env_allowlist")
        allowlist: list[str] = []
        for env_index, env_name in enumerate(allowlist_raw):
            env_text = _string(env_name, f"adapters[{index}].env_allowlist[{env_index}]")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_text):
                raise ConfigError(f"unsafe environment name: {env_text}")
            allowlist.append(env_text)
        adapters.append(
            AdapterConfig(
                id=item_id,
                kind=kind,
                argv=_argv(row.get("argv"), f"adapters[{index}].argv", base),
                timeout_seconds=timeout,
                max_output_bytes=maximum,
                env_allowlist=tuple(allowlist),
                secret_refs=_ref_map(row.get("secret_refs"), f"adapters[{index}].secret_refs", secret_ids),
            )
        )
    adapter_by_id = {adapter.id: adapter for adapter in adapters}
    providers: list[ProviderConfig] = []
    for index, row in enumerate(provider_rows):
        _reject_unknown(
            row, {"id", "dispatch", "max_size", "capabilities"},
            f"providers[{index}]",
        )
        item_id = _identifier(row.get("id"), f"providers[{index}].id")
        dispatch = _require_mapping(row.get("dispatch"), f"providers[{index}].dispatch")
        _reject_unknown(dispatch, {"adapter_id", "provider"}, f"providers[{index}].dispatch")
        adapter_id = _identifier(dispatch.get("adapter_id"), f"providers[{index}].dispatch.adapter_id")
        adapter = adapter_by_id.get(adapter_id)
        if adapter is None or adapter.kind not in allowed_kinds:
            raise ConfigError(f"provider {item_id} dispatch must reference an t3-thread adapter")
        thread_provider = _string(dispatch.get("provider", item_id), f"providers[{index}].dispatch.provider")
        capabilities_raw = _require_list(row.get("capabilities", []), f"providers[{index}].capabilities")
        capabilities = frozenset(_identifier(value, f"providers[{index}].capabilities") for value in capabilities_raw)
        providers.append(ProviderConfig(
            item_id, DispatchBinding(adapter_id, thread_provider), row.get("max_size"), capabilities,
        ))
    provider_ids = {provider.id for provider in providers}

    proxy_ref = data.get("proxy_secret_ref")
    if proxy_ref is not None:
        proxy_ref = _identifier(proxy_ref, "proxy_secret_ref")
        ref = next((item for item in secrets if item.id == proxy_ref), None)
        if ref is None or ref.source != "file":
            raise ConfigError("proxy_secret_ref must reference a file secret")
    elif providers:
        raise ConfigError("proxy_secret_ref is required with providers")

    limits: list[LimitConfig] = []
    for index, row in enumerate(limit_rows):
        _reject_unknown(row, {
            "id", "provider_id", "window_seconds", "ceiling_percent", "lead_seconds",
            "batch_size", "max_percent_per_window", "estimated_percent_per_job",
            "pacing_window_seconds", "urgency_seconds",
        }, f"limits[{index}]")
        item_id = _identifier(row.get("id"), f"limits[{index}].id")
        provider_id = _identifier(row.get("provider_id"), f"limits[{index}].provider_id")
        if provider_id not in provider_ids:
            raise ConfigError(f"limit {item_id} references missing provider {provider_id}")
        estimate_raw = row.get("estimated_percent_per_job")
        estimate = None if estimate_raw is None else _number(
            estimate_raw, f"limits[{index}].estimated_percent_per_job",
            minimum=0.000001, maximum=100,
        )
        window_seconds = _integer(row.get("window_seconds"), f"limits[{index}].window_seconds", minimum=1)
        urgency_seconds = _integer(
            row.get("urgency_seconds", 0), f"limits[{index}].urgency_seconds", minimum=0,
        )
        if urgency_seconds > window_seconds:
            raise ConfigError(
                f"limits[{index}].urgency_seconds must be at most window_seconds"
            )
        limits.append(
            LimitConfig(
                item_id,
                provider_id,
                window_seconds,
                _number(row.get("ceiling_percent"), f"limits[{index}].ceiling_percent", minimum=0, maximum=100),
                _integer(row.get("lead_seconds"), f"limits[{index}].lead_seconds", minimum=1),
                _integer(row.get("batch_size"), f"limits[{index}].batch_size", minimum=1),
                _number(
                    row.get("max_percent_per_window", 0),
                    f"limits[{index}].max_percent_per_window", minimum=0, maximum=100,
                ),
                estimate,
                _integer(
                    row.get("pacing_window_seconds", 3_600),
                    f"limits[{index}].pacing_window_seconds", minimum=1,
                ),
                urgency_seconds,
            )
        )

    record_command = _argv(data.get("record_command"), "record_command", base)
    # The JSON graph owns its database. BONUS_DB remains only in minimal_config for
    # deprecated queue-only compatibility and must never retarget a configured runtime.
    database_value = data.get("database")
    database = _safe_path(database_value, "database", base) if database_value else default_state_dir(env) / "queue.db"
    cache_value = data.get("cache_dir")
    cache_dir = _safe_path(cache_value, "cache_dir", base) if cache_value else default_cache_dir(env)

    scout_ntfy_url = data.get("scout_ntfy_url")
    if scout_ntfy_url is not None:
        scout_ntfy_url = _string(scout_ntfy_url, "scout_ntfy_url")
        try:
            parsed = urlsplit(scout_ntfy_url)
            port = parsed.port
        except ValueError as exc:
            raise ConfigError("scout_ntfy_url must be an absolute HTTP or HTTPS topic URL") from exc
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.path in {"", "/"} or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(char.isspace() for char in scout_ntfy_url)
            or len(scout_ntfy_url) > 2048
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ConfigError("scout_ntfy_url must be an absolute HTTP or HTTPS topic URL without credentials")

    viewer = dict(_require_mapping(data.get("viewer", {}), "viewer"))
    _reject_unknown(viewer, {"bind", "mutations_enabled", "remote", "preview"}, "viewer")
    if not isinstance(viewer.get("preview", False), bool):
        raise ConfigError("viewer.preview must be boolean")
    remote = viewer.get("remote")
    if remote is not None:
        remote_map = _require_mapping(remote, "viewer.remote")
        _reject_unknown(remote_map, {
            "trusted_loopback_proxy", "allowed_hosts",
            "allowed_origins", "https_terminated",
        }, "viewer.remote")
        trusted = remote_map.get("trusted_loopback_proxy", False)
        if not isinstance(trusted, bool):
            raise ConfigError("viewer.remote.trusted_loopback_proxy must be boolean")
        hosts = remote_map.get("allowed_hosts")
        origins = remote_map.get("allowed_origins")
        if not isinstance(hosts, list) or not hosts or any(not _exact_host(host) for host in hosts):
            raise ConfigError("viewer.remote.allowed_hosts must contain exact hosts")
        if not isinstance(origins, list) or not origins:
            raise ConfigError("viewer.remote.allowed_origins must contain exact HTTPS origins")
        for origin in origins:
            if not isinstance(origin, str):
                raise ConfigError("viewer.remote.allowed_origins must contain exact HTTPS origins")
            try:
                parsed = urlsplit(origin)
                parsed_port = parsed.port
            except ValueError as exc:
                raise ConfigError(
                    "viewer.remote.allowed_origins must contain exact HTTPS origins"
                ) from exc
            if (
                parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or parsed.username or parsed.password
                or any(char.isspace() for char in origin)
                or "*" in origin
                or (parsed_port is not None and not 1 <= parsed_port <= 65535)
            ):
                raise ConfigError("viewer.remote.allowed_origins must contain exact HTTPS origins")
        if trusted is not True:
            raise ConfigError("viewer.remote requires trusted_loopback_proxy=true")
        if not _loopback(viewer.get("bind", "127.0.0.1")):
            raise ConfigError("viewer.remote trusted loopback proxy requires loopback bind")

    pr_exceptions: list[Mapping[str, Any]] = []
    for index, item in enumerate(_require_list(data.get("pr_exceptions", []), "pr_exceptions")):
        row = dict(_require_mapping(item, f"pr_exceptions[{index}]"))
        _reject_unknown(row, {"path", "allow_push", "allow_pr"}, f"pr_exceptions[{index}]")
        row["path"] = str(_safe_path(row.get("path"), f"pr_exceptions[{index}].path", base))
        for option in ("allow_push", "allow_pr"):
            if option in row and not isinstance(row[option], bool):
                raise ConfigError(f"pr_exceptions[{index}].{option} must be boolean")
        pr_exceptions.append(row)

    return RuntimeConfig(
        schema_version=schema_version,
        source_path=Path(source_path).resolve(strict=False) if source_path else None,
        database=database,
        record_command=record_command,
        secret_refs=tuple(secrets),
        adapters=tuple(adapters),
        providers=tuple(providers),
        proxy_secret_ref=proxy_ref,
        limits=tuple(limits),
        viewer=viewer,
        pr_exceptions=tuple(pr_exceptions),
        recurrence_timezone=recurrence_timezone,
        usage_max_age_seconds=_integer(data.get("usage_max_age_seconds", 3_600), "usage_max_age_seconds", minimum=1),
        cache_dir=cache_dir,
        unmetered_max_inflight=_integer(data.get("unmetered_max_inflight", 2), "unmetered_max_inflight", minimum=1),
        max_jobs=(
            None if data.get("max_jobs") is None
            else _integer(data.get("max_jobs"), "max_jobs", minimum=1)
        ),
        host_load_gate=_host_load_gate(data.get("host_load_gate", {})),
        scout_ntfy_url=scout_ntfy_url,
    )


def load_config(
    path: str | os.PathLike[str] | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Load one JSON configuration file without resolving any secret values."""

    env = os.environ if environ is None else environ
    config_path = Path(path).expanduser() if path is not None else default_config_path(env)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ConfigError(f"invalid JSON config {config_path}: {exc}") from exc
    return validate_config(
        raw,
        source_dir=config_path.parent,
        source_path=config_path,
        environ=env,
    )


def load_terminal_config(
    path: str | os.PathLike[str] | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Read receipt authority without interpreting the current provider graph."""
    env = os.environ if environ is None else environ
    config_path = Path(path).expanduser()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ConfigError(f"cannot read terminal config {config_path}") from exc
    data = _require_mapping(raw, "terminal config")
    database_value = data.get("database")
    database = (
        _safe_path(database_value, "database", config_path.parent.resolve(strict=False))
        if database_value is not None else default_state_dir(env) / "queue.db"
    )
    timezone = _string(data.get("recurrence_timezone", "America/New_York"), "recurrence_timezone")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ConfigError("recurrence_timezone must be an IANA timezone") from exc
    return replace(
        minimal_config(database=database, environ=env),
        source_path=config_path.resolve(strict=False), recurrence_timezone=timezone,
    )


def minimal_config(
    *,
    database: str | os.PathLike[str] | Path | None = None,
    record_command: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Return a graph-free configuration for legacy queue-only commands.

    This is deliberately unavailable to planner/dispatch callers; it exists so historical
    ``bonusdb.sh init/add/status`` invocations do not require a provider configuration.
    ``BONUS_DB`` is a deprecated compatibility fallback on this graph-free path only.
    """

    env = os.environ if environ is None else environ
    db_path = Path(database or env.get("BONUS_DB") or (default_state_dir(env) / "queue.db")).expanduser().resolve(strict=False)
    command = tuple(record_command or (str(Path(__file__).resolve().parents[1] / "bin" / "bonus-drain"), "record"))
    return RuntimeConfig(
        schema_version=1,
        source_path=None,
        database=db_path,
        record_command=command,
        secret_refs=(),
        adapters=(),
        providers=(),
        proxy_secret_ref=None,
        limits=(),
        viewer={"bind": "127.0.0.1", "mutations_enabled": False},
        pr_exceptions=(),
        cache_dir=default_cache_dir(env),
    )


def resolve_secret(ref: SecretRef, *, environ: Mapping[str, str] | None = None) -> str:
    """Resolve a secret only for an adapter that is about to execute."""

    env = os.environ if environ is None else environ
    if ref.source == "env":
        value = env.get(ref.name or "")
        if value is None:
            raise ConfigError(f"secret reference {ref.id} environment variable is unavailable")
        return value
    assert ref.path is not None
    path = ref.path
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ConfigError(f"secret reference {ref.id} path is a symlink, not a safe regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ConfigError(f"secret reference {ref.id} must be a regular file")
            if metadata.st_uid != os.getuid():
                raise ConfigError(f"secret reference {ref.id} file is not owned by the current user")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ConfigError(f"secret reference {ref.id} file mode must be private 0600")
            if metadata.st_size > 65_536:
                raise ConfigError(f"secret reference {ref.id} file size exceeds 65536 bytes")
            payload = os.read(descriptor, 65_537)
        finally:
            os.close(descriptor)
        if len(payload) > 65_536:
            raise ConfigError(f"secret reference {ref.id} file is too large")
        return payload.decode("utf-8").rstrip("\r\n")
    except ConfigError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"secret reference {ref.id} file is unavailable or invalid") from exc
