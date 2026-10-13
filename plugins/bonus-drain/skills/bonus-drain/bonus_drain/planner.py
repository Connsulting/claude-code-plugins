"""Fail-closed capacity planning for proxy-owned provider pools."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

from .config import LimitConfig, RuntimeConfig
from .usage import ProxyAccount


@dataclass(frozen=True)
class PlanBatch:
    provider_id: str
    account_id: str
    batch_size: int
    resets_at: int
    eligibility_key: str
    limit_ids: tuple[str, ...]
    surplus: float = 0.0
    surplus_jobs: int = 0
    urgent: bool = False
    unmetered: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["limit_ids"] = list(self.limit_ids)
        if self.unmetered:
            value["resets_at"] = None
        return value


@dataclass(frozen=True)
class GateDecision:
    provider_id: str
    account_id: str
    open: bool
    reason: str | None
    batch_size: int
    resets_at: int | None
    eligibility_key: str | None
    limit_ids: tuple[str, ...]
    urgent: bool = False
    unmetered: bool = False
    surplus: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["limit_ids"] = list(self.limit_ids)
        if self.unmetered:
            value["resets_at"] = None
        return value


@dataclass(frozen=True)
class PlanResult:
    batches: tuple[PlanBatch, ...]
    closed: Mapping[tuple[str, str], str]
    gates: tuple[GateDecision, ...]
    generated_at: int
    subscriptions: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "subscriptions": [dict(subscription) for subscription in self.subscriptions],
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


def _limit_reading(limit: LimitConfig, reading: Any, now_epoch: int) -> tuple[float | None, int | None, str | None]:
    if not isinstance(reading, Mapping):
        return None, None, f"missing limit reading: {limit.id}"
    used = _finite_number(reading.get("used_percent"), minimum=0, maximum=100)
    if used is None:
        return None, None, f"invalid usage for limit {limit.id}"
    reset_value = reading.get("resets_at")
    if (isinstance(reset_value, bool) or not isinstance(reset_value, (int, float))
            or not math.isfinite(float(reset_value))):
        return used, None, f"invalid reset for limit {limit.id}"
    reset = int(reset_value)
    if reset <= now_epoch:
        return used, reset, f"expired or missing reset for limit {limit.id}"
    if reset - now_epoch > limit.lead_seconds:
        return used, reset, f"before lead window for limit {limit.id}"
    return used, reset, None


def _surplus_above_floor(limit: LimitConfig, used: float, remaining_seconds: int) -> float:
    """Weekly-percent points sitting above the remaining-headroom floor."""

    reserve = 0.0
    if limit.max_percent_per_window > 0:
        reserve = min(
            limit.ceiling_percent,
            limit.max_percent_per_window * (remaining_seconds / limit.pacing_window_seconds),
        )
    return limit.ceiling_percent - used - reserve


def _is_urgent(reset: int | None, now_epoch: int, urgency_seconds: int) -> bool:
    return bool(reset is not None and urgency_seconds > 0 and 0 < reset - now_epoch <= urgency_seconds)


def close_providers(plan: PlanResult, failures: Mapping[str, str]) -> PlanResult:
    """Close provider pools without changing their subscription evidence."""
    if not failures:
        return plan
    closed = dict(plan.closed)
    gates = []
    for gate in plan.gates:
        reason = failures.get(gate.provider_id)
        if reason is None:
            gates.append(gate)
        else:
            closed[(gate.provider_id, gate.account_id)] = reason
            gates.append(replace(gate, open=False, reason=reason, batch_size=0, urgent=False))
    return replace(
        plan,
        batches=tuple(batch for batch in plan.batches if batch.provider_id not in failures),
        closed=closed, gates=tuple(gates),
    )


def finalize_plan(
    config: RuntimeConfig, plan: PlanResult, *, eligible_count: int | Mapping[Any, int],
) -> PlanResult:
    """Order provider batches and allocate any shared scalar queue."""
    selected = sorted(plan.batches, key=lambda batch: (batch.unmetered, batch.resets_at, batch.provider_id))
    if isinstance(eligible_count, Mapping):
        return replace(plan, batches=tuple(selected))
    remaining = max(0, int(eligible_count))
    allocated = []
    for batch in selected:
        size = min(batch.batch_size, remaining)
        if size:
            allocated.append(replace(batch, batch_size=size))
            remaining -= size
    return replace(plan, batches=tuple(allocated))


def accounts_from_snapshots(config: RuntimeConfig, snapshots: Mapping[Any, Any]) -> tuple[ProxyAccount, ...]:
    configured = {provider.id for provider in config.providers}
    return tuple(ProxyAccount(_field(snapshot, "account_id"), _field(snapshot, "provider_id"),
                              bool(_field(snapshot, "unmetered", False)))
                 for snapshot in snapshots.values() if _field(snapshot, "provider_id") in configured)


def limit_window(limit: LimitConfig) -> str:
    return "seven_day" if limit.window_seconds == 604800 else "five_hour"


def build_plan(
    config: RuntimeConfig,
    snapshots: Mapping[Any, Any],
    *,
    eligible_count: int | Mapping[Any, int],
    now_epoch: int | None = None,
) -> PlanResult:
    """Build one gate and batch per provider using every enabled subscription."""
    now = int(time.time() if now_epoch is None else now_epoch)
    batches: list[PlanBatch] = []
    gates: list[GateDecision] = []
    closed: dict[tuple[str, str], str] = {}
    subscriptions: list[dict[str, Any]] = []
    pools: dict[str, list[tuple[Any, Any, dict[str, Any]]]] = {}
    for storage_key, snapshot in snapshots.items():
        original = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
        original["pacing"] = {}
        subscriptions.append(original)
        pools.setdefault(_field(snapshot, "provider_id"), []).append((storage_key, snapshot, original))

    for provider in config.providers:
        pool = pools.get(provider.id, [])
        key = (provider.id, provider.id)
        limits = config.limits_for_provider(provider.id)
        unmetered = bool(pool) and all(_field(snapshot, "unmetered", False) for _, snapshot, _ in pool)
        reason = None if pool else "no proxy subscriptions"
        if pool and any(_field(snapshot, "unmetered", False) for _, snapshot, _ in pool) and not unmetered:
            reason = "mixed metered and unmetered subscriptions"
        if pool and not unmetered and not limits:
            reason = reason or "provider has no limits"
        resets: list[tuple[int, str]] = []
        surpluses: dict[str, list[float]] = {limit.id: [] for limit in limits}
        seen = set()
        for storage_key, snapshot, original in pool:
            account_id = _field(snapshot, "account_id")
            if not isinstance(account_id, str) or not account_id or account_id in seen:
                reason = reason or "cache identity mismatch"
            seen.add(account_id)
            if isinstance(storage_key, tuple) and storage_key != (provider.id, account_id):
                reason = reason or "cache identity mismatch"
            if _field(snapshot, "fresh", True) is False or _field(snapshot, "error"):
                reason = reason or str(_field(snapshot, "error") or "stale usage cache")
            captured = _field(snapshot, "captured_at")
            if (isinstance(captured, bool) or not isinstance(captured, (int, float))
                    or not math.isfinite(float(captured))):
                reason = reason or "invalid cache timestamp"
            elif int(captured) > now + 300:
                reason = reason or "cache timestamp is in the future"
            elif now - int(captured) > config.usage_max_age_seconds:
                reason = reason or "stale usage cache"
            if unmetered:
                continue
            readings = _usage_limits(snapshot)
            for limit in limits:
                used, reset, limit_reason = _limit_reading(limit, readings.get(limit_window(limit)), now)
                pacing = {"ceiling_percent": limit.ceiling_percent, "used_percent": used,
                          "resets_at": reset, "reserve": None, "surplus": None}
                original["pacing"][limit.id] = pacing
                if reset is not None:
                    resets.append((reset, limit.id))
                if used is not None and reset is not None:
                    surplus = _surplus_above_floor(limit, used, max(0, reset - now))
                    pacing["reserve"] = limit.ceiling_percent - used - surplus
                    pacing["surplus"] = surplus
                    surpluses[limit.id].append(surplus)
                reason = reason or limit_reason

        nearest_reset, nearest_limit = min(resets, default=(2**63 - 1, "unmetered"))
        surplus_jobs = config.unmetered_max_inflight if unmetered else 0
        surplus_mean = 0.0
        if reason is None and not unmetered:
            budgets = []
            for limit in limits:
                values = surpluses[limit.id]
                total = math.fsum(values)
                mean = total / len(pool)
                if mean <= 0:
                    reason = f"at reserve target for limit {limit.id}"
                    surplus_mean = mean
                    break
                allowed = limit.batch_size
                if limit.estimated_percent_per_job is not None:
                    allowed = min(allowed, math.floor(total / limit.estimated_percent_per_job))
                    if allowed <= 0:
                        reason = f"remaining budget is below one job estimate for limit {limit.id}"
                elif limit.max_percent_per_window > 0:
                    reason = f"cannot enforce reserve without a job estimate for limit {limit.id}"
                budgets.append((allowed, mean, limit.id))
                if reason:
                    surplus_mean = mean
                    break
            if reason is None:
                surplus_jobs, surplus_mean, _restricting_limit = min(budgets)
        available = _available(eligible_count, provider.id, provider.id)
        if reason is None and available <= 0:
            reason = "no eligible tasks"
        reset_for_gate = None if not resets else nearest_reset
        limit_ids = () if unmetered else tuple(limit.id for limit in limits)
        if reason is not None:
            closed[key] = reason
            gates.append(GateDecision(
                provider.id, provider.id, False, reason, 0, reset_for_gate, None,
                limit_ids, unmetered=unmetered, surplus=surplus_mean,
            ))
            continue
        size = min(available, surplus_jobs)
        cycle = now // 3600 * 3600 if unmetered else nearest_reset
        eligibility_key = f"{provider.id}/{nearest_limit}/{cycle}"
        urgent = not unmetered and _is_urgent(
            nearest_reset, now, max((limit.urgency_seconds for limit in limits), default=0),
        )
        batches.append(PlanBatch(
            provider.id, provider.id, size, nearest_reset, eligibility_key, limit_ids,
            surplus_mean, surplus_jobs, urgent, unmetered,
        ))
        gates.append(GateDecision(
            provider.id, provider.id, True, None, size, nearest_reset, eligibility_key,
            limit_ids, urgent, unmetered, surplus_mean,
        ))
    batches.sort(key=lambda batch: (batch.unmetered, batch.resets_at, batch.provider_id))
    return PlanResult(tuple(batches), closed, tuple(gates), now, tuple(subscriptions))


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
