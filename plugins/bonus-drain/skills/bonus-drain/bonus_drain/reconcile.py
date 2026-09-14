"""Close abandoned runs only when the router positively observes a terminal worker."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from .adapters import AdapterError, execute_adapter
from .config import ConfigError, RuntimeConfig
from .db import QueueDB, QueueError
from .dispatcher import DispatchError, _activation


def reconcile_inflight(
    config: RuntimeConfig, queue: QueueDB, *, dry_run: bool = False,
    activation_call: Callable[[str, str], Any] | None = None,
    now_epoch: int | None = None,
) -> tuple[dict[str, Any], ...]:
    """Reuse router status and the CLI's terminal-record path; never retry a task.

    The router exposes a bounded history, not an exact-job query. An omitted job is
    unknown, never dead. Use the fresh `state`, not the persisted historical outcome.
    """
    reports: list[dict[str, Any]] = []
    observations: dict[str, list[dict[str, Any]] | str] = {}
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
            provider = config.provider(run.provider_id)
            adapter = config.adapter(provider.dispatch.adapter_id)
            if adapter.id not in observations:
                try:
                    result = execute_adapter(
                        replace(
                            adapter,
                            argv=(*adapter.argv, "status", "--limit", "1000", "--json"),
                            timeout_seconds=min(adapter.timeout_seconds, 30),
                        ),
                        # status exits 1 for a valid report containing failed jobs;
                        # 2 means the probe itself could not run.
                        {}, config=config, accepted_exit_codes=(0, 1),
                    )
                    rows = result.get("rows") if isinstance(result, dict) else None
                    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                        raise AdapterError("router status returned malformed rows")
                    observations[adapter.id] = rows
                except AdapterError as exc:
                    observations[adapter.id] = str(exc)
            rows = observations[adapter.id]
            if isinstance(rows, str):
                report["reason"] = rows
                continue
            matches = [row for row in rows if (
                row.get("job_id") == run.router_job_id
                and row.get("provider") == provider.dispatch.provider
            )]
            if len(matches) != 1:
                report["reason"] = "job missing or duplicated in router status; operator reconciliation required"
                continue
            state = matches[0].get("state")
            if state not in ("completed", "failed"):
                report["reason"] = (
                    "worker is running" if state == "running"
                    else "worker state is unknown; operator reconciliation required"
                )
                continue
            report["reason"] = f"router observed {state} execution without a terminal record"
            if dry_run:
                report["action"] = "would_fail"
                continue
            # Another worker/scout may have recorded its result during the status probe.
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
                        "signature": "unknown_launch:missing_terminal_record",
                    },
                } if run.attempt_id is not None else None,
                timestamp=(
                    datetime.fromtimestamp(now_epoch, timezone.utc).replace(
                        microsecond=0,
                    ).isoformat().replace("+00:00", "Z")
                    if now_epoch is not None else None
                ),
                now_epoch=now_epoch,
                release_activation=lambda: _activation(config, account, "release", activation_call),
            )
            report.update(action="failed", terminal_rowid=event.rowid_pk)
        except (ConfigError, QueueError, DispatchError) as exc:
            # record() rejects conflicting terminal results atomically. Preserve the winner.
            # Failed activation release retains ownership and remains a visible blocker.
            report["reason"] = str(exc)
    return tuple(reports)
