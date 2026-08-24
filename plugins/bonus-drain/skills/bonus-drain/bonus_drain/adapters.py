"""Safe execution of configured usage, activation, and agent-router adapters."""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from . import config as config_module
from .config import AdapterConfig, RuntimeConfig


class AdapterError(RuntimeError):
    """Adapter execution failed with a redacted diagnostic."""


class ProcessOutputLimit(RuntimeError):
    """A child exceeded its configured output budget and was terminated."""


def run_bounded_process(
    argv: list[str], *, timeout: float, max_output_bytes: int,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[bytes]:
    """Run one process group with streaming output caps and deterministic teardown."""

    process = subprocess.Popen(
        argv, shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(env), start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout

    def terminate_group() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_group()
                raise subprocess.TimeoutExpired(argv, timeout)
            events = selector.select(remaining)
            if not events:
                continue
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = buffers[key.data]
                buffer.extend(chunk)
                if len(buffer) > max_output_bytes:
                    terminate_group()
                    raise ProcessOutputLimit(
                        f"process output exceeded {max_output_bytes} bytes"
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            terminate_group()
            raise subprocess.TimeoutExpired(argv, timeout)
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            terminate_group()
            raise subprocess.TimeoutExpired(argv, timeout) from None
        return subprocess.CompletedProcess(
            argv, returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"]),
        )
    finally:
        selector.close()
        if process.poll() is None:
            terminate_group()
        process.stdout.close()
        process.stderr.close()


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
    if account_id and adapter.kind in {"usage", "reset"}:
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
            completed = run_bounded_process(
                argv, timeout=adapter.timeout_seconds,
                max_output_bytes=adapter.max_output_bytes, env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterError(f"adapter {adapter.id} exceeded {adapter.timeout_seconds:g}s timeout") from exc
        except ProcessOutputLimit as exc:
            raise AdapterError(f"adapter {adapter.id} output exceeded {adapter.max_output_bytes} bytes") from exc
        except OSError as exc:
            raise AdapterError(f"adapter {adapter.id} could not execute: {exc}") from exc
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
