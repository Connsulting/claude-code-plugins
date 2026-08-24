"""Strict, stdlib-only configuration for Bonus Drain.

The configuration graph is intentionally expressed in terms of ids and adapters.  Provider
names are data; none of them have semantic meaning to this module or to the planner.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit


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
    "auth_secret_ref",
    "secret_ref",
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
    capabilities: frozenset[str] = frozenset()
    account_mode: str = "multi"


@dataclass(frozen=True)
class PlanConfig:
    id: str
    provider_id: str


@dataclass(frozen=True)
class AccountConfig:
    id: str
    provider_id: str
    plan_id: str
    usage_adapter_id: str | None = None
    activation_adapter_id: str | None = None
    secret_refs: Mapping[str, str] = field(default_factory=dict)
    reset_adapter_id: str | None = None


@dataclass(frozen=True)
class LimitConfig:
    id: str
    plan_id: str
    window_seconds: int
    ceiling_percent: float
    lead_seconds: int
    batch_size: int
    max_percent_per_window: float = 0.0
    estimated_percent_per_job: float | None = None
    pacing_window_seconds: int = 18_000


@dataclass(frozen=True)
class RuntimeConfig:
    schema_version: int
    source_path: Path | None
    database: Path
    record_command: tuple[str, ...]
    secret_refs: tuple[SecretRef, ...]
    adapters: tuple[AdapterConfig, ...]
    providers: tuple[ProviderConfig, ...]
    plans: tuple[PlanConfig, ...]
    accounts: tuple[AccountConfig, ...]
    limits: tuple[LimitConfig, ...]
    viewer: Mapping[str, Any]
    pr_exceptions: tuple[Mapping[str, Any], ...]
    usage_max_age_seconds: int = 3_600
    cache_dir: Path = Path(".")

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

    def plan(self, plan_id: str) -> PlanConfig:
        return _lookup(self.plans, plan_id, "plan")

    def account(self, account_id: str) -> AccountConfig:
        return _lookup(self.accounts, account_id, "account")

    def limits_for_plan(self, plan_id: str) -> tuple[LimitConfig, ...]:
        return tuple(limit for limit in self.limits if limit.plan_id == plan_id)

    def accounts_for_provider(self, provider_id: str) -> tuple[AccountConfig, ...]:
        return tuple(account for account in self.accounts if account.provider_id == provider_id)


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
            if normalized not in _REFERENCE_KEYS and _SECRET_KEY_RE.search(normalized):
                if child not in (None, "", {}, []):
                    raise ConfigError(f"inline secret-like field is forbidden: {trail}.{key}")
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
            raise ConfigError(f"unsafe argv placeholder in {label}[{index}]: {sorted(unknown)[0]}")
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
            raise ConfigError(f"{label}.{name} references missing-ref {ref_id}")
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
        "usage_max_age_seconds", "secret_refs", "adapters", "providers", "plans",
        "accounts", "limits", "viewer", "pr_exceptions",
    }, "config")
    _reject_inline_secrets(data)
    env = os.environ if environ is None else environ
    base = Path(source_dir or ".").expanduser().resolve(strict=False)
    schema_version = _integer(data.get("schema_version", 1), "schema_version", minimum=1)
    if schema_version != 1:
        raise ConfigError(f"unsupported schema_version: {schema_version}")

    secret_rows = [_require_mapping(row, f"secret_refs[{i}]") for i, row in enumerate(_require_list(data.get("secret_refs", []), "secret_refs"))]
    adapter_rows = [_require_mapping(row, f"adapters[{i}]") for i, row in enumerate(_require_list(data.get("adapters", []), "adapters"))]
    provider_rows = [_require_mapping(row, f"providers[{i}]") for i, row in enumerate(_require_list(data.get("providers", []), "providers"))]
    plan_rows = [_require_mapping(row, f"plans[{i}]") for i, row in enumerate(_require_list(data.get("plans", []), "plans"))]
    account_rows = [_require_mapping(row, f"accounts[{i}]") for i, row in enumerate(_require_list(data.get("accounts", []), "accounts"))]
    limit_rows = [_require_mapping(row, f"limits[{i}]") for i, row in enumerate(_require_list(data.get("limits", []), "limits"))]
    for label, rows in (
        ("secret_refs", secret_rows),
        ("adapters", adapter_rows),
        ("providers", provider_rows),
        ("plans", plan_rows),
        ("accounts", account_rows),
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
    allowed_kinds = {"agent-router", "usage", "reset", "activation"}
    for index, row in enumerate(adapter_rows):
        _reject_unknown(row, {
            "id", "kind", "argv", "timeout_seconds", "max_output_bytes",
            "env_allowlist", "secret_refs",
        }, f"adapters[{index}]")
        item_id = _identifier(row.get("id"), f"adapters[{index}].id")
        kind = _string(row.get("kind"), f"adapters[{index}].kind")
        if kind not in allowed_kinds:
            suffix = "; dispatch adapters must be agent-router" if kind == "provider" else ""
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
        _reject_unknown(row, {"id", "dispatch", "capabilities", "account_mode"}, f"providers[{index}]")
        item_id = _identifier(row.get("id"), f"providers[{index}].id")
        dispatch = _require_mapping(row.get("dispatch"), f"providers[{index}].dispatch")
        _reject_unknown(dispatch, {"adapter_id", "provider"}, f"providers[{index}].dispatch")
        adapter_id = _identifier(dispatch.get("adapter_id"), f"providers[{index}].dispatch.adapter_id")
        adapter = adapter_by_id.get(adapter_id)
        if adapter is None or adapter.kind != "agent-router":
            raise ConfigError(f"provider {item_id} dispatch must reference an agent-router adapter")
        router_provider = _string(dispatch.get("provider", item_id), f"providers[{index}].dispatch.provider")
        capabilities_raw = _require_list(row.get("capabilities", []), f"providers[{index}].capabilities")
        capabilities = frozenset(_identifier(value, f"providers[{index}].capabilities") for value in capabilities_raw)
        account_mode = _string(row.get("account_mode", "multi"), f"providers[{index}].account_mode")
        if account_mode not in {"single", "multi"}:
            raise ConfigError(f"providers[{index}].account_mode must be single or multi")
        providers.append(ProviderConfig(item_id, DispatchBinding(adapter_id, router_provider), capabilities, account_mode))
    provider_ids = {provider.id for provider in providers}

    plans: list[PlanConfig] = []
    for index, row in enumerate(plan_rows):
        _reject_unknown(row, {"id", "provider_id"}, f"plans[{index}]")
        item_id = _identifier(row.get("id"), f"plans[{index}].id")
        provider_id = _identifier(row.get("provider_id"), f"plans[{index}].provider_id")
        if provider_id not in provider_ids:
            raise ConfigError(f"plan {item_id} references missing provider {provider_id}")
        plans.append(PlanConfig(item_id, provider_id))
    plan_by_id = {plan.id: plan for plan in plans}

    accounts: list[AccountConfig] = []
    for index, row in enumerate(account_rows):
        _reject_unknown(row, {
            "id", "provider_id", "plan_id", "usage_adapter_id", "reset_adapter_id",
            "activation_adapter_id", "secret_refs",
        }, f"accounts[{index}]")
        item_id = _identifier(row.get("id"), f"accounts[{index}].id")
        provider_id = _identifier(row.get("provider_id"), f"accounts[{index}].provider_id")
        plan_id = _identifier(row.get("plan_id"), f"accounts[{index}].plan_id")
        plan = plan_by_id.get(plan_id)
        if provider_id not in provider_ids:
            raise ConfigError(f"account {item_id} references missing provider {provider_id}")
        if plan is None or plan.provider_id != provider_id:
            raise ConfigError(f"account {item_id} has a missing or cross-provider plan {plan_id}")
        usage_adapter_id = row.get("usage_adapter_id")
        reset_adapter_id = row.get("reset_adapter_id")
        activation_adapter_id = row.get("activation_adapter_id")
        if usage_adapter_id is not None:
            usage_adapter_id = _identifier(usage_adapter_id, f"accounts[{index}].usage_adapter_id")
            adapter = adapter_by_id.get(usage_adapter_id)
            if adapter is None or adapter.kind != "usage":
                raise ConfigError(f"account {item_id} usage adapter is missing or not kind usage")
        if reset_adapter_id is not None:
            reset_adapter_id = _identifier(reset_adapter_id, f"accounts[{index}].reset_adapter_id")
            adapter = adapter_by_id.get(reset_adapter_id)
            if adapter is None or adapter.kind != "reset":
                raise ConfigError(f"account {item_id} reset adapter is missing or not kind reset")
        if activation_adapter_id is not None:
            activation_adapter_id = _identifier(activation_adapter_id, f"accounts[{index}].activation_adapter_id")
            adapter = adapter_by_id.get(activation_adapter_id)
            if adapter is None or adapter.kind != "activation":
                raise ConfigError(f"account {item_id} activation adapter is missing or not kind activation")
        accounts.append(
            AccountConfig(
                item_id,
                provider_id,
                plan_id,
                usage_adapter_id=usage_adapter_id,
                reset_adapter_id=reset_adapter_id,
                activation_adapter_id=activation_adapter_id,
                secret_refs=_ref_map(row.get("secret_refs"), f"accounts[{index}].secret_refs", secret_ids),
            )
        )

    accounts_by_provider: dict[str, list[AccountConfig]] = {}
    for account in accounts:
        accounts_by_provider.setdefault(account.provider_id, []).append(account)

    def adapter_option(adapter: AdapterConfig, option: str) -> str | None:
        try:
            index = adapter.argv.index(option)
        except ValueError:
            return None
        return adapter.argv[index + 1] if index + 1 < len(adapter.argv) else None

    for provider_id, provider_accounts in accounts_by_provider.items():
        provider = next(item for item in providers if item.id == provider_id)
        if provider.account_mode == "single" and len(provider_accounts) != 1:
            raise ConfigError(f"single-account provider {provider_id} requires exactly one configured account identity")
        if len(provider_accounts) < 2:
            continue
        activation_domains: set[tuple[str | None, str | None, str | None]] = set()
        for account in provider_accounts:
            if not account.activation_adapter_id:
                raise ConfigError(
                    f"multi-account provider {provider_id} requires activation for every account"
                )
            adapter = adapter_by_id[account.activation_adapter_id]
            verified = (
                Path(adapter.argv[0]).name == "bonus-drain-account-activation"
                and adapter_option(adapter, "--action") == "{action}"
                and adapter_option(adapter, "--account") == "{account_id}"
                and adapter_option(adapter, "--expected-account") == account.id
                and adapter_option(adapter, "--pin-file") is not None
                and adapter_option(adapter, "--active-path") is not None
            )
            if not verified:
                raise ConfigError(
                    f"multi-account provider {provider_id} account {account.id} requires a verified activation adapter with active-account proof"
                )
            activation_domains.add((
                adapter_option(adapter, "--pin-file"),
                adapter_option(adapter, "--active-path"),
                adapter_option(adapter, "--rotate"),
            ))
        if len(activation_domains) != 1:
            raise ConfigError(
                f"multi-account provider {provider_id} activation adapters must share one PIN, active proof, and rotator domain"
            )

    direct_grok_providers: set[str] = set()
    for account in accounts:
        if not account.usage_adapter_id:
            continue
        adapter = adapter_by_id[account.usage_adapter_id]
        if Path(adapter.argv[0]).name == "bonus-drain-grok-usage":
            direct_grok_providers.add(account.provider_id)
            nested_timeout = adapter_option(adapter, "--timeout")
            try:
                nested_seconds = float(nested_timeout) if nested_timeout is not None else None
            except ValueError as exc:
                raise ConfigError(f"direct Grok adapter {adapter.id} has an invalid nested timeout") from exc
            if nested_seconds is None or nested_seconds >= adapter.timeout_seconds:
                raise ConfigError(
                    f"direct Grok adapter {adapter.id} nested timeout must be shorter than its outer adapter timeout"
                )
    for provider_id in direct_grok_providers:
        provider = next(item for item in providers if item.id == provider_id)
        if provider.account_mode != "single" or len(accounts_by_provider.get(provider_id, ())) != 1:
            raise ConfigError(
                f"direct Grok provider {provider_id} must declare single account mode and exactly one configured account identity"
            )

    limits: list[LimitConfig] = []
    for index, row in enumerate(limit_rows):
        _reject_unknown(row, {
            "id", "plan_id", "window_seconds", "ceiling_percent", "lead_seconds",
            "batch_size", "max_percent_per_window", "estimated_percent_per_job",
            "pacing_window_seconds",
        }, f"limits[{index}]")
        item_id = _identifier(row.get("id"), f"limits[{index}].id")
        plan_id = _identifier(row.get("plan_id"), f"limits[{index}].plan_id")
        if plan_id not in plan_by_id:
            raise ConfigError(f"limit {item_id} references missing plan {plan_id}")
        estimate_raw = row.get("estimated_percent_per_job")
        estimate = None if estimate_raw is None else _number(estimate_raw, f"limits[{index}].estimated_percent_per_job", minimum=0.000001, maximum=100)
        limits.append(
            LimitConfig(
                item_id,
                plan_id,
                _integer(row.get("window_seconds"), f"limits[{index}].window_seconds", minimum=1),
                _number(row.get("ceiling_percent"), f"limits[{index}].ceiling_percent", minimum=0, maximum=100),
                _integer(row.get("lead_seconds"), f"limits[{index}].lead_seconds", minimum=1),
                _integer(row.get("batch_size"), f"limits[{index}].batch_size", minimum=1),
                _number(row.get("max_percent_per_window", 0), f"limits[{index}].max_percent_per_window", minimum=0, maximum=100),
                estimate,
                _integer(row.get("pacing_window_seconds", 18_000), f"limits[{index}].pacing_window_seconds", minimum=1),
            )
        )

    record_command = _argv(data.get("record_command"), "record_command", base)
    database_value = env.get("BONUS_DB") or data.get("database")
    database = _safe_path(database_value, "database", base) if database_value else default_state_dir(env) / "queue.db"
    cache_value = data.get("cache_dir")
    cache_dir = _safe_path(cache_value, "cache_dir", base) if cache_value else default_cache_dir(env)

    viewer = dict(_require_mapping(data.get("viewer", {}), "viewer"))
    _reject_unknown(viewer, {"bind", "mutations_enabled", "remote"}, "viewer")
    remote = viewer.get("remote")
    if remote is not None:
        remote_map = _require_mapping(remote, "viewer.remote")
        _reject_unknown(remote_map, {
            "auth_secret_ref", "trusted_loopback_proxy", "allowed_hosts",
            "allowed_origins", "https_terminated",
        }, "viewer.remote")
        auth_ref = remote_map.get("auth_secret_ref")
        if auth_ref is not None and auth_ref not in secret_ids:
            raise ConfigError(f"viewer.remote.auth_secret_ref references missing-ref {auth_ref}")
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
        if trusted:
            if auth_ref is not None:
                raise ConfigError(
                    "viewer.remote trusted loopback proxy cannot also use auth_secret_ref"
                )
            if not _loopback(viewer.get("bind", "127.0.0.1")):
                raise ConfigError("viewer.remote trusted loopback proxy requires loopback bind")
            if viewer.get("mutations_enabled", False) is not False:
                raise ConfigError("viewer.remote trusted loopback proxy requires mutations disabled")
        elif not isinstance(auth_ref, str) or not auth_ref:
            raise ConfigError(
                "viewer.remote requires auth_secret_ref unless trusted_loopback_proxy is true"
            )

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
        plans=tuple(plans),
        accounts=tuple(accounts),
        limits=tuple(limits),
        viewer=viewer,
        pr_exceptions=tuple(pr_exceptions),
        usage_max_age_seconds=_integer(data.get("usage_max_age_seconds", 3_600), "usage_max_age_seconds", minimum=1),
        cache_dir=cache_dir,
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


def minimal_config(
    *,
    database: str | os.PathLike[str] | Path | None = None,
    record_command: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Return a graph-free configuration for legacy queue-only commands.

    This is deliberately unavailable to planner/dispatch callers; it exists so historical
    ``bonusdb.sh init/add/status`` invocations do not require a provider configuration.
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
        plans=(),
        accounts=(),
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
