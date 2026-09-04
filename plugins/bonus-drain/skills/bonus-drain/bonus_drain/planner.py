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

    allowed = limit.batch_size
    return allowed, reset, None


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
        if reason is None:
            readings = _usage_limits(snapshot)
            for limit in limits:
                allowed, reset, limit_reason = _limit_admission(limit, readings.get(limit.id), now)
                if limit_reason:
                    reason = limit_reason
                    break
                batch_sizes.append(allowed)
                resets.append((reset, limit.id))

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
        size = min(available, min(batch_sizes))
        eligibility_key = f"{account.id}/{nearest_limit}/{nearest_reset}"
        batch = PlanBatch(
            account.provider_id, account.id, account.plan_id, size, nearest_reset,
            eligibility_key, tuple(limit.id for limit in limits),
        )
        provisional.append(batch)
        gates_by_key[key] = GateDecision(
            account.provider_id, account.id, account.plan_id, True, None, size,
            nearest_reset, eligibility_key, tuple(limit.id for limit in limits),
        )

    provisional.sort(key=lambda batch: (batch.resets_at, batch.provider_id, batch.account_id))

    # A scalar count describes one shared portable queue.  Allocate it in the same
    # nearest-reset order that dispatch will use so the plan never promises duplicate slots.
    if not isinstance(eligible_count, Mapping):
        remaining = max(0, int(eligible_count))
        allocated: list[PlanBatch] = []
        for batch in provisional:
            size = min(batch.batch_size, remaining)
            if size <= 0:
                key = (batch.provider_id, batch.account_id)
                closed[key] = "no eligible tasks remain after nearer resets"
                gates_by_key[key] = GateDecision(
                    batch.provider_id, batch.account_id, batch.plan_id, False, closed[key], 0,
                    batch.resets_at, batch.eligibility_key, batch.limit_ids,
                )
                continue
            allocated.append(PlanBatch(
                batch.provider_id, batch.account_id, batch.plan_id, size, batch.resets_at,
                batch.eligibility_key, batch.limit_ids,
            ))
            if size != batch.batch_size:
                key = (batch.provider_id, batch.account_id)
                gates_by_key[key] = GateDecision(
                    batch.provider_id, batch.account_id, batch.plan_id, True, None, size,
                    batch.resets_at, batch.eligibility_key, batch.limit_ids,
                )
            remaining -= size
        provisional = allocated

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
