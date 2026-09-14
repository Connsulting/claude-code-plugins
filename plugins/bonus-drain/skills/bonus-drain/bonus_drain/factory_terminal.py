"""Mirror a task's terminal ledger event onto its factory runs row.

The dispatcher writes a ``dispatched`` placeholder when it launches an /implement task. A
driver that never emits its own terminal telemetry would leave that row ``dispatched``
forever, so ``bonus-drain record`` closes it from the ledger. Every write goes through
``factory-telemetry.py``; the factory database is only read here. Only columns that are
still NULL are sent, and status only replaces the placeholder: a driver that wrote first
wins. Like the dispatch-time row, this is a measurement side effect and never fails the
record.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from .dispatcher import (
    FACTORY_SESSION_ENV_KEYS,
    FACTORY_TELEMETRY_TIMEOUT_SECONDS,
    factory_telemetry_script,
    new_factory_run_id,
)


PLACEHOLDER_STATUS = "dispatched"
# The factory runs table has no status CHECK and already carries `skipped` rows.
LEDGER_TO_RUN_STATUS = {"done": "complete", "failed": "failed", "skipped": "skipped"}
OUTCOME_MAX_CHARS = 500
GH_TIMEOUT_SECONDS = 15.0
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
PR_NUMBER = re.compile(r"(?<![\w&/])#(\d+)\b")
SENTENCE_END = re.compile(r"(?<=[.!?])\s")


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


def resolve_pr_url(reference: str, cwd: str) -> str | None:
    """Confirm the reference against the task's repo with gh when gh is available.

    Without gh a full URL is kept as written; a bare number cannot be placed in a repo
    and is dropped rather than guessed.
    """
    if shutil.which("gh") is None:
        return reference if PR_URL.fullmatch(reference) else None
    try:
        completed = subprocess.run(
            ["gh", "pr", "view", reference, "--json", "url", "--jq", ".url"],
            capture_output=True, text=True, cwd=Path(cwd).expanduser(),
            timeout=GH_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    url = completed.stdout.strip()
    return url if completed.returncode == 0 and PR_URL.fullmatch(url) else None


def read_factory_run(script: Path, run_id: str) -> Mapping[str, Any] | None:
    """Read one runs row, resolving the database exactly as the telemetry writer does."""
    spec = importlib.util.spec_from_file_location("bonus_drain_factory_telemetry", script)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = Path(module.db_path())
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
    pr_resolver: Callable[[str, str], str | None] = resolve_pr_url,
) -> dict[str, Any]:
    """Terminal values for the row; gh is only asked when the row still lacks a PR URL."""
    fills: dict[str, Any] = {}
    if existing.get("pr_url") is None:
        reference = pr_reference(summary)
        url = pr_resolver(reference, cwd) if reference is not None else None
        if url is not None:
            fills["pr_url"] = url
    fills["completed_at"] = event_ts
    fills["outcome"] = first_sentence(summary) or f"bonus-drain {status}"
    fills["status"] = LEDGER_TO_RUN_STATUS[status]
    return fills


def fill_only_null(existing: Mapping[str, Any], fills: Mapping[str, Any]) -> dict[str, Any] | None:
    """Narrow candidate fills to the row as it stands now, or None when nothing is left."""
    kept = {
        key: value for key, value in fills.items()
        if (existing.get(key) == PLACEHOLDER_STATUS if key == "status" else existing.get(key) is None)
    }
    if not kept:
        return None
    # record run requires the identity fields and always rewrites status, so the row's
    # own values ride along unchanged. Its placeholder branch clears session_id, so an
    # existing one is carried back in.
    payload = {
        "factory_version": existing["factory_version"],
        "repo": existing["repo"],
        "tier": existing["tier"],
        "status": existing["status"],
    }
    if existing.get("session_id"):
        payload["session_id"] = existing["session_id"]
    payload.update(kept)
    return payload


def record_factory_terminal(
    task_id: str,
    attempt_id: str | None,
    status: str,
    event_ts: str,
    summary: str | None,
    cwd: str,
    *,
    pr_resolver: Callable[[str, str], str | None] = resolve_pr_url,
) -> bool:
    """Close the dispatch-written runs row for this attempt. Returns True when written."""
    if attempt_id is None or status not in LEDGER_TO_RUN_STATUS:
        return False
    try:
        script = factory_telemetry_script()
        if script is None:
            return False
        run_id = new_factory_run_id(task_id, attempt_id)
        existing = read_factory_run(script, run_id)
        if existing is None or existing.get("drain_task_id") != task_id:
            return False
        candidates = candidate_fills(existing, status, event_ts, summary, cwd, pr_resolver)
        if fill_only_null(existing, candidates) is None:
            return False
        # The writer's upsert overwrites status and prefers incoming values, so re-read
        # after the gh lookup to keep a driver write that landed meanwhile. A write inside
        # the remaining read-to-commit gap can still be overwritten.
        current = read_factory_run(script, run_id)
        payload = fill_only_null(current, candidates) if current is not None else None
        if payload is None:
            return False
        env = {key: value for key, value in os.environ.items() if key not in FACTORY_SESSION_ENV_KEYS}
        with tempfile.NamedTemporaryFile(
            "w", prefix="bonus-drain-factory-terminal-", suffix=".json", delete=False,
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
                f"bonus-drain: factory run row for {task_id} not closed: {detail}",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail a record
        print(
            f"bonus-drain: factory run row for {task_id} not closed: {str(exc)[:500]}",
            file=sys.stderr,
        )
        return False
