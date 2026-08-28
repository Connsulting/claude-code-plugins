"""Cache-only scout orchestration."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import RuntimeConfig
from .db import QueueDB, hour_round, task_requires_legacy_exclusive
from .dispatcher import (
    AmbiguousDispatch,
    DispatchResult,
    dispatch,
)
from .planner import PlanResult, build_plan
from .usage import read_all


@dataclass(frozen=True)
class ScoutReport:
    generated_at: int
    dry_run: bool
    plan: PlanResult
    dispatched: tuple[DispatchResult, ...]
    previews: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "dry_run": self.dry_run,
            "plan": self.plan.to_dict(),
            "dispatched": [item.to_dict() for item in self.dispatched],
            "previews": list(self.previews),
            "errors": list(self.errors),
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


def plan_tick(
    config: RuntimeConfig,
    queue: QueueDB,
    cache_root: str | Path | None = None,
    *,
    now_epoch: int | None = None,
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
            capabilities=provider.capabilities,
        )
    plan = build_plan(config, snapshots, eligible_count=availability, now_epoch=now)
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
            capabilities=provider.capabilities,
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

    def augment(task_id: str, seen_slots: set[int], seen_tasks: set[str]) -> bool:
        if task_id in seen_tasks:
            return False
        seen_tasks.add(task_id)
        for slot in task_slots.get(task_id, ()):
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
    queue = queue or QueueDB(config.database)
    queue.initialize()
    tick = plan_tick(config, queue, cache_root, now_epoch=now)
    plan = tick.plan
    dispatched: list[DispatchResult] = []
    previews: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

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
                    router_call=router_call, activation_call=activation_call,
                ))
            except AmbiguousDispatch as exc:
                errors.append({"task_id": task.id, "kind": "ambiguous", "message": str(exc)})
                # The claim and durable activation lease remain fail-closed because the job may
                # exist. Continue with compatible work on the same account only; doctor requires
                # explicit reconciliation before an account switch.
            except Exception as exc:
                errors.append({"task_id": task.id, "kind": "failed", "message": str(exc)})

    return ScoutReport(now, dry_run, plan, tuple(dispatched), tuple(previews), tuple(errors))
