"""Independent, fail-closed capacity planning for arbitrary providers and accounts."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .config import AccountConfig, LimitConfig, RuntimeConfig


@dataclass(frozen=True)
class PlanBatch:
    provider_id: str
    account_id: str
    plan_id: str
    batch_size: int
    resets_at: int
    eligibility_key: str
    limit_ids: tuple[str, ...]
    surplus: float = 0.0
    surplus_jobs: int = 0
    urgent: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["limit_ids"] = list(self.limit_ids)
        return value


@dataclass(frozen=True)
class GateDecision:
    provider_id: str
    account_id: str
    plan_id: str
    open: bool
    reason: str | None
    batch_size: int
    resets_at: int | None
    eligibility_key: str | None
    limit_ids: tuple[str, ...]
    urgent: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["limit_ids"] = list(self.limit_ids)
        return value


@dataclass(frozen=True)
class PlanResult:
    batches: tuple[PlanBatch, ...]
    closed: Mapping[tuple[str, str], str]
    gates: tuple[GateDecision, ...]
    generated_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "batches": [batch.to_dict() for batch in self.batches],
            "gates": [gate.to_dict() for gate in self.gates],
            "closed": [
                {"provider_id": provider, "account_id": account, "reason": reason}
                for (provider, account), reason in sorted(self.closed.items())
            ],
        }


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _snapshot_for(snapshots: Mapping[Any, Any], account: AccountConfig) -> Any:
    return snapshots.get((account.provider_id, account.id)) or snapshots.get(account.id)


def _usage_limits(snapshot: Any) -> Mapping[str, Any]:
    value = _field(snapshot, "limits", {})
    return value if isinstance(value, Mapping) else {}


def _finite_number(value: Any, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        return None
    return number


def _available(eligible_count: int | Mapping[Any, int], provider_id: str, account_id: str) -> int:
    if isinstance(eligible_count, Mapping):
        for key in ((provider_id, account_id), account_id, provider_id, "total"):
            if key in eligible_count:
                try:
                    return max(0, int(eligible_count[key]))
                except (TypeError, ValueError):
                    return 0
        return 0
    try:
        return max(0, int(eligible_count))
    except (TypeError, ValueError):
        return 0


def _limit_admission(limit: LimitConfig, reading: Any, now_epoch: int) -> tuple[int, int, str | None]:
    if not isinstance(reading, Mapping):
        return 0, 0, f"missing limit reading: {limit.id}"
    used = _finite_number(reading.get("used_percent"), minimum=0, maximum=100)
    if used is None:
        return 0, 0, f"invalid usage for limit {limit.id}"
    reset_value = reading.get("resets_at")
    if isinstance(reset_value, bool) or not isinstance(reset_value, (int, float)):
        return 0, 0, f"invalid reset for limit {limit.id}"
    reset = int(reset_value)
    if reset <= now_epoch:
        return 0, reset, f"expired or missing reset for limit {limit.id}"
    remaining = reset - now_epoch
    if remaining > limit.lead_seconds:
        return 0, reset, f"before lead window for limit {limit.id}"
    if used >= limit.ceiling_percent:
        return 0, reset, f"at ceiling for limit {limit.id}"

    drainable = _surplus_above_floor(limit, used, remaining)
    if drainable <= 0:
        return 0, reset, f"at reserve target for limit {limit.id}"

    allowed = limit.batch_size
    if limit.estimated_percent_per_job is not None:
        allowed = min(allowed, math.floor(drainable / limit.estimated_percent_per_job))
        if allowed <= 0:
            return 0, reset, f"remaining budget is below one job estimate for limit {limit.id}"
    elif limit.max_percent_per_window > 0:
        # A remaining-headroom floor is a hard safety boundary. Without a job-cost
        # estimate, one launch could cross it, so missing accounting is not spare
        # capacity.
        return 0, reset, f"cannot enforce reserve without a job estimate for limit {limit.id}"
    return allowed, reset, None


def _surplus_above_floor(limit: LimitConfig, used: float, remaining_seconds: int) -> float:
    """Weekly-percent points sitting above the remaining-headroom floor."""

    reserve = 0.0
    if limit.max_percent_per_window > 0:
        reserve = min(
            limit.ceiling_percent,
            limit.max_percent_per_window * (remaining_seconds / limit.pacing_window_seconds),
        )
    return limit.ceiling_percent - used - reserve


def _account_urgency_seconds(config: RuntimeConfig, account: AccountConfig) -> int:
    limits = config.limits_for_plan(account.plan_id)
    return max((limit.urgency_seconds for limit in limits), default=0) if limits else 0


def _is_urgent(reset: int | None, now_epoch: int, urgency_seconds: int) -> bool:
    if reset is None or urgency_seconds <= 0:
        return False
    remaining = reset - now_epoch
    return 0 < remaining <= urgency_seconds


def preferred_provider_batches(
    config: RuntimeConfig,
    plan: PlanResult,
    active_account_ids: Mapping[str, str],
) -> tuple[PlanBatch, ...]:
    """Choose each provider's preferred open account without closing siblings."""

    batches_by_provider: dict[str, list[PlanBatch]] = {}
    for batch in plan.batches:
        batches_by_provider.setdefault(batch.provider_id, []).append(batch)

    preferred: list[PlanBatch] = []
    for provider in config.providers:
        open_batches = batches_by_provider.get(provider.id, [])
        if not open_batches:
            continue
        active_account_id = active_account_ids.get(provider.id)
        active = next(
            (batch for batch in open_batches if batch.account_id == active_account_id),
            None,
        )
        if active is not None:
            preferred.append(active)
            continue
        urgent = [batch for batch in open_batches if batch.urgent]
        if urgent:
            preferred.append(min(
                urgent,
                key=lambda batch: (batch.resets_at, batch.account_id),
            ))
            continue
        preferred.append(max(
            open_batches,
            key=lambda batch: (batch.surplus, -batch.resets_at, batch.account_id),
        ))
    return tuple(preferred)


def close_providers(
    plan: PlanResult,
    failures: Mapping[str, str],
) -> PlanResult:
    """Close every account candidate for providers with unresolved identity."""

    if not failures:
        return plan
    closed = dict(plan.closed)
    gates = []
    for gate in plan.gates:
        reason = failures.get(gate.provider_id)
        if reason is None:
            gates.append(gate)
            continue
        key = (gate.provider_id, gate.account_id)
        closed[key] = reason
        gates.append(GateDecision(
            gate.provider_id, gate.account_id, gate.plan_id, False, reason, 0,
            gate.resets_at, gate.eligibility_key, gate.limit_ids, False,
        ))
    batches = tuple(
        batch for batch in plan.batches if batch.provider_id not in failures
    )
    return PlanResult(batches, closed, tuple(gates), plan.generated_at)


def finalize_plan(
    config: RuntimeConfig,
    plan: PlanResult,
    *,
    active_account_ids: Mapping[str, str],
    eligible_count: int | Mapping[Any, int],
) -> PlanResult:
    """Select one account per provider, then allocate any shared scalar queue."""

    winners = preferred_provider_batches(config, plan, active_account_ids)
    winner_by_provider = {batch.provider_id: batch for batch in winners}
    closed = dict(plan.closed)
    gates_by_key = {
        (gate.provider_id, gate.account_id): gate for gate in plan.gates
    }
    selected: list[PlanBatch] = []
    for batch in plan.batches:
        winner = winner_by_provider.get(batch.provider_id)
        if winner is None:
            continue
        if batch.account_id == winner.account_id:
            selected.append(batch)
            continue
        key = (batch.provider_id, batch.account_id)
        reason = f"queued behind {winner.account_id}"
        closed[key] = reason
        gate = gates_by_key[key]
        gates_by_key[key] = GateDecision(
            gate.provider_id, gate.account_id, gate.plan_id, False, reason, 0,
            gate.resets_at, gate.eligibility_key, gate.limit_ids, False,
        )

    selected.sort(key=lambda batch: (batch.resets_at, batch.provider_id, batch.account_id))
    if not isinstance(eligible_count, Mapping):
        remaining = max(0, int(eligible_count))
        allocated: list[PlanBatch] = []
        for batch in selected:
            size = min(batch.batch_size, remaining)
            key = (batch.provider_id, batch.account_id)
            if size <= 0:
                reason = "no eligible tasks remain after nearer resets"
                closed[key] = reason
                gate = gates_by_key[key]
                gates_by_key[key] = GateDecision(
                    gate.provider_id, gate.account_id, gate.plan_id, False, reason, 0,
                    gate.resets_at, gate.eligibility_key, gate.limit_ids, gate.urgent,
                )
                continue
            if size != batch.batch_size:
                batch = PlanBatch(
                    batch.provider_id, batch.account_id, batch.plan_id, size,
                    batch.resets_at, batch.eligibility_key, batch.limit_ids,
                    batch.surplus, batch.surplus_jobs, batch.urgent,
                )
                gate = gates_by_key[key]
                gates_by_key[key] = GateDecision(
                    gate.provider_id, gate.account_id, gate.plan_id, True, None, size,
                    gate.resets_at, gate.eligibility_key, gate.limit_ids, gate.urgent,
                )
            allocated.append(batch)
            remaining -= size
        selected = allocated

    gates = tuple(
        gates_by_key[(account.provider_id, account.id)] for account in config.accounts
    )
    return PlanResult(tuple(selected), closed, gates, plan.generated_at)


def build_plan(
    config: RuntimeConfig,
    snapshots: Mapping[Any, Any],
    *,
    eligible_count: int | Mapping[Any, int],
    now_epoch: int | None = None,
) -> PlanResult:
    """Evaluate each configured account independently and order open batches by reset.

    No missing or malformed reading is interpreted as zero usage.  It closes only the
    affected account; siblings continue to be considered.
    """

    now = int(time.time() if now_epoch is None else now_epoch)
    provisional: list[PlanBatch] = []
    closed: dict[tuple[str, str], str] = {}
    gates_by_key: dict[tuple[str, str], GateDecision] = {}

    for account in config.accounts:
        key = (account.provider_id, account.id)
        snapshot = _snapshot_for(snapshots, account)
        reason: str | None = None
        if snapshot is None:
            reason = "missing usage cache"
        elif _field(snapshot, "fresh", True) is False:
            reason = str(_field(snapshot, "error", None) or "stale usage cache")
        else:
            captured = _field(snapshot, "captured_at")
            if isinstance(captured, bool) or not isinstance(captured, (int, float)):
                reason = "invalid cache timestamp"
            elif int(captured) > now + 300:
                reason = "cache timestamp is in the future"
            elif now - int(captured) > config.usage_max_age_seconds:
                reason = "stale usage cache"
            elif _field(snapshot, "provider_id") != account.provider_id or _field(snapshot, "account_id") != account.id:
                reason = "cache identity mismatch"

        limits = config.limits_for_plan(account.plan_id)
        if reason is None and not limits:
            reason = "plan has no limits"
        batch_sizes: list[int] = []
        resets: list[tuple[int, str]] = []
        surpluses: list[float] = []
        if reason is None:
            readings = _usage_limits(snapshot)
            for limit in limits:
                allowed, reset, limit_reason = _limit_admission(limit, readings.get(limit.id), now)
                if reset:
                    resets.append((reset, limit.id))
                if limit_reason:
                    reason = limit_reason
                    break
                reading = readings.get(limit.id)
                used = _finite_number(
                    reading.get("used_percent") if isinstance(reading, Mapping) else None,
                    minimum=0, maximum=100,
                ) or 0.0
                batch_sizes.append(allowed)
                surpluses.append(_surplus_above_floor(limit, used, reset - now))

        available = _available(eligible_count, account.provider_id, account.id)
        if reason is None and available <= 0:
            reason = "no eligible tasks"
        if reason is not None:
            closed[key] = reason
            gates_by_key[key] = GateDecision(
                account.provider_id, account.id, account.plan_id, False, reason, 0,
                min((item[0] for item in resets), default=None), None,
                tuple(limit.id for limit in limits),
            )
            continue

        nearest_reset, nearest_limit = min(resets, key=lambda item: (item[0], item[1]))
        surplus_jobs = min(batch_sizes)
        size = min(available, surplus_jobs)
        eligibility_key = f"{account.id}/{nearest_limit}/{nearest_reset}"
        urgent = _is_urgent(
            nearest_reset, now, _account_urgency_seconds(config, account),
        )
        batch = PlanBatch(
            account.provider_id, account.id, account.plan_id, size, nearest_reset,
            eligibility_key, tuple(limit.id for limit in limits),
            min(surpluses) if surpluses else 0.0,
            surplus_jobs,
            urgent,
        )
        provisional.append(batch)
        gates_by_key[key] = GateDecision(
            account.provider_id, account.id, account.plan_id, True, None, size,
            nearest_reset, eligibility_key, tuple(limit.id for limit in limits), urgent,
        )

    account_order = {
        (account.provider_id, account.id): index
        for index, account in enumerate(config.accounts)
    }
    provisional.sort(
        key=lambda batch: (batch.resets_at, account_order[(batch.provider_id, batch.account_id)]),
    )

    gates = tuple(gates_by_key[(account.provider_id, account.id)] for account in config.accounts)
    return PlanResult(tuple(provisional), closed, gates, now)


def build_gates(
    config: RuntimeConfig,
    snapshots: Mapping[Any, Any],
    *,
    eligible_count: int | Mapping[Any, int],
    now_epoch: int | None = None,
) -> tuple[GateDecision, ...]:
    return build_plan(
        config, snapshots, eligible_count=eligible_count, now_epoch=now_epoch,
    ).gates
