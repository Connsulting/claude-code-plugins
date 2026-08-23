"""Cache-only scout orchestration."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .config import RuntimeConfig
from .db import QueueDB, hour_round
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


def _cycle_anchor(config: RuntimeConfig, snapshots: dict[Any, Any], now_epoch: int) -> int:
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
    snapshots = read_all(config, cache_root, now_epoch=now)
    anchor = _cycle_anchor(config, snapshots, now)
    eligible = queue.count_eligible(anchor)
    plan = build_plan(config, snapshots, eligible_count=eligible, now_epoch=now)
    dispatched: list[DispatchResult] = []
    previews: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    previewed: set[str] = set()

    for batch in plan.batches:  # already nearest-reset-first
        provider = config.provider(batch.provider_id)
        cycle = batch.resets_at
        tasks = queue.eligible_tasks(
            cycle, provider_id=provider.id, capabilities=provider.capabilities,
            limit=batch.batch_size,
        )
        if dry_run:
            for task in tasks:
                if task.id in previewed:
                    continue
                previews.append({
                    "task_id": task.id,
                    "provider_id": batch.provider_id,
                    "account_id": batch.account_id,
                    "eligibility_key": batch.eligibility_key,
                })
                previewed.add(task.id)
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
                # The activation has been released but the task remains claimed. Continue with
                # unrelated tasks/accounts; doctor will require explicit reconciliation.
            except Exception as exc:
                errors.append({"task_id": task.id, "kind": "failed", "message": str(exc)})

    return ScoutReport(now, dry_run, plan, tuple(dispatched), tuple(previews), tuple(errors))
