"""Cache-only scout orchestration."""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from . import db, goals
from .config import RuntimeConfig
from .db import QueueDB, hour_round, task_requires_legacy_exclusive
from .dispatcher import (
    AmbiguousDispatch,
    DispatchResult,
    dispatch,
)
from .planner import PlanResult, build_plan
from .reconcile import reconcile_inflight
from .usage import read_all


@dataclass(frozen=True)
class ScoutReport:
    generated_at: int
    dry_run: bool
    plan: PlanResult
    dispatched: tuple[DispatchResult, ...]
    previews: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, str], ...]
    blockers: tuple[dict[str, Any], ...] = ()
    router_preflight: tuple[dict[str, Any], ...] = ()
    reconciliation: tuple[dict[str, Any], ...] = ()
    goal_updates: tuple[dict[str, Any], ...] = ()
    recoveries: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "dry_run": self.dry_run,
            "plan": self.plan.to_dict(),
            "dispatched": [item.to_dict() for item in self.dispatched],
            "previews": list(self.previews),
            "errors": list(self.errors),
            "blockers": list(self.blockers),
            "router_preflight": list(self.router_preflight),
            "reconciliation": list(self.reconciliation),
            "goal_updates": list(self.goal_updates),
            "recoveries": list(self.recoveries),
        }


@dataclass(frozen=True)
class TickPlan:
    cycle_anchor: int
    snapshots: Mapping[Any, Any]
    plan: PlanResult
    allocations: Mapping[tuple[str, str], tuple[Any, ...]]


class _InitializedQueueReader(QueueDB):
    """Read an already initialized queue without rerunning schema migrations."""

    def initialize(self) -> None:
        return None


def _initialized_queue_reader(queue: Any) -> Any:
    if isinstance(queue, _InitializedQueueReader):
        return queue
    if isinstance(queue, QueueDB):
        return _InitializedQueueReader(queue.path, timeout_seconds=queue.timeout_seconds)
    return queue


def _cycle_anchor(config: RuntimeConfig, snapshots: Mapping[Any, Any], now_epoch: int) -> int:
    resets: list[int] = []
    for account in config.accounts:
        snapshot = snapshots.get((account.provider_id, account.id))
        if snapshot is None:
            continue
        for reading in getattr(snapshot, "limits", {}).values():
            reset = reading.get("resets_at") if isinstance(reading, dict) else None
            if isinstance(reset, (int, float)) and not isinstance(reset, bool) and int(reset) > now_epoch:
                resets.append(int(reset))
    return hour_round(min(resets)) if resets else hour_round(now_epoch)


def _router_preflight(config: RuntimeConfig, plan: PlanResult) -> tuple[dict[str, Any], ...]:
    """Describe each resolved router executable needed by this tick without launching it."""

    adapter_ids = {
        config.provider(batch.provider_id).dispatch.adapter_id for batch in plan.batches
    }
    result: list[dict[str, Any]] = []
    for adapter_id in sorted(adapter_ids):
        adapter = config.adapter(adapter_id)
        executable = Path(adapter.argv[0])
        available = False
        identity = None
        try:
            available = executable.is_file() and os.access(executable, os.X_OK)
            if available:
                stat = executable.stat()
                identity = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            # A router replaced between the checks is unavailable for this tick.
            available = False
        result.append({
            "adapter_id": adapter.id,
            "executable": str(executable),
            "available": available,
            "identity": identity,
        })
    return tuple(result)


def _inflight_index(
    queue: QueueDB, now_epoch: int,
) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
    """Count non-terminal runs by provider and by (provider, account)."""

    by_provider: dict[str, int] = {}
    by_account: dict[tuple[str, str], int] = {}
    seen_attempts: set[str] = set()
    seen_legacy: set[tuple[str, str]] = set()
    for run in queue.inflight_details(now_epoch=now_epoch):
        provider_id = run.get("provider_id")
        account_id = run.get("account_id")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        by_provider[provider_id] = by_provider.get(provider_id, 0) + 1
        attempt_id = run.get("attempt_id")
        if isinstance(attempt_id, str) and attempt_id:
            seen_attempts.add(attempt_id)
        elif isinstance(run.get("eligibility_key"), str):
            seen_legacy.add((str(run.get("task")), str(run["eligibility_key"])))
        if isinstance(account_id, str) and account_id:
            key = (provider_id, account_id)
            by_account[key] = by_account.get(key, 0) + 1
    # A claim can be held before a dispatched row exists when launch identity is
    # ambiguous. It still consumes only its provider/account capacity.
    for claim in queue.claims():
        if claim.attempt_id is not None and claim.attempt_id in seen_attempts:
            continue
        if claim.attempt_id is None and (claim.task_id, claim.eligibility_key) in seen_legacy:
            continue
        by_provider[claim.provider_id] = by_provider.get(claim.provider_id, 0) + 1
        if claim.account_id:
            key = (claim.provider_id, claim.account_id)
            by_account[key] = by_account.get(key, 0) + 1
    return by_provider, by_account


def _apply_inflight_caps(
    plan: PlanResult,
    queue: QueueDB,
    *,
    now_epoch: int,
    provider_holds: tuple[str, ...] = (),
) -> PlanResult:
    """Subtract already-running jobs from this tick, per provider.

    A running job tightens that provider's cap; it does not block other providers.
    Running work on a sibling account of the same provider blocks a switch so a
    shared credential is not moved under live jobs.
    """

    by_provider, by_account = _inflight_index(queue, now_epoch)
    held_providers = set(provider_holds) | {
        claim.provider_id for claim in queue.claims(state="ambiguous")
    }
    if not by_provider and not held_providers:
        return plan
    closed = dict(plan.closed)
    gates_by_key = {(gate.provider_id, gate.account_id): gate for gate in plan.gates}
    kept = []
    for batch in plan.batches:
        key = (batch.provider_id, batch.account_id)
        if batch.provider_id in held_providers:
            reason = "provider lifecycle requires reconciliation"
            closed[key] = reason
            gates_by_key[key] = replace(
                gates_by_key[key], open=False, reason=reason, batch_size=0,
            )
            continue
        sibling = next(
            (
                account_id
                for (provider_id, account_id), count in by_account.items()
                if provider_id == batch.provider_id and account_id != batch.account_id and count > 0
            ),
            None,
        )
        if sibling is not None:
            reason = f"provider inflight on {sibling}"
            closed[key] = reason
            gates_by_key[key] = replace(
                gates_by_key[key], open=False, reason=reason, batch_size=0,
            )
            continue
        cap = min(
            batch.batch_size,
            max(0, batch.surplus_jobs - by_provider.get(batch.provider_id, 0)),
        )
        if cap <= 0:
            reason = "provider inflight at surplus cap"
            closed[key] = reason
            gates_by_key[key] = replace(
                gates_by_key[key], open=False, reason=reason, batch_size=0,
            )
            continue
        if cap != batch.batch_size:
            batch = replace(batch, batch_size=cap)
            gates_by_key[key] = replace(gates_by_key[key], batch_size=cap)
        kept.append(batch)
    gates = tuple(gates_by_key[(gate.provider_id, gate.account_id)] for gate in plan.gates)
    return PlanResult(tuple(kept), closed, gates, plan.generated_at)


def _apply_global_cap(
    plan: PlanResult, queue: QueueDB, max_jobs: int | None, *, now_epoch: int,
) -> PlanResult:
    """Cap new launches so in-flight plus this tick stay at most ``max_jobs``.

    Per-provider ``batch_size`` already limits one engine. This is the cross-provider
    ceiling. Urgent (last-day) batches take remaining slots first, then nearest reset.
    """

    if max_jobs is None:
        return plan
    by_provider, _by_account = _inflight_index(queue, now_epoch)
    remaining = max(0, max_jobs - sum(by_provider.values()))
    closed = dict(plan.closed)
    gates_by_key = {(gate.provider_id, gate.account_id): gate for gate in plan.gates}
    ranked = sorted(
        plan.batches,
        key=lambda batch: (
            not batch.urgent, batch.resets_at, batch.provider_id, batch.account_id,
        ),
    )
    kept: list[Any] = []
    for batch in ranked:
        key = (batch.provider_id, batch.account_id)
        if remaining <= 0:
            reason = "global job cap reached"
            closed[key] = reason
            gates_by_key[key] = replace(
                gates_by_key[key], open=False, reason=reason, batch_size=0,
            )
            continue
        cap = min(batch.batch_size, remaining)
        if cap != batch.batch_size:
            batch = replace(batch, batch_size=cap)
            gates_by_key[key] = replace(gates_by_key[key], batch_size=cap)
        kept.append(batch)
        remaining -= cap
    kept.sort(key=lambda batch: (batch.resets_at, batch.provider_id, batch.account_id))
    gates = tuple(gates_by_key[(gate.provider_id, gate.account_id)] for gate in plan.gates)
    return PlanResult(tuple(kept), closed, gates, plan.generated_at)


def plan_tick(
    config: RuntimeConfig,
    queue: QueueDB,
    cache_root: str | Path | None = None,
    *,
    now_epoch: int | None = None,
    provider_holds: tuple[str, ...] | None = None,
) -> TickPlan:
    """Build one adjusted tick plan from cache and initialized SQLite reads only."""

    now = int(time.time() if now_epoch is None else now_epoch)
    reader = _initialized_queue_reader(queue)
    snapshots = read_all(config, cache_root, now_epoch=now)
    anchor = _cycle_anchor(config, snapshots, now)
    availability: dict[tuple[str, str], int] = {}
    for account in config.accounts:
        provider = config.provider(account.provider_id)
        availability[(account.provider_id, account.id)] = reader.count_eligible(
            anchor,
            provider_id=provider.id,
            capabilities=provider.capabilities, automatic=True, now_epoch=now,
        )
    plan = build_plan(config, snapshots, eligible_count=availability, now_epoch=now)
    if provider_holds is None:
        provider_holds = db.doctor(queue).provider_holds
    plan = _apply_inflight_caps(
        plan, queue, now_epoch=now, provider_holds=provider_holds,
    )
    plan = _apply_global_cap(plan, queue, config.max_jobs, now_epoch=now)
    allocations: dict[tuple[str, str], tuple[Any, ...]] = {}

    # Build a capacity-expanded bipartite graph and find an augmenting-path matching. Processing
    # tasks in queue order preserves priority, while reassignment prevents a flexible task from
    # occupying the only slot capable of running constrained work. Batch slots remain ordered by
    # reset, so dispatch still runs nearest-reset-first after identities are reserved globally.
    task_by_id: dict[str, Any] = {}
    task_order: list[str] = []
    task_slots: dict[str, list[int]] = {}
    slots: list[int] = []
    slot_batch: dict[int, int] = {}
    for batch_index, batch in enumerate(plan.batches):
        provider = config.provider(batch.provider_id)
        candidates = list(reader.eligible_tasks(
            batch.resets_at,
            provider_id=provider.id,
            capabilities=provider.capabilities, automatic=True, now_epoch=now,
        ))
        batch_slots: list[int] = []
        for _index in range(batch.batch_size):
            slot = len(slots)
            slots.append(slot)
            slot_batch[slot] = batch_index
            batch_slots.append(slot)
        for task in candidates:
            if task.id not in task_by_id:
                task_by_id[task.id] = task
                task_order.append(task.id)
            task_slots.setdefault(task.id, []).extend(batch_slots)

    slot_task: dict[int, str] = {}

    def batch_fill(slot: int) -> int:
        batch_index = slot_batch[slot]
        return sum(1 for taken, _task in slot_task.items() if slot_batch[taken] == batch_index)

    def augment(task_id: str, seen_slots: set[int], seen_tasks: set[str]) -> bool:
        if task_id in seen_tasks:
            return False
        seen_tasks.add(task_id)
        # Prefer emptier provider batches so portable work cannot fill Claude's
        # six slots and leave a Codex surplus with nothing to run.
        ordered = sorted(task_slots.get(task_id, ()), key=lambda slot: (batch_fill(slot), slot))
        for slot in ordered:
            if slot in seen_slots:
                continue
            seen_slots.add(slot)
            incumbent = slot_task.get(slot)
            if incumbent is None or augment(incumbent, seen_slots, seen_tasks):
                slot_task[slot] = task_id
                return True
        return False

    # A legacy-exclusive task has fewer places to run than portable work. Preserve the
    # queue's normal priority order within each class, but exhaust exclusive work first so
    # portable tasks cannot consume every compatible slot across a multi-provider tick.
    task_order.sort(key=lambda task_id: not task_requires_legacy_exclusive(task_by_id[task_id]))
    for task_id in task_order:
        augment(task_id, set(), set())

    for batch_index, batch in enumerate(plan.batches):
        allocations[(batch.provider_id, batch.account_id)] = tuple(
            task_by_id[slot_task[slot]]
            for slot in slots
            if slot_batch[slot] == batch_index and slot in slot_task
        )

    adjusted_batches = tuple(
        replace(
            batch,
            batch_size=len(allocations[(batch.provider_id, batch.account_id)]),
        )
        for batch in plan.batches
        if allocations[(batch.provider_id, batch.account_id)]
    )
    adjusted_by_account = {
        (batch.provider_id, batch.account_id): batch for batch in adjusted_batches
    }
    adjusted_closed = dict(plan.closed)
    adjusted_gates = []
    for gate in plan.gates:
        key = (gate.provider_id, gate.account_id)
        adjusted = adjusted_by_account.get(key)
        if gate.open and adjusted is None:
            adjusted_closed[key] = "no compatible unallocated tasks remain"
            adjusted_gates.append(replace(
                gate, open=False, reason=adjusted_closed[key], batch_size=0,
            ))
        elif adjusted is not None:
            adjusted_gates.append(replace(gate, batch_size=adjusted.batch_size))
        else:
            adjusted_gates.append(gate)
    adjusted_plan = PlanResult(
        adjusted_batches,
        adjusted_closed,
        tuple(adjusted_gates),
        plan.generated_at,
    )
    return TickPlan(anchor, snapshots, adjusted_plan, allocations)


def run_once(
    config: RuntimeConfig,
    queue: QueueDB | None = None,
    cache_root: str | Path | None = None,
    *,
    now_epoch: int | None = None,
    dry_run: bool = False,
    router_call: Callable[..., Any] | None = None,
    activation_call: Callable[[str, str], Any] | None = None,
) -> ScoutReport:
    """Plan and dispatch one tick using cache only.

    The scout never invokes a usage adapter.  ``refresh`` is the sole usage producer.
    """

    now = int(time.time() if now_epoch is None else now_epoch)
    queue = queue or QueueDB(
        config.database, recurrence_timezone=config.recurrence_timezone,
    )
    queue.initialize()
    dispatched: list[DispatchResult] = []
    previews: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    lifecycle_report = db.doctor(queue)
    lifecycle_blockers: tuple[dict[str, Any], ...] = ()
    lifecycle_errors: tuple[dict[str, str], ...] = ()
    if not lifecycle_report.ok:
        message = "; ".join(lifecycle_report.diagnostics)
        lifecycle_blockers = ({
            "kind": "reconciliation_required",
            "tasks": list(lifecycle_report.reconciliation_required),
            "message": message,
        },)
        lifecycle_errors = ({
            "task_id": "*", "kind": "reconciliation_required",
            "message": message or "queue lifecycle requires reconciliation",
        },)

    reconciliation = reconcile_inflight(
        config, queue, dry_run=dry_run, activation_call=activation_call,
        now_epoch=now,
    )
    goal_updates = tuple(goals.GoalStore(queue).tick(now=now, dry_run=dry_run))
    recoveries = tuple(
        decision.to_dict()
        for decision in queue.reconcile_recoveries(now_epoch=now, dry_run=dry_run)
    )
    tick = plan_tick(
        config, queue, cache_root, now_epoch=now,
        provider_holds=lifecycle_report.provider_holds,
    )
    plan = tick.plan
    router_preflight = _router_preflight(config, plan)

    unavailable = [item for item in router_preflight if not item["available"]]
    if unavailable:
        blockers = lifecycle_blockers + tuple({
            "kind": "router_unavailable",
            "adapter_id": item["adapter_id"],
            "executable": item["executable"],
            "message": "resolved agent-router executable is missing or not executable",
        } for item in unavailable)
        router_errors = lifecycle_errors + tuple({
            "task_id": "*", "kind": "router_unavailable",
            "message": f"router preflight failed: {item['executable']}",
        } for item in unavailable)
        return ScoutReport(
            now, dry_run, plan, (), (), router_errors, blockers, router_preflight,
            reconciliation, goal_updates, recoveries,
        )

    for batch in plan.batches:  # already nearest-reset-first
        tasks = tick.allocations[(batch.provider_id, batch.account_id)]
        if dry_run:
            for task in tasks:
                previews.append({
                    "task_id": task.id,
                    "provider_id": batch.provider_id,
                    "account_id": batch.account_id,
                    "eligibility_key": batch.eligibility_key,
                })
            continue
        for task in tasks:
            try:
                dispatched.append(dispatch(
                    config, queue, task_id=task.id,
                    eligibility_key=batch.eligibility_key,
                    requested_provider=batch.provider_id,
                    trigger="bonus",
                    now_epoch=now,
                    router_call=router_call, activation_call=activation_call,
                ))
            except AmbiguousDispatch as exc:
                errors.append({"task_id": task.id, "kind": "ambiguous", "message": str(exc)})
                # The claim and durable activation lease remain fail-closed because the job may
                # exist. Continue with compatible work on the same account only; doctor requires
                # explicit reconciliation before an account switch.
            except Exception as exc:
                errors.append({"task_id": task.id, "kind": "failed", "message": str(exc)})

    return ScoutReport(
        now, dry_run, plan, tuple(dispatched), tuple(previews),
        lifecycle_errors + tuple(errors), lifecycle_blockers, router_preflight,
        reconciliation, goal_updates, recoveries,
    )
