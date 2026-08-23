"""Per-account usage refresh and normalized, atomic cache reads."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import AccountConfig, AdapterConfig, RuntimeConfig


class AdapterFailure(RuntimeError):
    """A usage adapter failed or returned an unsafe/unusable response."""


_SECRET_DIAGNOSTIC_RE = re.compile(
    r"(?i)(token|secret|password|api[_-]?key|authorization)\s*[=:]\s*([^\s,;]+)"
)


def _redact(value: Any) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")[:1000]
    return _SECRET_DIAGNOSTIC_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)


@dataclass(frozen=True)
class UsageSnapshot:
    provider_id: str
    account_id: str
    captured_at: int
    limits: Mapping[str, Mapping[str, float | int]]
    fresh: bool = True
    error: str | None = None

    def cache_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "captured_at": self.captured_at,
            "limits": {key: dict(value) for key, value in self.limits.items()},
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.cache_dict()
        value["fresh"] = self.fresh
        value["error"] = self.error
        return value


def cache_path(cache_root: str | os.PathLike[str] | Path, provider_id: str, account_id: str) -> Path:
    return Path(cache_root).expanduser().resolve(strict=False) / provider_id / f"{account_id}.json"


def _epoch(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise AdapterFailure(f"{label} must be an epoch")
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            except ValueError as exc:
                raise AdapterFailure(f"{label} must be an epoch") from exc
    raise AdapterFailure(f"{label} must be an epoch")


def normalize_snapshot(
    raw: Any,
    account: AccountConfig,
    *,
    now_epoch: int,
    require_resets: bool = True,
) -> UsageSnapshot:
    if isinstance(raw, UsageSnapshot):
        raw = raw.cache_dict()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterFailure("usage adapter returned invalid JSON") from exc
    if not isinstance(raw, Mapping):
        for attribute in ("data", "json", "output", "stdout"):
            candidate = getattr(raw, attribute, None)
            if candidate is not None:
                return normalize_snapshot(
                    candidate() if callable(candidate) else candidate,
                    account,
                    now_epoch=now_epoch,
                    require_resets=require_resets,
                )
        raise AdapterFailure("usage adapter returned a non-object")

    provider_id = raw.get("provider_id", account.provider_id)
    account_id = raw.get("account_id", account.id)
    if provider_id != account.provider_id or account_id != account.id:
        raise AdapterFailure("usage adapter identity mismatch")
    captured_at = _epoch(raw.get("captured_at", now_epoch), "captured_at")
    raw_limits = raw.get("limits")
    if not isinstance(raw_limits, Mapping):
        raise AdapterFailure("usage adapter omitted limits")
    limits: dict[str, dict[str, float | int]] = {}
    for raw_id, raw_reading in raw_limits.items():
        limit_id = str(raw_id)
        if not isinstance(raw_reading, Mapping):
            raise AdapterFailure(f"limit {limit_id} is not an object")
        used = raw_reading.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(float(used)):
            raise AdapterFailure(f"limit {limit_id} has invalid used_percent")
        used_number = float(used)
        if used_number < 0 or used_number > 100:
            raise AdapterFailure(f"limit {limit_id} used_percent is outside 0..100")
        reset = raw_reading.get("resets_at")
        limits[limit_id] = {"used_percent": used_number}
        if require_resets or reset is not None:
            limits[limit_id]["resets_at"] = _epoch(reset, f"limit {limit_id} resets_at")
    return UsageSnapshot(account.provider_id, account.id, captured_at, limits)


def normalize_resets(
    raw: Any,
    account: AccountConfig,
    *,
    now_epoch: int,
) -> tuple[int, Mapping[str, int]]:
    """Normalize a reset-only adapter response without inventing utilization values."""

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterFailure("reset adapter returned invalid JSON") from exc
    if not isinstance(raw, Mapping):
        for attribute in ("data", "json", "output", "stdout"):
            candidate = getattr(raw, attribute, None)
            if candidate is not None:
                return normalize_resets(
                    candidate() if callable(candidate) else candidate,
                    account,
                    now_epoch=now_epoch,
                )
        raise AdapterFailure("reset adapter returned a non-object")
    if raw.get("provider_id", account.provider_id) != account.provider_id or raw.get("account_id", account.id) != account.id:
        raise AdapterFailure("reset adapter identity mismatch")
    captured_at = _epoch(raw.get("captured_at", now_epoch), "captured_at")
    raw_limits = raw.get("limits")
    if not isinstance(raw_limits, Mapping):
        raise AdapterFailure("reset adapter omitted limits")
    resets: dict[str, int] = {}
    for raw_id, raw_reading in raw_limits.items():
        limit_id = str(raw_id)
        if not isinstance(raw_reading, Mapping):
            raise AdapterFailure(f"reset limit {limit_id} is not an object")
        resets[limit_id] = _epoch(
            raw_reading.get("resets_at"), f"reset limit {limit_id} resets_at"
        )
    return captured_at, resets


def _atomic_write(path: Path, snapshot: UsageSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        payload = (json.dumps(snapshot.cache_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_cached(
    cache_root: str | os.PathLike[str] | Path,
    account: AccountConfig,
    *,
    now_epoch: int | None = None,
    max_age_seconds: int = 3_600,
) -> UsageSnapshot:
    now = int(time.time() if now_epoch is None else now_epoch)
    path = cache_path(cache_root, account.provider_id, account.id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        snapshot = normalize_snapshot(raw, account, now_epoch=now)
    except (OSError, UnicodeError, json.JSONDecodeError, AdapterFailure) as exc:
        return UsageSnapshot(account.provider_id, account.id, 0, {}, fresh=False, error=_redact(exc))
    if snapshot.captured_at > now + 300:
        return UsageSnapshot(
            snapshot.provider_id, snapshot.account_id, snapshot.captured_at, snapshot.limits,
            fresh=False, error="cache timestamp is in the future",
        )
    if now - snapshot.captured_at > max_age_seconds:
        return UsageSnapshot(
            snapshot.provider_id, snapshot.account_id, snapshot.captured_at, snapshot.limits,
            fresh=False, error="stale usage cache",
        )
    return snapshot


def read_all(
    config: RuntimeConfig,
    cache_root: str | os.PathLike[str] | Path | None = None,
    *,
    now_epoch: int | None = None,
) -> dict[tuple[str, str], UsageSnapshot]:
    root = Path(cache_root) if cache_root is not None else config.usage_cache_dir
    return {
        (account.provider_id, account.id): read_cached(
            root, account, now_epoch=now_epoch, max_age_seconds=config.usage_max_age_seconds,
        )
        for account in config.accounts
    }


def read_cache(
    config: RuntimeConfig,
    cache_root: str | os.PathLike[str] | Path,
    provider_id: str,
    account_id: str,
    *,
    now_epoch: int | None = None,
) -> UsageSnapshot:
    """Compatibility-shaped single cache read used by both viewer implementations."""

    account = config.account(account_id)
    if account.provider_id != provider_id:
        return UsageSnapshot(provider_id, account_id, 0, {}, fresh=False, error="cache identity mismatch")
    return read_cached(
        cache_root, account, now_epoch=now_epoch,
        max_age_seconds=config.usage_max_age_seconds,
    )


load_cached = read_cache


def _default_adapter_call(
    adapter: AdapterConfig,
    variables: Mapping[str, str],
    *,
    config: RuntimeConfig,
) -> Any:
    try:
        from . import adapters  # type: ignore
    except ImportError as exc:  # pragma: no cover - packaging always includes adapters
        raise AdapterFailure("adapter runtime is unavailable") from exc
    # execute_adapter has one stable signature.  Do not catch TypeError here: a TypeError from
    # inside a successfully started adapter call is a real failure, and retrying would execute a
    # foreground adapter twice.
    return adapters.execute_adapter(adapter, variables=variables, config=config)


def refresh_all(
    config: RuntimeConfig,
    cache_root: str | os.PathLike[str] | Path | None = None,
    *,
    adapter_call: Callable[..., Any] | None = None,
    now_epoch: int | None = None,
) -> dict[tuple[str, str], UsageSnapshot]:
    """Refresh every configured account independently.

    Activation is intentionally absent: reading usage does not require or mutate the active
    account.  One failing adapter leaves its last good file intact and cannot suppress siblings.
    """

    now = int(time.time() if now_epoch is None else now_epoch)
    root = Path(cache_root) if cache_root is not None else config.usage_cache_dir
    results: dict[tuple[str, str], UsageSnapshot] = {}
    for account in config.accounts:
        key = (account.provider_id, account.id)
        if not account.usage_adapter_id:
            existing = read_cached(
                root, account, now_epoch=now, max_age_seconds=config.usage_max_age_seconds,
            )
            results[key] = UsageSnapshot(
                existing.provider_id, existing.account_id, existing.captured_at, existing.limits,
                fresh=False, error="account has no usage adapter",
            )
            continue
        adapter = config.adapter(account.usage_adapter_id)
        variables = {
            "provider_id": account.provider_id,
            "account_id": account.id,
            "cache_path": str(cache_path(root, account.provider_id, account.id)),
        }
        try:
            if adapter_call is None:
                raw = _default_adapter_call(adapter, variables, config=config)
            else:
                raw = adapter_call(adapter, variables, config=config)
            expected_ids = {item.id for item in config.limits_for_plan(account.plan_id)}
            snapshot = normalize_snapshot(
                raw, account, now_epoch=now,
                require_resets=account.reset_adapter_id is None,
            )
            missing_usage = expected_ids - set(snapshot.limits)
            if missing_usage:
                raise AdapterFailure(
                    f"usage adapter omitted configured limit {sorted(missing_usage)[0]}"
                )
            if account.reset_adapter_id is not None:
                reset_adapter = config.adapter(account.reset_adapter_id)
                if adapter_call is None:
                    reset_raw = _default_adapter_call(reset_adapter, variables, config=config)
                else:
                    reset_raw = adapter_call(reset_adapter, variables, config=config)
                reset_captured, resets = normalize_resets(reset_raw, account, now_epoch=now)
                missing_resets = expected_ids - set(resets)
                if missing_resets:
                    raise AdapterFailure(
                        f"reset adapter omitted configured limit {sorted(missing_resets)[0]}"
                    )
                merged = {
                    limit_id: {
                        "used_percent": snapshot.limits[limit_id]["used_percent"],
                        "resets_at": resets[limit_id],
                    }
                    for limit_id in expected_ids
                }
                snapshot = UsageSnapshot(
                    account.provider_id, account.id,
                    min(snapshot.captured_at, reset_captured), merged,
                )
            _atomic_write(cache_path(root, account.provider_id, account.id), snapshot)
            results[key] = snapshot
        except Exception as exc:  # an account failure is data, never a sibling-killing exception
            existing = read_cached(
                root, account, now_epoch=now, max_age_seconds=config.usage_max_age_seconds,
            )
            results[key] = UsageSnapshot(
                existing.provider_id, existing.account_id, existing.captured_at, existing.limits,
                fresh=False, error=_redact(exc),
            )
    return results


def legacy_window_line(
    config: RuntimeConfig,
    snapshot: UsageSnapshot,
) -> str:
    """Render the historical ``5h reset weekly reset`` line without provider branches."""

    account = config.account(snapshot.account_id)
    limits = config.limits_for_plan(account.plan_id)
    ordered = sorted(limits, key=lambda item: (item.window_seconds, item.id))
    short = ordered[0] if len(ordered) > 1 else None
    long = ordered[-1] if ordered else None

    def reading(limit_id: str | None) -> tuple[float | int, int]:
        if not limit_id or limit_id not in snapshot.limits:
            return 0, 0
        item = snapshot.limits[limit_id]
        used = item.get("used_percent", 0)
        used_value: float | int = int(used) if float(used).is_integer() else float(used)
        return used_value, int(item.get("resets_at", 0))

    short_used, short_reset = reading(short.id if short else None)
    long_used, long_reset = reading(long.id if long else None)
    return f"{short_used} {short_reset} {long_used} {long_reset}"
