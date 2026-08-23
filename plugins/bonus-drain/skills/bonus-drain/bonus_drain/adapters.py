"""Safe execution of configured usage, activation, and agent-router adapters."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from . import config as config_module
from .config import AdapterConfig, RuntimeConfig


class AdapterError(RuntimeError):
    """Adapter execution failed with a redacted diagnostic."""


_SAFE_BASE_ENV = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
}
_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|token|secret|password|api[_-]?key)\s*[=:]\s*([^\s,;]+)"
)


def _redact(text: str, secret_values: list[str]) -> str:
    result = text.replace("\r", " ").replace("\n", " ")
    for value in sorted((item for item in secret_values if item), key=len, reverse=True):
        result = result.replace(value, "[REDACTED]")
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)[:1000]


def _secrets(
    adapter: AdapterConfig,
    variables: Mapping[str, str],
    config: RuntimeConfig,
    environ: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    refs = {ref.id: ref for ref in config.secret_refs}
    bindings = dict(adapter.secret_refs)
    account_id = variables.get("account_id")
    if account_id:
        try:
            account = config.account(account_id)
        except config_module.ConfigError:
            account = None
        if account is not None:
            for name, ref_id in account.secret_refs.items():
                bindings.setdefault(f"BONUS_SECRET_{name.upper()}", ref_id)
    values: dict[str, str] = {}
    exported: dict[str, str] = {}
    for name, ref_id in bindings.items():
        ref = refs.get(ref_id)
        if ref is None:
            raise AdapterError(f"adapter {adapter.id} references unknown secret {ref_id}")
        try:
            value = config_module.resolve_secret(ref, environ=environ)
        except config_module.ConfigError as exc:
            raise AdapterError(str(exc)) from exc
        values[name] = value
        env_name = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f"BONUS_SECRET_{name.upper()}"
        exported[env_name] = value
    return values, exported


def execute_adapter(
    adapter: AdapterConfig,
    variables: Mapping[str, str],
    *,
    config: RuntimeConfig,
    environ: Mapping[str, str] | None = None,
    expect_json: bool = True,
) -> Any:
    """Execute a validated adapter without a shell and return its JSON value.

    Only a small runtime environment, explicitly allowlisted names, and resolved secret-ref
    bindings are passed.  A temporary secret file exists only when argv requests
    ``{secret_file}``; it is mode 0600 and unlinked in all outcomes.
    """

    source_env = os.environ if environ is None else environ
    secret_values, secret_env = _secrets(adapter, variables, config, source_env)
    child_env = {
        name: source_env[name]
        for name in (_SAFE_BASE_ENV | set(adapter.env_allowlist))
        if name in source_env
    }
    child_env.update(secret_env)
    temporary_path: Path | None = None
    try:
        substitutions = {key: str(value) for key, value in variables.items()}
        if any("{secret_file}" in token for token in adapter.argv):
            descriptor, raw_path = tempfile.mkstemp(prefix="bonus-drain-secrets.")
            temporary_path = Path(raw_path)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(secret_values, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            substitutions["secret_file"] = str(temporary_path)
        else:
            substitutions["secret_file"] = ""

        argv: list[str] = []
        for raw in adapter.argv:
            value = raw
            for name, replacement in substitutions.items():
                value = value.replace("{" + name + "}", replacement)
            if "{" in value or "}" in value:
                raise AdapterError(f"adapter {adapter.id} has an unresolved argv placeholder")
            argv.append(value)
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=adapter.timeout_seconds,
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterError(f"adapter {adapter.id} exceeded {adapter.timeout_seconds:g}s timeout") from exc
        except OSError as exc:
            raise AdapterError(f"adapter {adapter.id} could not execute: {exc}") from exc

        if len(completed.stdout) > adapter.max_output_bytes or len(completed.stderr) > adapter.max_output_bytes:
            raise AdapterError(f"adapter {adapter.id} output exceeded {adapter.max_output_bytes} bytes")
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        secrets = list(secret_values.values())
        if completed.returncode != 0:
            detail = next(
                (line for line in reversed(stderr.splitlines() + stdout.splitlines()) if line.strip()),
                "no diagnostic",
            )
            raise AdapterError(
                f"adapter {adapter.id} exited {completed.returncode}: {_redact(detail, secrets)}"
            )
        if not expect_json:
            return {"ok": True, "stdout": _redact(stdout.strip(), secrets)}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"adapter {adapter.id} returned invalid JSON") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


run_adapter = execute_adapter
execute = execute_adapter
