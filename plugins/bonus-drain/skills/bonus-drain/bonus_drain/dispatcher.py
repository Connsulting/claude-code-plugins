"""Claimed, router-only dispatch with concrete provider bookkeeping."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from hashlib import sha256
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import AccountConfig, AdapterConfig, ConfigError, ProviderConfig, RuntimeConfig
from .db import (
    COMPLETION_MECHANISMS,
    LEGACY_EXCLUSIVE_CAPABILITY,
    REASON_CODES,
    QueueDB,
    QueueError,
    Task,
    canonical_model,
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

    def __init__(self, message: str, *, known_not_switched: bool = False):
        super().__init__(message)
        self.known_not_switched = known_not_switched


_PROVEN_UNSWITCHED_ACTIVATION = "requested account did not become active"


def _trusted_unswitched_activation(exc: Exception, adapter_id: str) -> bool:
    if isinstance(exc, ActivationUnavailable) and exc.known_not_switched:
        return True
    expected = (
        f"account activation activate failed: adapter {adapter_id} exited 1: "
        f"bonus-drain-account-activation: {_PROVEN_UNSWITCHED_ACTIVATION}"
    )
    return str(exc) == expected


_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MCP_FILE_LIMIT = 1_048_576
_OUTCOME_FILE_LIMIT = 65_536
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
_CODEX_APP_SERVER_MISSING_RE = re.compile(
    r"could not run [`']codex app-server daemon start[`']:\s*No such file or directory",
    re.IGNORECASE,
)


class _DuplicateJSONKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJSONKey(key)
        value[key] = item
    return value


def _strict_json_loads(raw: str | bytes) -> Any:
    return json.loads(raw, object_pairs_hook=_unique_json_object)


@dataclass(frozen=True)
class DispatchResult:
    task_id: str
    eligibility_key: str
    provider_id: str
    account_id: str | None
    job_id: str
    prompt: str
    factory_run_id: str | None = None
    attempt_id: str | None = None
    dependency_base: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Factory telemetry (the /implement runs ledger). The dispatcher creates the runs row at
# launch so a driver that never emits telemetry still leaves a joinable row. This is a
# measurement side effect only: it never blocks, fails, or reorders a dispatch.
FACTORY_RUN_ID_LINE = "FACTORY_RUN_ID"
FACTORY_TELEMETRY_ENV = "BONUS_DRAIN_FACTORY_TELEMETRY"
FACTORY_TELEMETRY_DEFAULT = Path.home() / ".claude" / "skills" / "implement" / "factory-telemetry.py"
FACTORY_TELEMETRY_TIMEOUT_SECONDS = 30.0
# The writer stamps runs.session_id from these when the payload has none; the dispatcher's
# own session must never be recorded as the driver's.
FACTORY_SESSION_ENV_KEYS = (
    "CLAUDE_CODE_SESSION_ID",
    "GROK_SESSION_ID",
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
)


def new_factory_run_id(task_id: str, attempt_id: str) -> str:
    """Derive the runs row id from the attempt so the terminal record can find it again."""
    return f"drain-{task_id}-{attempt_id.replace('-', '')[:12]}"


def factory_telemetry_script() -> Path | None:
    raw = os.environ.get(FACTORY_TELEMETRY_ENV)
    if raw is not None and not raw.strip():
        return None
    path = Path(raw).expanduser() if raw else FACTORY_TELEMETRY_DEFAULT
    return path if path.is_file() else None


def factory_repo_name(cwd: str) -> str:
    """The driver records the repo basename; guess it from the task cwd.

    A cwd that is the parent of several repos (monorepo-of-repos) yields the parent's
    name. The driver's own run event replaces the guess, so this only has to be a
    reasonable label for rows the driver never touches.
    """
    path = Path(cwd).expanduser()
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        top = completed.stdout.strip()
        if completed.returncode == 0 and top:
            return Path(top).name
    except (OSError, subprocess.SubprocessError):
        pass
    return path.name or str(path)


def factory_run_payload(
    task: Task,
    provider: ProviderConfig,
    router_decision_id: Any,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "factory_version": "v1",
        "repo": factory_repo_name(task.cwd),
        "tier": "quick",
        "launch_mode": "background",
        "status": "dispatched",
        "drain_task_id": task.id,
    }
    if isinstance(router_decision_id, int) and not isinstance(router_decision_id, bool):
        payload["router_decision_id"] = router_decision_id
    if attempt_id is not None:
        payload["drain_attempt_id"] = attempt_id
    return payload


def record_factory_run(
    task: Task,
    provider: ProviderConfig,
    run_id: str,
    router_decision_id: Any,
    telemetry_call: Callable[[list[str], dict[str, Any]], Any] | None = None,
    attempt_id: str | None = None,
) -> bool:
    """Create the placeholder runs row for a launched /implement task.

    Returns True when the row was written. Every failure is swallowed and reported on
    stderr: the dispatch has already happened and its bookkeeping is authoritative.
    """
    try:
        payload = factory_run_payload(task, provider, router_decision_id, attempt_id)
        if telemetry_call is not None:
            telemetry_call(["record", "run", "--run-id", run_id], payload)
            return True
        script = factory_telemetry_script()
        if script is None:
            return False
        env = {key: value for key, value in os.environ.items() if key not in FACTORY_SESSION_ENV_KEYS}
        with tempfile.NamedTemporaryFile(
            "w", prefix="bonus-drain-factory-run-", suffix=".json", delete=False,
            encoding="utf-8",
        ) as handle:
            json.dump(payload, handle, sort_keys=True)
            payload_path = Path(handle.name)
        try:
            completed = subprocess.run(
                [
                    sys.executable, str(script), "record", "run",
                    "--run-id", run_id, "--json-file", str(payload_path),
                ],
                capture_output=True, text=True, env=env,
                timeout=FACTORY_TELEMETRY_TIMEOUT_SECONDS, check=False,
            )
        finally:
            try:
                payload_path.unlink()
            except OSError:
                pass
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:500]
            print(
                f"bonus-drain: factory run row for {task.id} not written: {detail}",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail a dispatch
        print(
            f"bonus-drain: factory run row for {task.id} not written: {str(exc)[:500]}",
            file=sys.stderr,
        )
        return False


def replay_factory_terminal(
    queue: QueueDB,
    task: Task,
    attempt_id: str,
) -> bool:
    """Replay a terminal event that committed before its placeholder was written."""
    try:
        terminal = next(
            (
                event for event in queue.runs(task_id=task.id)
                if event.attempt_id == attempt_id
                and event.status in {"done", "skipped", "failed"}
            ),
            None,
        )
        if terminal is None:
            return False
        from .factory_terminal import record_factory_terminal

        return record_factory_terminal(
            task.id,
            terminal.attempt_id,
            terminal.status,
            terminal.ts,
            terminal.summary,
            task.cwd,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail a dispatch
        print(
            f"bonus-drain: factory terminal replay for {task.id} failed: {str(exc)[:500]}",
            file=sys.stderr,
        )
        return False


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
    attempt_id: str | None = None,
    outcome_path: Path | None = None,
) -> str:
    command = list(config.record_command)
    command.extend(
        [
            "--database", str(config.database),
            "--task", task.id,
            "--kind", task.kind,
            "--eligibility-key", eligibility_key,
            "--cycle", str(cycle_from_key(eligibility_key)),
            "--status", "done|skipped|failed|awaiting_human",
            "--provider-id", provider_id,
        ]
    )
    if account_id:
        command.extend(["--account-id", account_id])
    if attempt_id:
        command.extend(["--attempt-id", attempt_id])
    if outcome_path is not None:
        command.extend(["--outcome-file", str(outcome_path)])
    command.extend(["--summary", "<one line>"])
    return shlex.join(command)


def _outcome_contract_line() -> str:
    schema = {
        "reason": {
            "code": {"allowed": sorted(REASON_CODES)},
            "detail": "<non-empty bounded detail>",
            "signature": "<stable non-secret signature>",
        },
        "completion": {
            "verified": True,
            "mechanism": {"allowed": sorted(COMPLETION_MECHANISMS)},
            "evidence": ["<non-empty verification reference>"],
        },
        "repository": {
            "remote": "<exact canonical remote URL>",
            "target_ref": "refs/heads/<exact target branch>",
            "branch_ref": "refs/heads/<exact result branch>",
            "integration_state": {"allowed": ["merged", "unmerged"]},
        },
    }
    return "OUTCOME_SCHEMA=" + json.dumps(schema, sort_keys=True, separators=(",", ":"))


def _continue_progress_line(
    config: RuntimeConfig,
    task: Task,
    attempt_id: str,
) -> str:
    executable = str(Path(__file__).resolve().parents[1] / "bin" / "bonus-drain")
    command = [
        executable,
        "continue-progress",
        "--database", str(config.database),
        "--task", task.id,
        "--from-attempt", attempt_id,
        "--json",
    ]
    if config.source_path is not None:
        command[2:2] = ["--config", str(config.source_path)]
    return shlex.join(command)


def _recover_complete_line(
    config: RuntimeConfig,
    task: Task,
    attempt_id: str,
    outcome_path: Path,
) -> str:
    executable = str(Path(__file__).resolve().parents[1] / "bin" / "bonus-drain")
    command = [
        executable,
        "recover-complete",
        "--database", str(config.database),
        "--task", task.id,
        "--from-attempt", attempt_id,
        "--outcome-file", str(outcome_path),
        "--summary", "<one line>",
        "--json",
    ]
    if config.source_path is not None:
        command[2:2] = ["--config", str(config.source_path)]
    return shlex.join(command)


def _pr_policy(config: RuntimeConfig, task: Task) -> str:
    from .goals import coordinator_contract
    goal = coordinator_contract(QueueDB(config.database), task)
    if goal and goal['merge_policy'] == 'merge':
        return ('This goal coordinator may merge verified PRs within the recorded goal authority. '
                'Check exact heads, required checks, reviews, dependencies, and cleanup first. '
                'This grant does not extend to task drivers or unrelated repositories and deployments.')
    cwd = Path(task.cwd).expanduser().resolve(strict=False)
    for exception in config.pr_exceptions:
        root = Path(str(exception["path"])).expanduser().resolve(strict=False)
        try:
            cwd.relative_to(root)
        except ValueError:
            continue
        if bool(exception.get("allow_pr", exception.get("allow_push", False))):
            return (
                "This configured repository permits a branch push and pull request. "
                "Merge only when the task contract explicitly grants merge authority into a named "
                "epic/* branch, and only into that branch after all PR checks pass. "
                "Otherwise, do not merge."
            )
        if bool(exception.get("allow_push", False)):
            return "This configured repository permits pushing the committed branch; do not open or merge a pull request."
    return "Produce a branch and committed artifact only; do not push, publish, merge, or delete unrelated files."


# Checkout cleanliness, default ports, and local dependencies are setup the worker
# performs. They are not skip gates. Genuine authority, prerequisite, provider,
# contract, ownership, and validation failures still skip immediately.
PRECONDITION_EXECUTION_RULE = (
    "Run the precondition first. "
    "A dirty or wrong-branch shared checkout, untracked worktree directories, "
    "occupied default ports, a shared baseline lock, or a missing local dependency "
    "is setup you are authorized to perform, not an unmet precondition. "
    "Create your own clean worktree from the named remote base, bind private ports, "
    "and install dependencies. Do not clean, reset, or reuse another owner's checkout. "
    "Record skipped immediately only when the work is already complete, or when a "
    "genuine precondition is false: missing authority, a missing prerequisite you "
    "cannot create, an unavailable provider or required service, a frozen contract, "
    "another owner already editing the same paths, or a validation gate that rejects "
    "the change for a reason setup cannot remove."
)


def render_prompt(
    config: RuntimeConfig,
    task: Task,
    eligibility_key: str,
    provider_id: str,
    account_id: str | None,
    factory_run_id: str | None = None,
    *,
    attempt: Any | None = None,
    outcome_path: Path | None = None,
    dependency_base: Mapping[str, Any] | None = None,
    recovery: Mapping[str, Any] | None = None,
) -> str:
    """Render one task and the stable terminal-record contract.

    ``factory_run_id`` is appended as an exact ``FACTORY_RUN_ID=<id>`` line after
    ``BACKGROUND_RUN=1`` on /implement tasks so the driver's telemetry upserts onto the
    runs row the dispatcher already created.
    """

    sections = [f"Goal: {task.goal}"]
    if dependency_base is not None:
        encoded_base = json.dumps(
            dict(dependency_base), sort_keys=True, separators=(",", ":"),
        )
        sections.extend([
            f"DEPENDENCY_BASE={encoded_base}",
            (
                "Fetch DEPENDENCY_BASE.branch_ref from the task repository, confirm it exists, "
                "and start the isolated worktree from the current tip of that fetched branch. This branch "
                "selection overrides the default starting branch. If it is unavailable, stop as a setup "
                "failure with reason.code=verification_needed and a detail and signature naming "
                "dependency_ref_unavailable. Do not substitute another branch. This selection "
                "grants no merge authority."
            ),
        ])
    if attempt is not None:
        attempt_context = {
            "attempt_id": attempt.id,
            "mode": attempt.mode,
            "ordinal": attempt.ordinal,
            "origin": attempt.origin,
        }
        if recovery:
            attempt_context["recovery"] = dict(recovery)
        sections.append(
            "ATTEMPT_CONTEXT="
            + json.dumps(attempt_context, sort_keys=True, separators=(",", ":"))
        )
    for label, value in (
        ("Source thread or plan", task.source_ref),
        ("Work group", task.work_group),
        ("Prerequisite task IDs", ", ".join(task.depends_on)),
        ("Context", task.context),
        ("Constraints", task.constraints),
        ("Precondition", task.precondition),
        ("Done when", task.done_when),
    ):
        if value:
            sections.append(f"{label}:\n{value}")
    from .goals import coordinator_contract
    goal = coordinator_contract(QueueDB(config.database), task)
    if goal:
        # Bind every goal mutation to the same queue as the terminal command. A custom
        # record adapter need not itself support the goal verbs; the shipped CLI does.
        executable = str(Path(__file__).resolve().parents[1] / 'bin' / 'bonus-drain')
        common = ['--database', str(config.database)]
        if config.source_path is not None:
            common.extend(['--config', str(config.source_path)])
        commands = []
        for label, action in (('read', 'show'), ('decision', 'advance'), ('operation', 'operation')):
            argv = [executable, 'goal', action, goal['id'], *common, '--json']
            if action != 'show':
                argv.extend(['--turn', task.id, '--file', '<private-json-file>'])
            commands.append(f'Goal {label} command: ' + shlex.join(argv))
        sections.append('Use these exact runtime/config/database bindings for this goal; '
                        'replace only the file placeholder when needed.\n' + '\n'.join(commands))
    if task.kind == "oneoff":
        contract = [
            "--- ASYNC TASK EXECUTION CONTRACT ---",
            "Execute this authorized asynchronous task within its stated contract.",
            _pr_policy(config, task),
            PRECONDITION_EXECUTION_RULE,
            "On bounded ambiguity, choose the reasonable default, note it, and continue without asking for input.",
        ]
    else:
        contract = [
            "--- RECURRING ASYNC JOB EXECUTION CONTRACT ---",
            "Run this vetted recurring operation with its configured mandate unchanged.",
            PRECONDITION_EXECUTION_RULE,
            "On bounded ambiguity, choose the reasonable default, note it, and continue without asking for input.",
        ]
    contract.extend(
        [
            "Never leave this background run blocked, waiting for input, or otherwise non-terminal.",
            "Opening or updating a pull request is not done. Normal PR work is done only after all PR checks pass for the current head. Epic Forge work, or a task whose contract requires an epic merge, is done only after all PR checks pass and the PR is confirmed merged into the exact authorized epic branch. Follow /implement's check watcher and epic merge procedure when applicable. Pending or failing checks, a running check watcher, and an unmerged epic PR must never be recorded as done. Keep working or waiting while progress remains possible. Record done with completion.mechanism=artifact and evidence of the PR URL, passing checks, and the required merge. Pending human review alone does not block normal PR completion once checks pass.",
            "Record awaiting_human only when you finished everything you can and the remaining step needs Brian personally: hands-on testing only he can do (for example a real human review comment or a live Slack check) or a decision or approval (for example approving a CI or automation diff before commit, or choosing between conflicting acceptance criteria). Its reason.detail must name exactly what Brian must do.",
            "If the work itself cannot be completed, record failed with the blocker before exiting; do not request input or set a blocked status.",
            "Failed, skipped, or awaiting_human results require the structured reason and must not claim verified completion.",
            f"The concrete provider for this accounted run is {provider_id}.",
            "When finished, record exactly one terminal event with this command (replace only the status and summary placeholders):",
            f"  {_record_line(config, task, eligibility_key, provider_id, account_id, attempt.id if attempt is not None else None, outcome_path)}",
            "Do not replace the task id, eligibility key, or attempt id and do not stop an idle background session.",
        ]
    )
    if account_id and config.account(account_id).activation_scope == "launch":
        contract.insert(
            -3,
            f"The recorded account {account_id} is the launch account, not a run-long account pin; later credential rotation is permitted.",
        )
    if attempt is not None and outcome_path is not None:
        contract.extend([
            (
                "Before the terminal command, write one JSON outcome object to its exact private "
                "outcome file. The parseable line below is a field schema, not a literal result: "
                "choose one allowed reason code and, when done, one allowed completion mechanism."
            ),
            _outcome_contract_line(),
            (
                "Every terminal result requires reason.code, non-empty reason.detail, and a stable, "
                "non-secret reason.signature. Status done requires reason.code=done_when_verified, "
                "completion.verified=true, one supported completion.mechanism, and at least one "
                "non-empty completion.evidence reference."
            ),
            (
                "If this task produces a branch or commit that a dependent task must use, include "
                "repository with remote, target_ref, branch_ref, and integration_state from "
                "the task's observed Git branch metadata. Record whether the branch is merged "
                "or unmerged. This handoff grants no push or merge authority."
            ),
        ])
    if attempt is not None and outcome_path is not None and task.kind == "oneoff":
        contract.extend([
            (
                "If a later user message in this same thread continues the work after this "
                "attempt recorded failed, skipped, or awaiting_human, keep this task and do not "
                "launch another worker. Before more work, run the exact continue-progress command "
                "below. It marks this same router job in progress and does not start a dispatch. "
                "If it refuses, stop without changing the original attempt and do not launch a replacement."
            ),
            f"  {_continue_progress_line(config, task, attempt.id)}",
            (
                "When that continued work finishes, record its terminal result with the record "
                "command in this prompt, replacing only the attempt id with the attempt_id "
                "continue-progress printed. Do not call recover-complete after continue-progress succeeds."
            ),
            (
                "If continue-progress was not opened and the continued work already meets done-when, "
                "write verified evidence to the same private outcome path and invoke the exact "
                "recover-complete command below before replying. If the work still fails, retain "
                "the original terminal evidence and bounded recovery state."
            ),
            (
                "Terminal processing may remove the staging file. Before recover-complete, "
                "recreate only that exact path as a regular non-symlink file, keep its parent "
                "directory at mode 0700, set the file to mode 0600, and write one JSON object "
                "of at most 65536 bytes. Preserve the path; a default 0644 write is rejected."
            ),
            f"  {_recover_complete_line(config, task, attempt.id, outcome_path)}",
        ])
    prompt = "\n\n".join(sections + ["\n".join(contract)])
    if task.use_implement:
        rendered = f"/implement {prompt}\nBACKGROUND_RUN=1"
        if factory_run_id:
            rendered += f"\n{FACTORY_RUN_ID_LINE}={factory_run_id}"
        return rendered
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


def _private_diagnostic(config: RuntimeConfig, diagnostic: Any) -> str:
    """Use the router diagnostic redactor for private-file boundary errors."""

    if config.adapters:
        return _router_diagnostic(config, config.adapters[0], diagnostic)
    from .adapters import _redact

    return _redact(str(diagnostic), [])[:500]


def _outcome_directory(config: RuntimeConfig) -> Path:
    directory = config.state_dir / "outcomes"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = directory.lstat()
    except OSError as exc:
        raise DispatchError(
            _private_diagnostic(config, "outcome state directory is unavailable")
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DispatchError(
            _private_diagnostic(config, "outcome state directory must be owned with mode 0700")
        )
    return directory


def materialize_outcome_file(config: RuntimeConfig, attempt_id: str) -> Path:
    """Allocate an attempt-scoped private staging file outside the task checkout."""

    directory = _outcome_directory(config)
    digest = sha256(attempt_id.encode("utf-8")).hexdigest()[:24]
    descriptor = -1
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f"attempt-{digest}-", suffix=".json", dir=directory,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write("{}\n")
            stream.flush()
            os.fsync(stream.fileno())
        return Path(raw_path)
    except OSError as exc:
        raise DispatchError(
            _private_diagnostic(config, "private outcome file could not be allocated")
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_outcome_file(config: RuntimeConfig, path: Path) -> Mapping[str, Any]:
    """Read one owned, regular, bounded JSON object without following symlinks."""

    directory = _outcome_directory(config)
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        if candidate.parent.resolve(strict=True) != directory.resolve(strict=True):
            raise ValueError("outcome file is not a direct child of the state directory")
    except (OSError, ValueError) as exc:
        raise DispatchError(
            _private_diagnostic(config, "outcome file is outside the private state directory")
        ) from exc

    descriptor = -1
    try:
        before = candidate.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise DispatchError("outcome file must be a regular file, not a symlink")
        if before.st_uid != os.getuid():
            raise DispatchError("outcome file must be owned by the current user")
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise DispatchError("outcome file must have mode 0600")
        if before.st_size > _OUTCOME_FILE_LIMIT:
            raise DispatchError(f"outcome file exceeds {_OUTCOME_FILE_LIMIT} bytes")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise DispatchError("outcome file identity changed while opening")
        payload = os.read(descriptor, _OUTCOME_FILE_LIMIT + 1)
        if len(payload) > _OUTCOME_FILE_LIMIT:
            raise DispatchError(f"outcome file exceeds {_OUTCOME_FILE_LIMIT} bytes")
        value = _strict_json_loads(payload.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise DispatchError("outcome file must contain one JSON object")
        return value
    except DispatchError as exc:
        raise DispatchError(_private_diagnostic(config, exc)) from exc
    except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateJSONKey) as exc:
        raise DispatchError(
            _private_diagnostic(config, "outcome file is not readable JSON")
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def remove_outcome_file(path: Path) -> None:
    """Remove only the already-validated attempt staging path."""

    try:
        path.unlink()
    except FileNotFoundError:
        pass


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


def _mcp_servers(
    value: Mapping[str, Any],
    label: str,
    selected_names: set[str] | None = None,
) -> dict[str, Mapping[str, Any]]:
    raw = value.get("mcpServers", {})
    if not isinstance(raw, Mapping) or len(raw) > _MCP_SERVER_LIMIT:
        raise DispatchError(f"{label}.mcpServers must be a bounded object")
    result: dict[str, Mapping[str, Any]] = {}
    for raw_name, definition in raw.items():
        name = str(raw_name)
        if not _MCP_NAME_RE.fullmatch(name) or not isinstance(definition, Mapping):
            raise DispatchError(f"{label} contains an invalid MCP server")
        if selected_names is not None and name not in selected_names:
            continue
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
    selected_names = set(names)

    merged: dict[str, Mapping[str, Any]] = {}
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    global_path = home / ".claude.json"
    global_value: Mapping[str, Any] = {}
    if global_path.is_file():
        global_value = _read_mcp_json(global_path, "user MCP config")
        merged.update(_mcp_servers(global_value, "user MCP config", selected_names))

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
            merged.update(_mcp_servers(
                _read_mcp_json(path, "project MCP config"),
                "project MCP config",
                selected_names,
            ))
    projects = global_value.get("projects", {})
    if projects is not None and not isinstance(projects, Mapping):
        raise DispatchError("user MCP config projects must be an object")
    if isinstance(projects, Mapping):
        for directory in reversed(ancestors):
            project = projects.get(str(directory), {})
            if project:
                if not isinstance(project, Mapping):
                    raise DispatchError("user MCP project entry must be an object")
                merged.update(_mcp_servers(project, "user MCP project entry", selected_names))

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


def _positive_prelaunch_codex_executable_rejection(stdout: str, stderr: str) -> str | None:
    """Return the exact router diagnostic proving Codex could not be executed."""

    for line in reversed((stderr + "\n" + stdout).splitlines()):
        normalized = line.strip()
        if _CODEX_APP_SERVER_MISSING_RE.search(normalized):
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
        value = _strict_json_loads(stdout)
    except (json.JSONDecodeError, _DuplicateJSONKey) as exc:
        if completed.returncode != 0:
            detail = next(
                (line for line in reversed(stderr.splitlines() + stdout.splitlines()) if line.strip()),
                "no diagnostic",
            )
            prelaunch_rejection = (
                _positive_prelaunch_mcp_flag_rejection(stdout, stderr)
                or _positive_prelaunch_codex_executable_rejection(stdout, stderr)
            )
            if phase == "launch" and prelaunch_rejection is not None:
                raise KnownDispatchFailure(
                    _router_diagnostic(config, adapter, prelaunch_rejection)
                ) from exc
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
            result = _strict_json_loads(result)
        except (json.JSONDecodeError, _DuplicateJSONKey) as exc:
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
    leased_account_ids: tuple[str, ...] = (),
) -> AccountConfig | None:
    account_hint = eligibility_key.split("/", 1)[0]
    accounts = config.accounts_for_provider(provider.id)
    for account in accounts:
        if account.id == account_hint:
            return account
    if len(accounts) > 1:
        return None
    leased = set(leased_account_ids)
    configured_ids = {account.id for account in accounts}
    if leased and not leased <= configured_ids:
        raise InvalidRoute(
            f"provider {provider.id} activation lease names an unknown configured account"
        )
    if len(leased) > 1:
        raise InvalidRoute(
            f"provider {provider.id} has multiple conflicting activation leases"
        )
    for account in accounts:
        if account.id in leased:
            return account
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
    telemetry_call: Callable[[list[str], dict[str, Any]], Any] | None = None,
    trigger: str = "manual",
    now_epoch: int | None = None,
) -> DispatchResult:
    """Classify if requested, claim, activate, and launch through agent-router once.

    ``telemetry_call`` is a test seam for the factory run row; runtime callers leave it
    unset so the row is written through ``factory-telemetry.py``.
    """

    if config.viewer.get("preview") is True:
        raise InvalidRoute("Preview: execution is disabled")
    task = queue.task(task_id)
    if task is None:
        raise InvalidRoute(f"unknown task: {task_id}")
    if trigger not in {"manual", "bonus", "scheduled"}:
        raise InvalidRoute("invalid run trigger")
    readiness = queue.readiness(task_id, now_epoch=now_epoch)
    if not readiness["ready"]:
        raise AlreadyClaimed(readiness["reason"])
    try:
        selected_dependency_base = queue.dependency_base(task_id)
    except QueueError as exc:
        raise DispatchError(str(exc)) from exc
    if requested_provider == "auto":
        if not config.providers:
            raise InvalidRoute("auto classification requires at least one provider")
        classifier_adapter = config.adapter(config.providers[0].dispatch.adapter_id)
        classifier_argv = list(classifier_adapter.argv) + [
            "run", "--provider", "auto", "--dry-run", "--dir", task.cwd,
            "--name", f"Classify: {task.title}", "--json", classification_prompt(task),
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

    account = _account_for(
        config,
        provider,
        eligibility_key,
        tuple(
            lease.account_id
            for lease in queue.activation_leases(provider_id=provider.id)
        ),
    )
    provider_accounts = config.accounts_for_provider(provider.id)
    if len(provider_accounts) > 1:
        from .kick import resolve_active_account_id

        active_account_id = resolve_active_account_id(config, queue, provider.id)
        if account is None:
            account = config.account(active_account_id)
    elif account is None and provider_accounts:
        account = provider_accounts[0]
    account_id = account.id if account else None
    attempt = queue.claim(
        task.id, eligibility_key, provider.id, account_id,
        provider_capabilities=provider.capabilities, automatic=trigger == "bonus", expected_task=task,
        now_epoch=now_epoch,
    )
    if attempt is None:
        raise AlreadyClaimed(f"task is no longer eligible: {task.id}")

    activated = False
    outcome_path: Path | None = None
    lease_managed = bool(
        account is not None
        and account.activation_adapter_id is not None
        and activation_call is None
    )

    def release_lease() -> None:
        _activation(config, account, "release", None)

    def abort_known_nonlaunch(reason: str) -> None:
        release_activation: Callable[[], None] | None = None
        if activated:
            if lease_managed:
                release_activation = release_lease
            elif activation_call is not None:
                # Injected callbacks have no durable activation lease for QueueDB
                # to inspect. Prove their cleanup before releasing the exact claim;
                # a failed cleanup retains ambiguous ownership.
                try:
                    _activation(config, account, "release", activation_call)
                except Exception as cleanup_exc:
                    queue.mark_attempt_ambiguous(
                        task.id,
                        eligibility_key,
                        attempt.id,
                        "known-not-launched activation cleanup requires reconciliation: "
                        f"{str(cleanup_exc)[:500]}",
                    )
                    raise AmbiguousDispatch(
                        "launch did not occur, but account activation cleanup requires reconciliation"
                    ) from cleanup_exc
        try:
            changed = queue.abort_unlaunched_attempt(
                task.id,
                eligibility_key,
                attempt.id,
                reason,
                release_activation=release_activation,
            )
            if not changed and queue.claim_for(task.id, eligibility_key) is not None:
                raise QueueError("exact attempt could not be aborted")
        except Exception as cleanup_exc:
            queue.mark_attempt_ambiguous(
                task.id,
                eligibility_key,
                attempt.id,
                f"known-not-launched cleanup requires reconciliation: {str(cleanup_exc)[:500]}",
            )
            raise AmbiguousDispatch(
                "launch did not occur, but cleanup requires reconciliation"
            ) from cleanup_exc
        if outcome_path is not None:
            remove_outcome_file(outcome_path)

    try:
        outcome_path = materialize_outcome_file(config, attempt.id)
        if lease_managed:
            assert account is not None
            from .kick import active_marker_for_account

            try:
                marker_before_activation = active_marker_for_account(config, account.id)
            except InvalidRoute as exc:
                if (
                    len(provider_accounts) == 1
                    and isinstance(exc.__cause__, FileNotFoundError)
                ):
                    marker_before_activation = None
                else:
                    raise
            try:
                queue.acquire_activation(
                    task.id,
                    eligibility_key,
                    provider.id,
                    account.id,
                    lambda: _activation(config, account, "activate", None),
                    attempt_id=attempt.id,
                )
            except Exception as exc:
                incomplete = any(
                    lease.task_id == task.id and lease.eligibility_key == eligibility_key
                    for lease in queue.activation_leases(provider_id=provider.id)
                )
                assert account.activation_adapter_id is not None
                marker_unchanged = False
                try:
                    marker_after_activation = active_marker_for_account(config, account.id)
                    marker_unchanged = (
                        marker_before_activation is not None
                        and marker_after_activation == marker_before_activation
                    )
                except InvalidRoute:
                    pass
                if (
                    incomplete
                    and marker_unchanged
                    and _trusted_unswitched_activation(exc, account.activation_adapter_id)
                ):
                    # The adapter verified the active account never moved and rolled
                    # the pin back. That is known-not-launched, not post-launch
                    # ambiguity; dropping the unproven lease unblocks the provider.
                    try:
                        abandoned = queue.abandon_unproven_activation(
                            task.id, eligibility_key,
                        )
                    except Exception as cleanup_exc:
                        queue.mark_attempt_ambiguous(
                            task.id,
                            eligibility_key,
                            attempt.id,
                            "known-not-launched activation cleanup requires reconciliation: "
                            f"{str(cleanup_exc)[:500]}",
                        )
                        raise AmbiguousDispatch(
                            "account activation cleanup requires reconciliation"
                        ) from cleanup_exc
                    if abandoned:
                        raise ActivationUnavailable(
                            str(exc), known_not_switched=True,
                        ) from exc
                if incomplete:
                    queue.mark_attempt_ambiguous(
                        task.id, eligibility_key, attempt.id,
                        f"account activation requires reconciliation: {str(exc)[:500]}",
                    )
                    raise AmbiguousDispatch(
                        "account activation outcome is incomplete and requires reconciliation"
                    ) from exc
                raise ActivationUnavailable(
                    str(exc), known_not_switched=True,
                ) from exc
            activated = True
        else:
            try:
                _activation(config, account, "activate", activation_call)
            except Exception as exc:
                if (
                    isinstance(exc, ActivationUnavailable)
                    and exc.known_not_switched
                ):
                    raise ActivationUnavailable(
                        str(exc), known_not_switched=True,
                    ) from exc
                queue.mark_attempt_ambiguous(
                    task.id,
                    eligibility_key,
                    attempt.id,
                    f"account activation requires reconciliation: {str(exc)[:500]}",
                )
                raise AmbiguousDispatch(
                    "account activation outcome is incomplete and requires reconciliation"
                ) from exc
            activated = account is not None and activation_call is not None
        try:
            rechecked_dependency_base = queue.dependency_base(task.id)
        except QueueError as exc:
            raise KnownDispatchFailure(str(exc)) from exc
        if rechecked_dependency_base != selected_dependency_base:
            raise KnownDispatchFailure(
                "dependency base changed after claim; refusing the router launch"
            )
        factory_run_id = new_factory_run_id(task.id, attempt.id) if task.use_implement else None
        prompt = render_prompt(
            config,
            task,
            eligibility_key,
            provider.id,
            account_id,
            factory_run_id,
            attempt=attempt,
            outcome_path=outcome_path,
            dependency_base=rechecked_dependency_base,
            recovery=(
                readiness.get("recovery")
                if isinstance(readiness.get("recovery"), Mapping)
                else None
            ),
        )
        launch_argv = list(adapter.argv) + [
            "run", "--provider", provider.dispatch.provider, "--dir", task.cwd,
            "--name", task.title,
        ]
        model = canonical_model(task.model)
        if model:
            launch_argv.extend(["--model", model])
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
                task.id, eligibility_key, attempt_id=attempt.id,
                status="dispatched", provider_id=provider.id,
                account_id=account_id, router_job_id=job_id, trigger=trigger,
            )
        except Exception as exc:
            raise AmbiguousDispatch("router launched but dispatch bookkeeping failed") from exc
        if factory_run_id is not None:
            # After the queue's own dispatched record so a telemetry problem can never
            # leave the claim in a state that looks unlaunched.
            placeholder_written = record_factory_run(
                task,
                provider,
                factory_run_id,
                response.get("log_id"),
                telemetry_call,
                attempt.id,
            )
            if placeholder_written:
                replay_factory_terminal(queue, task, attempt.id)
        if account is not None and account.activation_scope == "launch" and activated:
            try:
                if lease_managed:
                    queue.release_activation_after_dispatch(
                        task.id, eligibility_key, release_lease, attempt_id=attempt.id,
                    )
                else:
                    _activation(config, account, "release", activation_call)
                activated = False
            except Exception as exc:
                raise AmbiguousDispatch(
                    "router launched but launch-scoped activation cleanup requires reconciliation"
                ) from exc
        return DispatchResult(
            task_id=task.id,
            eligibility_key=eligibility_key,
            provider_id=provider.id,
            account_id=account_id,
            job_id=job_id,
            prompt=prompt,
            factory_run_id=factory_run_id,
            attempt_id=attempt.id,
            dependency_base=rechecked_dependency_base,
        )
    except AmbiguousDispatch as exc:
        claim = queue.claim_for(task.id, eligibility_key)
        if claim is not None and claim.attempt_id == attempt.id:
            queue.mark_attempt_ambiguous(
                task.id, eligibility_key, attempt.id, str(exc),
            )
        # Runtime adapter leases remain durable after an ambiguous launch: the task stays
        # non-dispatchable and the active account cannot be switched out from under a job that
        # may exist. Injected test/operator callbacks retain their historical eager release.
        if activation_call is not None and activated:
            try:
                _activation(config, account, "release", activation_call)
            except Exception:
                pass
        raise
    except ActivationUnavailable as exc:
        abort_known_nonlaunch(f"activation unavailable: {str(exc)[:500]}")
        raise
    except Exception as exc:
        abort_known_nonlaunch(f"known launch failure: {str(exc)[:500]}")
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
    now_epoch: int | None = None,
) -> tuple[DispatchResult, ...]:
    results: list[DispatchResult] = []
    for task_id in task_ids:
        try:
            results.append(dispatch(
                config, queue, task_id=task_id, eligibility_key=eligibility_key,
                requested_provider=provider_id, router_call=router_call,
                activation_call=activation_call,
                now_epoch=now_epoch,
            ))
        except (AlreadyClaimed, KnownDispatchFailure):
            continue
    return tuple(results)
