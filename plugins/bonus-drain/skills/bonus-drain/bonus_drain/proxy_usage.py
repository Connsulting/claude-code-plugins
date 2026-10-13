"""Read account inventory and usage through CLIProxyAPI management only."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import RuntimeConfig, resolve_secret
from .usage import (
    ProxyAccount,
    UsageError,
    UsageSnapshot,
    _epoch,
    cache_path,
    write_cache,
)

ANTHROPIC_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
TARGETS = {
    ANTHROPIC_URL: {
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-cli/2.1.280 (external, cli)",
    },
    CODEX_URL: {},
}
# CPA uses vendor names for some auth files; routing uses the configured pool ids.
PROVIDERS = {"anthropic": "claude", "claude": "claude", "openai": "codex",
             "codex": "codex", "grok": "xai", "xai": "xai", "meta": "meta"}
MAX_RESPONSE_BYTES = 1_048_576


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class ProxyReader:
    def __init__(self, config: RuntimeConfig) -> None:
        ref = next((item for item in config.secret_refs
                    if item.id == config.proxy_secret_ref and item.source == "file"), None)
        if ref is None:
            raise UsageError("proxy dotenv file secret ref is required")
        values = {}
        for line in resolve_secret(ref).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if not separator:
                raise UsageError("invalid proxy dotenv entry")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[name.strip()] = value
        base = values.get("CPA_BASE_URL", "").rstrip("/")
        try:
            parsed = urlsplit(base)
            valid_port = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError:
            raise UsageError("invalid proxy base URL") from None
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or not valid_port
                or any(char.isspace() for char in base)):
            raise UsageError("invalid proxy base URL")
        key = values.get("CPA_MGMT_KEY", "")
        if not key or any(char in key for char in "\r\n"):
            raise UsageError("proxy management credential is missing or invalid")
        self._base = base
        self._key = key
        self._opener = build_opener(_NoRedirect())

    def _request(self, endpoint: str, payload: Mapping[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(self._base + "/v0/management/" + endpoint, data=data,
                          headers={"Authorization": "Bearer " + self._key,
                                   "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise UsageError("proxy response exceeds size limit")
            return json.loads(raw)
        except (HTTPError, URLError, OSError, ValueError):
            # Exceptions, bodies and request objects can contain credentials. Never echo them.
            raise UsageError("proxy management request failed") from None

    def accounts(self, config: RuntimeConfig) -> tuple[tuple[ProxyAccount, str], ...]:
        raw = self._request("auth-files")
        rows = raw.get("files") if isinstance(raw, Mapping) else None
        if not isinstance(rows, list):
            raise UsageError("invalid proxy account inventory")
        configured = {item.id for item in config.providers}
        accounts = []
        seen = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise UsageError("invalid proxy account entry")
            if row.get("disabled") is True:
                continue
            name = row.get("provider")
            if not isinstance(name, str):
                raise UsageError("proxy account omitted provider")
            provider = PROVIDERS.get(name.lower(), name.lower())
            if provider not in configured:
                continue
            index = row.get("auth_index")
            if isinstance(index, bool) or not isinstance(index, (str, int)):
                raise UsageError("proxy account omitted auth index")
            index = str(index)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", index):
                raise UsageError("invalid proxy auth index")
            account = ProxyAccount(provider + "-" + index, provider,
                                   provider not in {"claude", "codex"})
            if account.id in seen:
                raise UsageError("duplicate proxy account")
            seen.add(account.id)
            accounts.append((account, index))
        return tuple(accounts)

    def api_call(self, auth_index: str, url: str) -> Mapping[str, Any]:
        if url not in TARGETS:
            raise UsageError("usage target is not allowed")
        raw = self._request("api-call", {
            "auth_index": auth_index, "method": "GET", "url": url,
            "header": {"Authorization": "Bearer $TOKEN$", **TARGETS[url]},
        })
        if not isinstance(raw, Mapping) or raw.get("status_code") != 200:
            raise UsageError("provider usage request failed")
        body = raw.get("body")
        try:
            body = json.loads(body) if isinstance(body, str) else body
        except ValueError:
            raise UsageError("invalid provider usage response") from None
        if not isinstance(body, Mapping):
            raise UsageError("invalid provider usage response")
        return body


def _reading(raw: Any, percent: str, reset: str) -> dict[str, float | int]:
    if not isinstance(raw, Mapping):
        raise UsageError("invalid provider usage window")
    used = raw.get(percent)
    if (isinstance(used, bool) or not isinstance(used, (float, int))
            or not math.isfinite(used) or not 0 <= used <= 100):
        raise UsageError("invalid provider usage percent")
    return {"used_percent": float(used), "resets_at": _epoch(raw.get(reset), "reset")}


def parse_windows(provider: str, body: Mapping[str, Any]) -> dict[str, Mapping[str, float | int]]:
    windows = {}
    if provider == "claude":
        for name, raw in body.items():
            if (name in {"five_hour", "seven_day"} or name.startswith("seven_day_")) and raw is not None:
                windows[name] = _reading(raw, "utilization", "resets_at")
    elif provider == "codex":
        limits = body.get("rate_limit")
        if not isinstance(limits, Mapping):
            raise UsageError("missing Codex rate limits")
        # WHAM primary is the short window and secondary is the weekly window,
        # matching codex's existing weekly admission policy. Duration wins if supplied.
        for name, fallback in (("primary_window", "five_hour"), ("secondary_window", "seven_day")):
            raw = limits.get(name)
            if raw is None:
                continue
            duration = raw.get("limit_window_seconds") if isinstance(raw, Mapping) else None
            window = "seven_day" if duration == 604800 else "five_hour" if duration == 18000 else fallback
            windows[window] = _reading(raw, "used_percent", "reset_at")
    return windows


def refresh(config: RuntimeConfig, cache_root: str | Path | None = None, *,
            now_epoch: int | None = None) -> dict[tuple[str, str], UsageSnapshot]:
    now = int(time.time() if now_epoch is None else now_epoch)
    root = Path(cache_root) if cache_root is not None else config.usage_cache_dir
    reader = ProxyReader(config)
    accounts = reader.accounts(config)
    results = {}
    for account, index in accounts:
        try:
            windows = {} if account.unmetered else parse_windows(
                account.provider_id, reader.api_call(index, ANTHROPIC_URL if account.provider_id == "claude" else CODEX_URL),
            )
            snapshot = UsageSnapshot(account.provider_id, account.id, now, windows,
                                     unmetered=account.unmetered)
        except UsageError:
            snapshot = UsageSnapshot(account.provider_id, account.id, now, {}, fresh=False,
                                     error="proxy usage unavailable", unmetered=account.unmetered)
        write_cache(cache_path(root, account.provider_id, account.id), snapshot.cache_dict())
        results[(account.provider_id, account.id)] = snapshot
    write_cache(root / "auth-files.json", {
        "captured_at": now,
        "accounts": [{"id": account.id, "provider_id": account.provider_id,
                      "unmetered": account.unmetered} for account, _index in accounts],
    })
    return results
