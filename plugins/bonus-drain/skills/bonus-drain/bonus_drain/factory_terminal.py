"""Mirror a task's terminal ledger event onto its factory telemetry rows.

The dispatcher writes a ``dispatched`` placeholder when it launches an /implement task.
A driver that never emits its own terminal telemetry would leave that row open forever,
so ``bonus-drain record`` closes it from the ledger. The installed telemetry writer owns
database resolution, readiness, transaction, and completion checks. Run fields other
than an open status are filled only while NULL. Like the dispatch time row, this is a
measurement side effect and never fails the record.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .dispatcher import (
    factory_telemetry_script,
    new_factory_run_id,
)


OPEN_RUN_STATUSES = frozenset({"dispatched", "running"})
RUN_FILL_FIELDS = ("status", "completed_at", "outcome", "pr_url", "pr_state", "merged_at")
# The factory runs table has no status CHECK and already carries `skipped` rows.
LEDGER_TO_RUN_STATUS = {"done": "complete", "failed": "failed", "skipped": "skipped"}
OUTCOME_MAX_CHARS = 500
GH_TIMEOUT_SECONDS = 15.0
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
PR_NUMBER = re.compile(r"(?<![\w&/])#(\d+)\b")
SENTENCE_END = re.compile(r"(?<=[.!?])\s")


@dataclass(frozen=True)
class PullRequest:
    url: str
    state: str | None
    merged_at: str | None


def first_sentence(summary: str | None) -> str | None:
    text = " ".join((summary or "").split())
    if not text:
        return None
    return SENTENCE_END.split(text, maxsplit=1)[0][:OUTCOME_MAX_CHARS]


def pr_reference(summary: str | None) -> str | None:
    """The first PR URL or ``#NNNN`` in the summary, whichever appears earlier."""
    text = summary or ""
    matches = [match for match in (PR_URL.search(text), PR_NUMBER.search(text)) if match]
    if not matches:
        return None
    first = min(matches, key=lambda match: match.start())
    return first.group(0) if first.re is PR_URL else first.group(1)


def _pull_request(value: Any) -> PullRequest | None:
    if not isinstance(value, Mapping):
        return None
    url = value.get("url")
    if not isinstance(url, str) or PR_URL.fullmatch(url) is None:
        return None
    raw_state = value.get("state")
    state = raw_state.lower() if isinstance(raw_state, str) and raw_state else None
    raw_merged_at = value.get("mergedAt")
    merged_at = raw_merged_at if isinstance(raw_merged_at, str) and raw_merged_at else None
    return PullRequest(url=url, state=state, merged_at=merged_at)


def resolve_pull_request(
    reference: str | None,
    branch: str | None,
    cwd: str,
) -> PullRequest | None:
    """Resolve PR identity from the pushed branch, then fall back to the summary."""
    if shutil.which("gh") is None:
        if reference is not None and PR_URL.fullmatch(reference):
            return PullRequest(reference, None, None)
        return None
    commands: list[tuple[list[str], bool]] = []
    if isinstance(branch, str) and branch.strip():
        commands.append(([
            "gh", "pr", "list", "--head", branch.strip(), "--state", "all",
            "--json", "url,state,mergedAt", "--limit", "1",
        ], True))
    if reference is not None:
        commands.append(([
            "gh", "pr", "view", reference, "--json", "url,state,mergedAt",
        ], False))
    for argv, listed in commands:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True, text=True, cwd=Path(cwd).expanduser(),
                timeout=GH_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        try:
            value = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            continue
        if listed:
            value = value[0] if isinstance(value, list) and value else None
        resolved = _pull_request(value)
        if resolved is not None:
            return resolved
    return None


def read_factory_run(writer: Any, run_id: str) -> Mapping[str, Any] | None:
    """Read one runs row, resolving the database exactly as the telemetry writer does."""
    path = Path(writer.db_path())
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def candidate_fills(
    existing: Mapping[str, Any],
    status: str,
    event_ts: str,
    summary: str | None,
    cwd: str,
    pr_resolver: Callable[
        [str | None, str | None, str], PullRequest | None
    ] = resolve_pull_request,
) -> dict[str, Any]:
    """Return terminal values from the ledger and one coherent PR lookup."""
    fills: dict[str, Any] = {}
    if any(existing.get(field) is None for field in ("pr_url", "pr_state", "merged_at")):
        reference = pr_reference(summary)
        pull_request = pr_resolver(reference, existing.get("branch"), cwd)
        existing_url = existing.get("pr_url")
        if pull_request is not None and existing_url in {None, pull_request.url}:
            fills["pr_url"] = pull_request.url
            if pull_request.state is not None:
                fills["pr_state"] = pull_request.state.lower()
            if pull_request.merged_at is not None:
                fills["merged_at"] = pull_request.merged_at
    fills["completed_at"] = event_ts
    fills["outcome"] = first_sentence(summary) or f"bonus-drain {status}"
    fills["status"] = LEDGER_TO_RUN_STATUS[status]
    return fills


def fill_only_null(existing: Mapping[str, Any], fills: Mapping[str, Any]) -> dict[str, Any] | None:
    """Narrow candidate fills to the row as it stands now, or None when nothing is left."""
    kept = {
        key: value for key, value in fills.items()
        if (
            existing.get(key) in OPEN_RUN_STATUSES
            if key == "status"
            else existing.get(key) is None
        )
    }
    return kept or None


def _writer_module(script: Path) -> Any | None:
    spec = importlib.util.spec_from_file_location("bonus_drain_factory_telemetry", script)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply_terminal(
    writer: Any,
    run_id: str,
    task_id: str,
    event_ts: str,
    candidates: Mapping[str, Any],
) -> bool:
    """Apply the terminal event atomically through the telemetry writer's DB API."""
    connection = writer.connect(Path(writer.db_path()))
    try:
        writer.require_current(connection, ready=True)
        with writer.immediate(connection):
            current = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,),
            ).fetchone()
            if current is None or current["drain_task_id"] != task_id:
                return False
            updates = fill_only_null(dict(current), candidates) or {}
            resolved_url = candidates.get("pr_url")
            if (
                current["pr_url"] is not None
                and resolved_url is not None
                and current["pr_url"] != resolved_url
            ):
                for field in ("pr_url", "pr_state", "merged_at"):
                    updates.pop(field, None)
            if updates.get("status") == "complete":
                writer.assert_blocking_findings_resolved(connection, run_id)
            if updates:
                ordered = [field for field in RUN_FILL_FIELDS if field in updates]
                assignments = ",".join(f'"{field}"=?' for field in ordered)
                connection.execute(
                    f"UPDATE runs SET {assignments} WHERE run_id=? AND drain_task_id=?",
                    tuple(updates[field] for field in ordered) + (run_id, task_id),
                )
            stages = connection.execute(
                "UPDATE stages SET completed_at=COALESCE(completed_at,?) "
                "WHERE run_id=? AND status='running' AND completed_at IS NULL",
                (event_ts, run_id),
            ).rowcount
            return bool(updates or stages)
    finally:
        connection.close()


def record_factory_terminal(
    task_id: str,
    attempt_id: str | None,
    status: str,
    event_ts: str,
    summary: str | None,
    cwd: str,
    *,
    pr_resolver: Callable[
        [str | None, str | None, str], PullRequest | None
    ] = resolve_pull_request,
) -> bool:
    """Close the dispatch-written runs row for this attempt. Returns True when written."""
    if attempt_id is None or status not in LEDGER_TO_RUN_STATUS:
        return False
    try:
        script = factory_telemetry_script()
        if script is None:
            return False
        run_id = new_factory_run_id(task_id, attempt_id)
        writer = _writer_module(script)
        if writer is None:
            return False
        existing = read_factory_run(writer, run_id)
        if existing is None or existing.get("drain_task_id") != task_id:
            return False
        candidates = candidate_fills(existing, status, event_ts, summary, cwd, pr_resolver)
        return _apply_terminal(
            writer, run_id, task_id, event_ts, candidates,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail a record
        print(
            f"bonus-drain: factory run row for {task_id} not closed: {str(exc)[:500]}",
            file=sys.stderr,
        )
        return False
