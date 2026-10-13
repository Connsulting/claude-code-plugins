"""Per-account usage refresh and normalized, atomic cache reads."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import RuntimeConfig, _identifier


class UsageError(RuntimeError):
    """Usage acquisition or cached data is unusable."""


@dataclass(frozen=True)
class ProxyAccount:
    id: str
    provider_id: str
    unmetered: bool = False


@dataclass(frozen=True)
class UsageSnapshot:
    provider_id: str
    account_id: str
    captured_at: int
    limits: Mapping[str, Mapping[str, float | int]]
    fresh: bool = True
    error: str | None = None
    unmetered: bool = False

    def cache_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "captured_at": self.captured_at,
            "limits": {key: dict(value) for key, value in self.limits.items()},
            "unmetered": self.unmetered,
            "error": self.error,
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
        raise UsageError(f"{label} must be an epoch")
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            except ValueError as exc:
                raise UsageError(f"{label} must be an epoch") from exc
    raise UsageError(f"{label} must be an epoch")


def normalize_snapshot(
    raw: Any,
    account: ProxyAccount,
    *,
    now_epoch: int,
) -> UsageSnapshot:
    if isinstance(raw, UsageSnapshot):
        raw = raw.cache_dict()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UsageError("usage cache returned invalid JSON") from exc
    if not isinstance(raw, Mapping):
        raise UsageError("usage cache returned a non-object")

    if "provider_id" not in raw:
        raise UsageError("usage cache omitted provider_id")
    if "account_id" not in raw:
        raise UsageError("usage cache omitted account_id")
    if "captured_at" not in raw:
        raise UsageError("usage cache omitted captured_at")
    provider_id = raw.get("provider_id")
    account_id = raw.get("account_id")
    if provider_id != account.provider_id or account_id != account.id:
        raise UsageError("usage cache identity mismatch")
    captured_at = _epoch(raw.get("captured_at"), "captured_at")
    raw_limits = raw.get("limits")
    if not isinstance(raw_limits, Mapping):
        raise UsageError("usage cache omitted limits")
    limits: dict[str, dict[str, float | int]] = {}
    for raw_id, raw_reading in raw_limits.items():
        limit_id = str(raw_id)
        if not isinstance(raw_reading, Mapping):
            raise UsageError(f"limit {limit_id} is not an object")
        used = raw_reading.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(float(used)):
            raise UsageError(f"limit {limit_id} has invalid used_percent")
        used_number = float(used)
        if used_number < 0 or used_number > 100:
            raise UsageError(f"limit {limit_id} used_percent is outside 0..100")
        reset = raw_reading.get("resets_at")
        limits[limit_id] = {"used_percent": used_number}
        limits[limit_id]["resets_at"] = _epoch(reset, f"limit {limit_id} resets_at")
    return UsageSnapshot(
        account.provider_id, account.id, captured_at, limits,
        fresh=raw.get("error") is None,
        error="proxy usage unavailable" if raw.get("error") else None,
        unmetered=account.unmetered,
    )


def write_cache(path: Path, value: Mapping[str, Any]) -> None:
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
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
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
    account: ProxyAccount,
    *,
    now_epoch: int | None = None,
    max_age_seconds: int = 3_600,
) -> UsageSnapshot:
    now = int(time.time() if now_epoch is None else now_epoch)
    path = cache_path(cache_root, account.provider_id, account.id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        snapshot = normalize_snapshot(raw, account, now_epoch=now)
    except FileNotFoundError:
        return UsageSnapshot(account.provider_id, account.id, 0, {}, fresh=False, error="missing usage cache", unmetered=account.unmetered)
    except (OSError, UnicodeError, json.JSONDecodeError, UsageError):
        return UsageSnapshot(account.provider_id, account.id, 0, {}, fresh=False, error="usage cache unavailable", unmetered=account.unmetered)
    if snapshot.captured_at > now + 300:
        return UsageSnapshot(
            snapshot.provider_id, snapshot.account_id, snapshot.captured_at, snapshot.limits,
            fresh=False, error="cache timestamp is in the future", unmetered=account.unmetered,
        )
    if now - snapshot.captured_at > max_age_seconds:
        return UsageSnapshot(
            snapshot.provider_id, snapshot.account_id, snapshot.captured_at, snapshot.limits,
            fresh=False, error="stale usage cache", unmetered=account.unmetered,
        )
    return snapshot


def read_accounts(
    config: RuntimeConfig,
    cache_root: str | os.PathLike[str] | Path | None = None,
    *,
    now_epoch: int | None = None,
) -> tuple[ProxyAccount, ...]:
    root = Path(cache_root) if cache_root is not None else config.usage_cache_dir
    try:
        raw = json.loads((root / "auth-files.json").read_text(encoding="utf-8"))
        _epoch(raw["captured_at"], "captured_at")
        if not isinstance(raw["accounts"], list):
            raise UsageError("invalid proxy account inventory")
        accounts = []
        seen = set()
        providers = {provider.id for provider in config.providers}
        for item in raw["accounts"]:
            provider = _identifier(item["provider_id"], "provider_id")
            account_id = _identifier(item["id"], "account_id")
            if not isinstance(item["unmetered"], bool) or (provider, account_id) in seen:
                raise UsageError("invalid proxy account inventory")
            seen.add((provider, account_id))
            if provider in providers:
                accounts.append(ProxyAccount(account_id, provider, item["unmetered"]))
        return tuple(accounts)
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, UsageError):
        raise UsageError("proxy account inventory unavailable") from None


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
        for account in read_accounts(config, root, now_epoch=now_epoch)
    }


def read_cache(
    config: RuntimeConfig,
    cache_root: str | os.PathLike[str] | Path,
    provider_id: str,
    account_id: str,
    *,
    now_epoch: int | None = None,
) -> UsageSnapshot:
    account = next((item for item in read_accounts(config, cache_root, now_epoch=now_epoch)
                    if item.id == account_id and item.provider_id == provider_id), None)
    if account is None:
        return UsageSnapshot(provider_id, account_id, 0, {}, fresh=False, error="unknown proxy account")
    return read_cached(cache_root, account, now_epoch=now_epoch,
                       max_age_seconds=config.usage_max_age_seconds)


def refresh_all(
    config: RuntimeConfig,
    cache_root: str | os.PathLike[str] | Path | None = None,
    *,
    now_epoch: int | None = None,
) -> dict[tuple[str, str], UsageSnapshot]:
    from .proxy_usage import refresh
    return refresh(config, cache_root, now_epoch=now_epoch)


def legacy_window_line(config: RuntimeConfig, snapshot: UsageSnapshot) -> str:
    """Render the shell window line from the normalized proxy cache."""
    def reading(name: str) -> tuple[float | int, int]:
        item = snapshot.limits.get(name, {})
        used = item.get("used_percent", 0)
        return (int(used) if float(used).is_integer() else float(used),
                int(item.get("resets_at", 0)))
    short_used, short_reset = reading("five_hour")
    long_used, long_reset = reading("seven_day")
    return f"{short_used} {short_reset} {long_used} {long_reset}"
