"""Close abandoned runs when T3 shows a terminal thread and the queue has no terminal event."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import AdapterError
from .config import ConfigError, RuntimeConfig
from .db import QueueDB, QueueError
from .dispatcher import DispatchError, _activation
from .t3_state import TERMINAL_RUN_STATUSES, T3StateError, T3Thread, lookup_thread

_ABANDONED_TURN_IDLE_SECONDS = 900


def execute_adapter(adapter, fields, **_kwargs):
    """Observation seam. Production reads the T3 state database.

    Tests replace this with the historical status-row payload. A direct call
    passes ``thread_id`` and optional ``state_path``.
    """

    del adapter
    thread_id = fields["thread_id"]
    path = fields.get("state_path")
    return lookup_thread(thread_id, path)


def _rfc3339_epoch(value: str) -> float | None:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _abandoned_turn(row: dict[str, Any], now_epoch: int | None) -> tuple[str, str] | None:
    if row.get("state") != "turn_ended":
        return None
    run_status = row.get("run_status")
    if run_status not in ("cancelled", "interrupted"):
        return None
    activity = row.get("last_activity_at")
    if not isinstance(activity, str):
        return None
    activity_epoch = _rfc3339_epoch(activity)
    if activity_epoch is None:
        return None
    now = (
        float(now_epoch) if now_epoch is not None
        else datetime.now(timezone.utc).timestamp()
    )
    if now - activity_epoch < _ABANDONED_TURN_IDLE_SECONDS:
        return None
    return run_status, activity


@dataclass(frozen=True)
class _Observation:
    run_status: str | None
    hold_reason: str | None
    reason: str
    signature: str


def _normalize_observation(raw: Any, thread_id: str, *, provider_name: str, now_epoch: int | None) -> _Observation | str | None:
    if isinstance(raw, T3Thread):
        status = raw.run_status
        if raw.active_run or status not in TERMINAL_RUN_STATUSES:
            hold = (
                "worker is running" if raw.active_run or status in {
                    "preparing", "starting", "running", "queued",
                } or status is None
                else "worker state is unknown; operator reconciliation required"
            )
            return _Observation(status, hold, hold, "")
        if status in {"cancelled", "interrupted"}:
            reason = f"T3 run_status {status} without a terminal record"
            return _Observation(status, None, reason, f"unknown_launch:t3_run_{status}")
        reason = f"T3 observed {status} execution without a terminal record"
        return _Observation(status, None, reason, "unknown_launch:missing_terminal_record")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "router status returned malformed rows"
    rows = raw.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return "router status returned malformed rows"
    matches = [
        row for row in rows
        if row.get("job_id") == thread_id and row.get("provider") == provider_name
    ]
    if len(matches) != 1:
        return "job missing or duplicated in router status; operator reconciliation required"
    row = matches[0]
    state = row.get("state")
    abandoned = _abandoned_turn(row, now_epoch)
    if state not in ("completed", "failed") and abandoned is None:
        hold = (
            "worker is running" if state == "running"
            else "worker state is unknown; operator reconciliation required"
        )
        return _Observation(None, hold, hold, "")
    if abandoned is None:
        reason = f"router observed {state} execution without a terminal record"
        return _Observation(str(state), None, reason, "unknown_launch:missing_terminal_record")
    run_status, activity = abandoned
    reason = (
        "router observed turn_ended "
        f"run_status {run_status} last_activity_at {activity} "
        "without a terminal record"
    )
    return _Observation(run_status, None, reason, f"unknown_launch:t3_run_{run_status}")


def reconcile_inflight(
    config: RuntimeConfig, queue: QueueDB, *, dry_run: bool = False,
    activation_call: Callable[[str, str], Any] | None = None,
    now_epoch: int | None = None,
    thread_lookup: Callable[[str], T3Thread | None] | None = None,
    state_path: Path | None = None,
) -> tuple[dict[str, Any], ...]:
    """Read T3 thread state and record a missing terminal event as failed.

    ``thread_lookup`` is a test seam. Runtime callers leave it unset and read
    ``~/.t3/userdata/statev2.sqlite`` read-only. A missing or duplicate observation
    stays held. An active run stays held.
    """

    reports: list[dict[str, Any]] = []
    observations: dict[str, T3Thread | None | str] = {}
    for run in queue.inflight():
        report = {
            "task_id": run.task, "eligibility_key": run.eligibility_key,
            "router_job_id": run.router_job_id, "attempt_id": run.attempt_id,
            "action": "held",
        }
        reports.append(report)
        claim = queue.claim_for(run.task, run.eligibility_key)
        if claim is not None and claim.state == "ambiguous":
            report["reason"] = "launch ownership is ambiguous; operator reconciliation required"
            continue
        if not run.router_job_id or not run.provider_id or not run.eligibility_key:
            report["reason"] = "missing execution identity; operator reconciliation required"
            continue
        try:
            thread_id = run.router_job_id
            if thread_id not in observations:
                try:
                    if thread_lookup is not None:
                        raw = thread_lookup(thread_id)
                    else:
                        provider = config.provider(run.provider_id)
                        adapter = config.adapter(provider.dispatch.adapter_id)
                        raw = execute_adapter(
                            adapter,
                            {"thread_id": thread_id, "state_path": state_path},
                            config=config,
                        )
                    observations[thread_id] = _normalize_observation(
                        raw, thread_id, provider_name=config.provider(run.provider_id).dispatch.provider,
                        now_epoch=now_epoch,
                    )
                except (T3StateError, AdapterError) as exc:
                    observations[thread_id] = str(exc)
            observed = observations[thread_id]
            if isinstance(observed, str):
                report["reason"] = observed
                continue
            if observed is None:
                report["reason"] = "thread missing in T3 state; operator reconciliation required"
                continue
            if observed.hold_reason:
                report["reason"] = observed.hold_reason
                continue
            if queue.continuation_lacks_separate_liveness(run):
                report["reason"] = (
                    "shared thread is already terminal; "
                    "continuation liveness is not proven"
                )
                continue
            signature = observed.signature
            report["reason"] = observed.reason
            if dry_run:
                report["action"] = "would_fail"
                continue
            if not any(item.rowid_pk == run.rowid_pk for item in queue.inflight()):
                report["action"] = "already_recorded"
                continue
            account = config.account(run.account_id) if run.account_id else None
            event = queue.record(
                run.task, run.eligibility_key, attempt_id=run.attempt_id,
                status="failed", kind=run.kind,
                cycle=run.cycle, provider_id=run.provider_id, account_id=run.account_id,
                router_job_id=run.router_job_id,
                summary=f"scout reconciliation: {report['reason']}; task completion unverified",
                outcome={
                    "reason": {
                        "code": "unknown_launch",
                        "detail": report["reason"],
                        "signature": signature,
                    },
                } if run.attempt_id is not None else None,
                timestamp=(
                    datetime.fromtimestamp(now_epoch, timezone.utc).replace(
                        microsecond=0,
                    ).isoformat().replace("+00:00", "Z")
                    if now_epoch is not None else None
                ),
                now_epoch=now_epoch,
                release_activation=lambda account=account: _activation(config, account, "release", activation_call),
            )
            report.update(action="failed", terminal_rowid=event.rowid_pk)
        except (ConfigError, QueueError, DispatchError) as exc:
            report["reason"] = str(exc)
    return tuple(reports)
