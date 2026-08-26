"""Claimed, router-only dispatch with concrete provider bookkeeping."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import time
from hashlib import sha256
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import AccountConfig, AdapterConfig, ConfigError, ProviderConfig, RuntimeConfig
from .db import (
    LEGACY_EXCLUSIVE_CAPABILITY,
    QueueDB,
    Task,
    cycle_from_key,
    task_requires_legacy_exclusive,
)


class DispatchError(RuntimeError):
    """Base dispatch failure."""


class InvalidRoute(DispatchError):
    """Requested or classified provider is invalid for the task."""


class AlreadyClaimed(DispatchError):
    """The task became ineligible before the atomic claim."""


class KnownDispatchFailure(DispatchError):
    """The router positively reported that no launch occurred."""


class ClassificationFailure(DispatchError):
    """A non-launching pre-claim router classification could not complete."""


class AmbiguousDispatch(DispatchError):
    """The router response cannot prove whether a launch occurred."""


class ActivationUnavailable(DispatchError):
    """Another durable account lease currently owns this provider."""


_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MCP_FILE_LIMIT = 1_048_576
_MCP_SERVER_LIMIT = 64
_MCP_SELECTION_LIMIT = 32
_MCP_STALE_SECONDS = 30 * 24 * 60 * 60
_MCP_ENV_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_MCP_SENSITIVE_RE = re.compile(
    r"(?:authorization|cookie|credential|password|secret|token|api[-_]?key)", re.IGNORECASE
)
_MCP_OWNED_FILE_RE = re.compile(r"^task-[0-9a-f]{24}\.json$")
_MCP_FLAG_REJECTION_RE = re.compile(
    r"(?:unknown|unrecognized|unexpected)\s+(?:option|argument|flag)"
    r"|(?:option|argument|flag)\s+(?:is\s+)?(?:unknown|unrecognized|unexpected)",
    re.IGNORECASE,
)
_MCP_CLAUDE_ONLY_RE = re.compile(
    r"--(?:strict-)?mcp-config\s+is\s+a\s+claude\s+only\s+flag",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DispatchResult:
    task_id: str
    eligibility_key: str
    provider_id: str
    account_id: str | None
    job_id: str
    prompt: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def provider_compatible(task: Task, provider: ProviderConfig) -> bool:
    if task.allowed_providers and provider.id not in task.allowed_providers:
        return False
    if task_requires_legacy_exclusive(task) and \
       LEGACY_EXCLUSIVE_CAPABILITY not in provider.capabilities:
        return False
    return set(task.required_capabilities).issubset(provider.capabilities)


def _record_line(
    config: RuntimeConfig,
    task: Task,
    eligibility_key: str,
    provider_id: str,
    account_id: str | None,
) -> str:
    command = list(config.record_command)
    command.extend(
        [
            "--database", str(config.database),
            "--task", task.id,
            "--kind", task.kind,
            "--eligibility-key", eligibility_key,
            "--cycle", str(cycle_from_key(eligibility_key)),
            "--status", "done|skipped|failed",
            "--provider-id", provider_id,
        ]
    )
    if account_id:
        command.extend(["--account-id", account_id])
    command.extend(["--summary", "<one line>"])
    return shlex.join(command)


def _pr_policy(config: RuntimeConfig, task: Task) -> str:
    cwd = Path(task.cwd).expanduser().resolve(strict=False)
    for exception in config.pr_exceptions:
        root = Path(str(exception["path"])).expanduser().resolve(strict=False)
        try:
            cwd.relative_to(root)
        except ValueError:
            continue
        if bool(exception.get("allow_pr", exception.get("allow_push", False))):
            return "This configured repository permits a branch push and pull request; never merge it."
        if bool(exception.get("allow_push", False)):
            return "This configured repository permits pushing the committed branch; do not open or merge a pull request."
    return "Produce a branch and committed artifact only; do not push, publish, merge, or delete unrelated files."


def render_prompt(
    config: RuntimeConfig,
    task: Task,
    eligibility_key: str,
    provider_id: str,
    account_id: str | None,
) -> str:
    """Render one task and the stable terminal-record contract."""

    sections = [f"Goal: {task.goal}"]
    for label, value in (
        ("Context", task.context),
        ("Constraints", task.constraints),
        ("Precondition", task.precondition),
        ("Done when", task.done_when),
    ):
        if value:
            sections.append(f"{label}:\n{value}")
    if task.kind == "oneoff":
        contract = [
            "--- BONUS TASK EXECUTION CONTRACT ---",
            "This is opportunistic work funded by otherwise expiring capacity.",
            _pr_policy(config, task),
            "Run the precondition first. If it is unmet, record skipped immediately.",
            "On bounded ambiguity, choose the reasonable default, note it, and continue without asking for input.",
        ]
    else:
        contract = [
            "--- RECURRING BONUS JOB EXECUTION CONTRACT ---",
            "Run this vetted recurring operation with its configured mandate unchanged.",
            "Run the precondition first. If it is unmet, record skipped immediately.",
            "On bounded ambiguity, choose the reasonable default, note it, and continue without asking for input.",
        ]
    contract.extend(
        [
            f"The concrete provider for this accounted run is {provider_id}.",
            "When finished, record exactly one terminal event with this command (replace only the status and summary placeholders):",
            f"  {_record_line(config, task, eligibility_key, provider_id, account_id)}",
            "Do not replace the task id or eligibility key and do not stop an idle background session.",
        ]
    )
    prompt = "\n\n".join(sections + ["\n".join(contract)])
    if task.use_implement:
        return f"/implement {prompt}\nBACKGROUND_RUN=1"
    return prompt


def classification_prompt(task: Task) -> str:
    fields = [
        f"Title: {task.title}", f"Goal: {task.goal}", f"Working directory: {task.cwd}",
        f"Allowed provider ids: {', '.join(task.allowed_providers) if task.allowed_providers else 'any configured provider'}",
        f"Required capabilities: {', '.join(task.required_capabilities) if task.required_capabilities else 'none'}",
    ]
    for label, value in (
        ("Context", task.context), ("Constraints", task.constraints),
        ("Precondition", task.precondition), ("Done when", task.done_when),
    ):
        if value:
            fields.append(f"{label}: {value[:2000]}")
    return "\n".join(fields)


def _safe_environment(adapter: AdapterConfig) -> dict[str, str]:
    permitted = {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"}
    permitted.update(adapter.env_allowlist)
    # Hermetic router adapters commonly expose their output/control channel through these
    # non-credential variables. Keep this narrow instead of inheriting the whole environment.
    permitted.update(name for name in os.environ if name.startswith("ROUTER_"))
    return {name: os.environ[name] for name in permitted if name in os.environ}


def _read_mcp_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > _MCP_FILE_LIMIT:
            raise DispatchError(f"{label} is missing or exceeds {_MCP_FILE_LIMIT} bytes")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DispatchError(f"{label} is not readable JSON") from exc
    if not isinstance(value, Mapping):
        raise DispatchError(f"{label} must contain an object")
    return value


def _sanitize_mcp_value(value: Any, label: str) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized_key = key.lower()
            if normalized_key == "env":
                if not isinstance(child, Mapping):
                    raise DispatchError(f"{label}.env must be an object")
                clean_env: dict[str, str] = {}
                for raw_name, raw_value in child.items():
                    name = str(raw_name)
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                        raise DispatchError(f"{label}.env contains an unsafe variable name")
                    if not isinstance(raw_value, str) or not _MCP_ENV_REF_RE.fullmatch(raw_value):
                        raise DispatchError(
                            f"{label}.env.{name} must be an external ${{NAME}} reference"
                        )
                    clean_env[name] = raw_value
                result[key] = clean_env
                continue
            if normalized_key == "headers":
                if not isinstance(child, Mapping):
                    raise DispatchError(f"{label}.headers must be an object")
                clean_headers: dict[str, Any] = {}
                for raw_name, raw_value in child.items():
                    name = str(raw_name)
                    if _MCP_SENSITIVE_RE.search(name) and (
                        not isinstance(raw_value, str)
                        or not _MCP_ENV_REF_RE.fullmatch(raw_value)
                    ):
                        raise DispatchError(
                            f"{label}.headers.{name} must be an external ${{NAME}} reference"
                        )
                    clean_headers[name] = _sanitize_mcp_value(
                        raw_value, f"{label}.headers.{name}"
                    )
                result[key] = clean_headers
                continue
            if _MCP_SENSITIVE_RE.search(key) and (
                not isinstance(child, str) or not _MCP_ENV_REF_RE.fullmatch(child)
            ):
                raise DispatchError(f"{label}.{key} must be an external ${{NAME}} reference")
            result[key] = _sanitize_mcp_value(child, f"{label}.{key}")
        return result
    if isinstance(value, list):
        result = [_sanitize_mcp_value(item, f"{label}[{index}]") for index, item in enumerate(value)]
        for index, item in enumerate(result):
            if isinstance(item, str) and item.startswith("-") and _MCP_SENSITIVE_RE.search(item):
                candidate = item.split("=", 1)[1] if "=" in item else (
                    result[index + 1] if index + 1 < len(result) else None
                )
                if not isinstance(candidate, str) or not _MCP_ENV_REF_RE.fullmatch(candidate):
                    raise DispatchError(
                        f"{label}[{index}] secret argument must use an external ${{NAME}} reference"
                    )
        return result
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise DispatchError(f"{label} contains a non-JSON value")


def _mcp_servers(value: Mapping[str, Any], label: str) -> dict[str, Mapping[str, Any]]:
    raw = value.get("mcpServers", {})
    if not isinstance(raw, Mapping) or len(raw) > _MCP_SERVER_LIMIT:
        raise DispatchError(f"{label}.mcpServers must be a bounded object")
    result: dict[str, Mapping[str, Any]] = {}
    for raw_name, definition in raw.items():
        name = str(raw_name)
        if not _MCP_NAME_RE.fullmatch(name) or not isinstance(definition, Mapping):
            raise DispatchError(f"{label} contains an invalid MCP server")
        sanitized = _sanitize_mcp_value(definition, f"MCP server {name}")
        # Round-trip through JSON to bound the sanitized definition and detach it from input.
        encoded = json.dumps(sanitized, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MCP_FILE_LIMIT:
            raise DispatchError(f"MCP server {name} exceeds {_MCP_FILE_LIMIT} bytes")
        result[name] = json.loads(encoded)
    return result


def _resolved_mcp_servers(task: Task) -> dict[str, Mapping[str, Any]]:
    raw_mcp = (task.mcp or "").strip()
    if not raw_mcp:
        return {}
    if raw_mcp == "none":
        return {}

    cwd = Path(task.cwd).expanduser().resolve(strict=False)
    candidate = Path(raw_mcp).expanduser()
    if candidate.is_absolute() or "/" in raw_mcp or raw_mcp.endswith(".json"):
        if not candidate.is_absolute():
            if ".." in candidate.parts:
                raise DispatchError("relative MCP config path may not traverse parents")
            candidate = cwd / candidate
        return _mcp_servers(_read_mcp_json(candidate.resolve(strict=False), "task MCP config"), "task MCP config")

    names = [item.strip() for item in raw_mcp.split(",") if item.strip()]
    if not names or len(names) > _MCP_SELECTION_LIMIT or len(names) != len(set(names)):
        raise DispatchError("MCP selection must contain 1..32 unique server names")
    if any(not _MCP_NAME_RE.fullmatch(name) for name in names):
        raise DispatchError("MCP selection contains an unsafe server name")

    merged: dict[str, Mapping[str, Any]] = {}
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    global_path = home / ".claude.json"
    global_value: Mapping[str, Any] = {}
    if global_path.is_file():
        global_value = _read_mcp_json(global_path, "user MCP config")
        merged.update(_mcp_servers(global_value, "user MCP config"))

    ancestors: list[Path] = []
    repository_found = False
    for directory in (cwd, *cwd.parents):
        ancestors.append(directory)
        if (directory / ".git").exists():
            repository_found = True
            break
    if not repository_found:
        # Without a repository boundary, never walk arbitrary parents for MCP configuration.
        ancestors = [cwd]
    for directory in reversed(ancestors):
        path = directory / ".mcp.json"
        if path.is_file():
            merged.update(_mcp_servers(_read_mcp_json(path, "project MCP config"), "project MCP config"))
    projects = global_value.get("projects", {})
    if projects is not None and not isinstance(projects, Mapping):
        raise DispatchError("user MCP config projects must be an object")
    if isinstance(projects, Mapping):
        for directory in reversed(ancestors):
            project = projects.get(str(directory), {})
            if project:
                if not isinstance(project, Mapping):
                    raise DispatchError("user MCP project entry must be an object")
                merged.update(_mcp_servers(project, "user MCP project entry"))

    missing = [name for name in names if name not in merged]
    if missing:
        raise DispatchError(f"MCP server is not resolvable: {missing[0]}")
    return {name: merged[name] for name in names}


def _prune_owned_mcp_files(directory: Path, current: Path) -> None:
    cutoff = time.time() - _MCP_STALE_SECONDS
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry == current or not _MCP_OWNED_FILE_RE.fullmatch(entry.name):
            continue
        try:
            metadata = entry.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mtime >= cutoff
            ):
                continue
            entry.unlink()
        except OSError:
            continue


def _materialize_mcp_config(config: RuntimeConfig, task: Task, _eligibility_key: str) -> Path | None:
    if task.mcp is None or not task.mcp.strip():
        return None
    servers = _resolved_mcp_servers(task)
    directory = config.state_dir / "mcp"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise DispatchError("MCP state directory is not an owned directory")
    directory.chmod(0o700)
    digest = sha256(task.id.encode("utf-8")).hexdigest()[:24]
    path = directory / f"task-{digest}.json"
    _prune_owned_mcp_files(directory, path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".task-{digest}-", suffix=".tmp", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        payload = json.dumps({"mcpServers": servers}, sort_keys=True, separators=(",", ":")) + "\n"
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        return path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _uses_claude_mcp_scoping(provider: ProviderConfig) -> bool:
    """Whether this agent-router provider accepts Claude's task MCP flags.

    MCP selection is a Claude Code-only router contract.  Codex deliberately uses its
    configured connectors instead of a task-provided MCP file, so do not validate,
    materialize, or pass task MCP state for it (or for other providers).
    """

    return provider.dispatch.provider == "claude"


def _phase_uncertainty(phase: str, message: str) -> DispatchError:
    if phase == "classification":
        detail = message.replace("; launch state is unknown", "")
        return ClassificationFailure(
            f"{detail}; dry-run classification failed before claim and is retry-safe"
        )
    return AmbiguousDispatch(message)


def _explicitly_not_launched(value: Mapping[str, Any]) -> bool:
    containers = [value]
    dispatch_value = value.get("dispatch")
    if isinstance(dispatch_value, Mapping):
        containers.append(dispatch_value)
    launch_values = [item.get("launched") for item in containers if "launched" in item]
    if not launch_values or any(item is not False for item in launch_values):
        return False
    return not any(
        isinstance(item.get("job_id"), str) and bool(item.get("job_id"))
        for item in containers
    )


def _positive_prelaunch_mcp_flag_rejection(stdout: str, stderr: str) -> str | None:
    """Return a parser diagnostic that proves a rejected MCP flag never launched work."""

    for line in reversed((stderr + "\n" + stdout).splitlines()):
        normalized = line.strip()
        if (
            "--mcp-config" in normalized or "--strict-mcp-config" in normalized
        ) and (
            _MCP_FLAG_REJECTION_RE.search(normalized)
            or _MCP_CLAUDE_ONLY_RE.search(normalized)
        ):
            return normalized
    return None


def _router_diagnostic(
    config: RuntimeConfig,
    adapter: AdapterConfig,
    diagnostic: Any,
) -> str:
    """Redact and bound router-controlled text before it can leave dispatch.

    Router JSON must remain unmodified for protocol parsing (including ``job_id``), but any
    stdout/stderr-derived text is untrusted diagnostic data.  Reuse the adapter's configured
    secret resolution and redactor so errors, claim details, summaries, and the viewer never
    retain configured secret values.
    """

    from .adapters import _redact, _secrets

    try:
        secret_values, _secret_env = _secrets(adapter, {}, config, os.environ)
    except Exception:
        # Failure to resolve a redaction secret must not turn a router error into a new failure.
        # _redact still removes named credential assignments and bounds the diagnostic.
        secret_values = {}
    return _redact(str(diagnostic), list(secret_values.values()))[:500]


def _completed_router_result(
    completed: subprocess.CompletedProcess[Any],
    *,
    config: RuntimeConfig,
    adapter: AdapterConfig,
    phase: str,
) -> Mapping[str, Any]:
    stdout = completed.stdout.decode("utf-8", errors="replace") \
        if isinstance(completed.stdout, bytes) else str(completed.stdout or "")
    stderr = completed.stderr.decode("utf-8", errors="replace") \
        if isinstance(completed.stderr, bytes) else str(completed.stderr or "")
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        if completed.returncode != 0:
            detail = next(
                (line for line in reversed(stderr.splitlines() + stdout.splitlines()) if line.strip()),
                "no diagnostic",
            )
            if phase == "launch" and _positive_prelaunch_mcp_flag_rejection(stdout, stderr):
                raise KnownDispatchFailure(_router_diagnostic(config, adapter, detail)) from exc
            raise _phase_uncertainty(
                phase,
                f"agent-router exited {completed.returncode} without validated JSON: "
                f"{_router_diagnostic(config, adapter, detail)}; launch state is unknown",
            ) from exc
        raise _phase_uncertainty(
            phase, "agent-router returned successful non-JSON output; launch state is unknown"
        ) from exc
    if not isinstance(value, Mapping):
        raise _phase_uncertainty(
            phase, "agent-router returned non-object output; launch state is unknown"
        )
    if phase == "launch" and _explicitly_not_launched(value):
        detail = value.get("error")
        if not isinstance(detail, str) or not detail:
            detail = next(
                (line for line in reversed(stderr.splitlines()) if line.strip()),
                "router reported launched=false",
            )
        raise KnownDispatchFailure(_router_diagnostic(config, adapter, detail))
    if completed.returncode != 0:
        detail = next(
            (line for line in reversed(stderr.splitlines() + stdout.splitlines()) if line.strip()),
            "no diagnostic",
        )
        raise _phase_uncertainty(
            phase,
            f"agent-router exited {completed.returncode}: "
            f"{_router_diagnostic(config, adapter, detail)}; launch state is unknown",
        )
    return value


def _subprocess_call(
    argv: Sequence[str],
    *,
    config: RuntimeConfig,
    adapter: AdapterConfig,
    phase: str,
    **_kwargs: Any,
) -> Mapping[str, Any]:
    from .adapters import ProcessOutputLimit, run_bounded_process

    try:
        completed = run_bounded_process(
            list(argv), timeout=adapter.timeout_seconds,
            max_output_bytes=adapter.max_output_bytes,
            env=_safe_environment(adapter),
        )
    except subprocess.TimeoutExpired as exc:
        raise _phase_uncertainty(
            phase,
            f"agent-router timed out after {adapter.timeout_seconds:g}s; launch state is unknown"
        ) from exc
    except ProcessOutputLimit as exc:
        raise _phase_uncertainty(
            phase, "agent-router output exceeded configured limit; launch state is unknown"
        ) from exc
    except OSError as exc:
        if phase == "classification":
            raise ClassificationFailure(f"agent-router classifier could not start: {exc}") from exc
        raise KnownDispatchFailure(f"agent-router could not start: {exc}") from exc
    return _completed_router_result(completed, config=config, adapter=adapter, phase=phase)


def _call_router(
    callback: Callable[..., Any] | None,
    argv: Sequence[str],
    config: RuntimeConfig,
    adapter: AdapterConfig,
    *,
    phase: str,
) -> Mapping[str, Any]:
    if phase not in {"classification", "launch"}:
        raise DispatchError(f"invalid router phase: {phase}")
    if adapter.kind != "agent-router":
        raise InvalidRoute(f"{phase} adapter {adapter.id} must be kind agent-router")
    if phase == "classification" and "--dry-run" not in argv:
        raise DispatchError("router classification must use --dry-run")
    if phase == "launch" and "--dry-run" in argv:
        raise DispatchError("router launch must not use --dry-run")
    try:
        if callback is None:
            result = _subprocess_call(argv, config=config, adapter=adapter, phase=phase)
        else:
            result = callback(
                list(argv), timeout_seconds=adapter.timeout_seconds,
                max_output_bytes=adapter.max_output_bytes, env_allowlist=adapter.env_allowlist,
                phase=phase,
            )
    except ClassificationFailure:
        raise
    except KnownDispatchFailure as exc:
        if phase == "classification":
            raise ClassificationFailure(str(exc)) from exc
        raise
    except AmbiguousDispatch as exc:
        if phase == "classification":
            raise ClassificationFailure(str(exc)) from exc
        raise
    except subprocess.TimeoutExpired as exc:
        raise _phase_uncertainty(
            phase, "agent-router timed out; launch state is unknown"
        ) from exc
    except OSError as exc:
        if phase == "classification":
            raise ClassificationFailure(f"agent-router classifier could not start: {exc}") from exc
        raise KnownDispatchFailure(f"agent-router could not start: {exc}") from exc
    except Exception as exc:
        from .adapters import ProcessOutputLimit

        if isinstance(exc, ProcessOutputLimit):
            raise _phase_uncertainty(
                phase, "agent-router output exceeded configured limit; launch state is unknown"
            ) from exc
        if phase == "classification":
            raise ClassificationFailure(
                f"agent-router dry-run classification failed before claim and is retry-safe: {exc}"
            ) from exc
        raise AmbiguousDispatch(
            "agent-router invocation failed after the launch attempt; launch state is unknown"
        ) from exc
    if isinstance(result, subprocess.CompletedProcess):
        return _completed_router_result(result, config=config, adapter=adapter, phase=phase)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise _phase_uncertainty(
                phase, "agent-router returned successful non-JSON output; launch state is unknown"
            ) from exc
    if not isinstance(result, Mapping):
        raise _phase_uncertainty(
            phase, "agent-router returned successful non-object output; launch state is unknown"
        )
    if phase == "launch" and _explicitly_not_launched(result):
        detail = result.get("error")
        raise KnownDispatchFailure(
            _router_diagnostic(config, adapter, detail)
            if isinstance(detail, str) and detail else "router reported launched=false"
        )
    return result


def _classified_provider(config: RuntimeConfig, raw: Mapping[str, Any]) -> ProviderConfig:
    candidate: Any = raw.get("provider") or raw.get("provider_id")
    if candidate is None and isinstance(raw.get("decision"), Mapping):
        candidate = raw["decision"].get("provider") or raw["decision"].get("provider_id")
    for provider in config.providers:
        if candidate in {provider.id, provider.dispatch.provider}:
            return provider
    raise InvalidRoute(f"agent-router classified unknown provider: {candidate!r}")


def _provider(config: RuntimeConfig, provider_id: str) -> ProviderConfig:
    try:
        return config.provider(provider_id)
    except ConfigError:
        for provider in config.providers:
            if provider.dispatch.provider == provider_id:
                return provider
        raise InvalidRoute(f"unknown provider: {provider_id}") from None


def _account_for(
    config: RuntimeConfig,
    provider: ProviderConfig,
    eligibility_key: str,
) -> AccountConfig | None:
    account_hint = eligibility_key.split("/", 1)[0]
    for account in config.accounts_for_provider(provider.id):
        if account.id == account_hint:
            return account
    accounts = config.accounts_for_provider(provider.id)
    return accounts[0] if accounts else None


def _activation(
    config: RuntimeConfig,
    account: AccountConfig | None,
    action: str,
    callback: Callable[[str, str], Any] | None,
) -> None:
    if account is None:
        return
    if callback is not None:
        callback(action, account.id)
        return
    if not account.activation_adapter_id:
        return
    adapter = config.adapter(account.activation_adapter_id)
    try:
        from .adapters import AdapterError, execute_adapter

        execute_adapter(
            adapter,
            {"action": action, "account_id": account.id, "provider_id": account.provider_id},
            config=config,
            expect_json=False,
        )
    except (AdapterError, ConfigError) as exc:
        raise DispatchError(f"account activation {action} failed: {exc}") from exc


def dispatch(
    config: RuntimeConfig,
    queue: QueueDB,
    *,
    task_id: str,
    eligibility_key: str,
    requested_provider: str,
    router_call: Callable[..., Any] | None = None,
    activation_call: Callable[[str, str], Any] | None = None,
) -> DispatchResult:
    """Classify if requested, claim, activate, and launch through agent-router once."""

    task = queue.task(task_id)
    if task is None:
        raise InvalidRoute(f"unknown task: {task_id}")
    if requested_provider == "auto":
        if not config.providers:
            raise InvalidRoute("auto classification requires at least one provider")
        classifier_adapter = config.adapter(config.providers[0].dispatch.adapter_id)
        classifier_argv = list(classifier_adapter.argv) + [
            "run", "--provider", "auto", "--dry-run", "--dir", task.cwd,
            "--name", f"Bonus classification: {task.id}", "--json", classification_prompt(task),
        ]
        classified = _call_router(
            router_call, classifier_argv, config, classifier_adapter, phase="classification",
        )
        provider = _classified_provider(config, classified)
    else:
        provider = _provider(config, requested_provider)
    if provider.id == "auto" or not provider_compatible(task, provider):
        raise InvalidRoute(f"provider {provider.id} is incompatible with task {task.id}")
    adapter = config.adapter(provider.dispatch.adapter_id)
    if adapter.kind != "agent-router":
        raise InvalidRoute(f"launch adapter {adapter.id} must be kind agent-router")

    account = _account_for(config, provider, eligibility_key)
    account_id = account.id if account else None
    if not queue.claim(
        task.id, eligibility_key, provider.id, account_id,
        provider_capabilities=provider.capabilities,
    ):
        raise AlreadyClaimed(f"task is no longer eligible: {task.id}")

    activated = False
    lease_managed = bool(
        account is not None
        and account.activation_adapter_id is not None
        and activation_call is None
    )

    def release_lease() -> None:
        _activation(config, account, "release", None)

    try:
        if lease_managed:
            assert account is not None
            try:
                queue.acquire_activation(
                    task.id,
                    eligibility_key,
                    provider.id,
                    account.id,
                    lambda: _activation(config, account, "activate", None),
                )
            except Exception as exc:
                incomplete = any(
                    lease.task_id == task.id and lease.eligibility_key == eligibility_key
                    for lease in queue.activation_leases(provider_id=provider.id)
                )
                if incomplete:
                    queue.mark_ambiguous(
                        task.id, eligibility_key,
                        detail=f"account activation requires reconciliation: {str(exc)[:500]}",
                    )
                    raise AmbiguousDispatch(
                        "account activation outcome is incomplete and requires reconciliation"
                    ) from exc
                raise ActivationUnavailable(str(exc)) from exc
            activated = True
        else:
            _activation(config, account, "activate", activation_call)
            activated = account is not None and activation_call is not None
        prompt = render_prompt(config, task, eligibility_key, provider.id, account_id)
        launch_argv = list(adapter.argv) + [
            "run", "--provider", provider.dispatch.provider, "--dir", task.cwd,
            "--name", f"Bonus: {task.id}",
        ]
        if task.model:
            launch_argv.extend(["--model", task.model])
        if _uses_claude_mcp_scoping(provider):
            mcp_path = _materialize_mcp_config(config, task, eligibility_key)
            if mcp_path is not None:
                launch_argv.extend(["--mcp-config", str(mcp_path), "--strict-mcp-config"])
        launch_argv.extend(["--json", prompt])
        response = _call_router(router_call, launch_argv, config, adapter, phase="launch")
        dispatch_data = response.get("dispatch")
        job_id = dispatch_data.get("job_id") if isinstance(dispatch_data, Mapping) else response.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise AmbiguousDispatch("agent-router response did not contain a job identity")
        try:
            queue.record(
                task.id, eligibility_key, status="dispatched", provider_id=provider.id,
                account_id=account_id, router_job_id=job_id,
            )
        except Exception as exc:
            raise AmbiguousDispatch("router launched but dispatch bookkeeping failed") from exc
        return DispatchResult(task.id, eligibility_key, provider.id, account_id, job_id, prompt)
    except AmbiguousDispatch as exc:
        claim = queue.claim_for(task.id, eligibility_key)
        if claim is not None and claim.state == "claimed":
            queue.mark_ambiguous(task.id, eligibility_key, detail=str(exc))
        # Runtime adapter leases remain durable after an ambiguous launch: the task stays
        # non-dispatchable and the active account cannot be switched out from under a job that
        # may exist. Injected test/operator callbacks retain their historical eager release.
        if activation_call is not None and activated:
            try:
                _activation(config, account, "release", activation_call)
            except Exception:
                pass
        raise
    except ActivationUnavailable:
        queue.release_claim(task.id, eligibility_key, reason="activation unavailable")
        raise
    except Exception as exc:
        if activation_call is not None and activated:
            try:
                _activation(config, account, "release", activation_call)
            except Exception as release_exc:
                queue.mark_ambiguous(
                    task.id, eligibility_key,
                    detail=f"known-not-launched activation cleanup failed: {str(release_exc)[:500]}",
                )
                raise AmbiguousDispatch(
                    "launch did not occur, but account activation cleanup requires reconciliation"
                ) from release_exc
        try:
            if lease_managed and activated:
                queue.record(
                    task.id, eligibility_key, status="failed", provider_id=provider.id,
                    account_id=account_id, summary=f"known not launched: {str(exc)[:500]}",
                    release_activation=release_lease,
                )
                # A positively non-launched attempt is retry-safe. Requeue removes the
                # temporary failed terminal event after its durable activation cleanup.
                queue.requeue(task.id, eligibility_key)
            else:
                queue.release_claim(task.id, eligibility_key, reason="known launch failure")
        except Exception as cleanup_exc:
            claim = queue.claim_for(task.id, eligibility_key)
            if claim is not None and claim.state == "claimed":
                queue.mark_ambiguous(
                    task.id, eligibility_key,
                    detail=f"known-not-launched cleanup requires reconciliation: {str(cleanup_exc)[:500]}",
                )
                raise AmbiguousDispatch(
                    "launch did not occur, but cleanup requires reconciliation"
                ) from cleanup_exc
            raise DispatchError(
                "launch did not occur, but retry eligibility could not be restored"
            ) from cleanup_exc
        raise KnownDispatchFailure(str(exc)) from exc


def dispatch_batch(
    config: RuntimeConfig,
    queue: QueueDB,
    *,
    task_ids: Iterable[str],
    eligibility_key: str,
    provider_id: str,
    router_call: Callable[..., Any] | None = None,
    activation_call: Callable[[str, str], Any] | None = None,
) -> tuple[DispatchResult, ...]:
    results: list[DispatchResult] = []
    for task_id in task_ids:
        try:
            results.append(dispatch(
                config, queue, task_id=task_id, eligibility_key=eligibility_key,
                requested_provider=provider_id, router_call=router_call,
                activation_call=activation_call,
            ))
        except (AlreadyClaimed, KnownDispatchFailure):
            continue
    return tuple(results)
