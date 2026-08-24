"""Cache/DB-only Bonus Drain viewer with fail-closed external access.

No request path invokes a usage adapter, provider executable, router, or service
manager. External access is either application-authenticated or explicitly trusted
behind an authenticated loopback proxy. Mutating routes are not registered unless
the operator opts in, and then require a session, CSRF token, and exact Host and
Origin matches.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs
from urllib.parse import urlsplit


class ViewerError(RuntimeError):
    """Base class for viewer failures."""


class SecurityError(ViewerError):
    """The requested viewer mode is not safely configured."""


MAX_REQUEST_BODY = 4096
MAX_SESSIONS = 128
SESSION_TTL_SECONDS = 8 * 60 * 60
SESSION_COOKIE = "bonus_drain_session"
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_FAILURES = 5
MAX_LOGIN_CLIENTS = 256
LOGGER = logging.getLogger(__name__)


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _loopback(bind: str) -> bool:
    if bind.lower() == "localhost":
        return True
    try:
        return ip_address(bind).is_loopback
    except ValueError:
        return False


def _exact_host(value: Any) -> bool:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return False
    if any(marker in value for marker in ("/", "*", "@", "?", "#")):
        return False
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and (port is None or 1 <= port <= 65535)
    )


@dataclass(frozen=True)
class ViewerAccount:
    provider_id: str
    account_id: str
    captured_at: int | None
    fresh: bool
    limits: Mapping[str, Any]
    diagnostic: str | None = None


@dataclass(frozen=True)
class ViewerSnapshot:
    generated_at: int
    accounts: tuple[ViewerAccount, ...]
    tasks: tuple[Any, ...]
    eligible: tuple[Any, ...]
    disabled: tuple[Any, ...]
    runs: tuple[Any, ...]
    claims: tuple[Any, ...]
    inflight: tuple[Any, ...]

    def as_dict(self) -> dict[str, Any]:
        return _plain(self)


def _cache_record(cache_root: Path, provider_id: str, account_id: str) -> tuple[dict[str, Any] | None, str | None]:
    # Keep path construction in the cache module so viewer and refresher cannot drift.
    try:
        from . import usage

        path = usage.cache_path(cache_root, provider_id, account_id)
    except Exception as exc:
        return None, f"cache path unavailable: {exc}"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "cache missing"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None, "cache malformed"
    if not isinstance(raw, dict):
        return None, "cache malformed"
    if raw.get("provider_id") != provider_id or raw.get("account_id") != account_id:
        return None, "cache identity mismatch"
    if not isinstance(raw.get("captured_at"), (int, float)) or isinstance(raw.get("captured_at"), bool):
        return None, "cache timestamp missing"
    if not isinstance(raw.get("limits"), dict):
        return None, "cache limits malformed"
    return raw, None


def _queue_collection(queue: Any, method: str) -> tuple[Any, ...]:
    callback = getattr(queue, method, None)
    if not callable(callback):
        return ()
    try:
        result = callback()
    except Exception:
        return ()
    if result is None:
        return ()
    return tuple(_plain(item) for item in result)


def _queue_call(queue: Any, method: str, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
    callback = getattr(queue, method, None)
    if not callable(callback):
        return ()
    try:
        result = callback(*args, **kwargs)
    except Exception:
        return ()
    return tuple(_plain(item) for item in (result or ()))


def snapshot(
    cfg: Any,
    queue: Any,
    cache_root: str | Path,
    *,
    now_epoch: int | None = None,
) -> ViewerSnapshot:
    """Return only persisted cache and queue data; never refresh usage."""

    now = int(time.time() if now_epoch is None else now_epoch)
    root = Path(cache_root)
    accounts: list[ViewerAccount] = []
    future_resets: list[int] = []
    # The config validator has already guaranteed provider/account relationships.
    for account in _get(cfg, "accounts", ()):
        provider_id = str(_get(account, "provider_id"))
        account_id = str(_get(account, "id"))
        raw, diagnostic = _cache_record(root, provider_id, account_id)
        captured = int(raw["captured_at"]) if raw is not None else None
        # The usage module owns the authoritative freshness calculation where available.
        fresh = False
        if raw is not None:
            try:
                from . import usage
                cached = usage.read_cached(
                    root,
                    account,
                    now_epoch=now,
                    max_age_seconds=int(_get(cfg, "usage_max_age_seconds", 3600)),
                )
                fresh = bool(_get(cached, "fresh", False))
                diagnostic = _get(cached, "error", diagnostic)
            except Exception:
                max_age = int(_get(cfg, "usage_max_age_seconds", 3600))
                fresh = 0 <= now - captured <= max_age
        accounts.append(
            ViewerAccount(
                provider_id=provider_id,
                account_id=account_id,
                captured_at=captured,
                fresh=fresh,
                limits=_plain(raw.get("limits", {})) if raw else {},
                diagnostic=diagnostic,
            )
        )
        if raw is not None:
            for reading in raw.get("limits", {}).values():
                reset = reading.get("resets_at") if isinstance(reading, Mapping) else None
                if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > now:
                    future_resets.append(int(reset))
    tasks = _queue_collection(queue, "tasks")
    cycle = min(future_resets, default=now)
    eligible_by_id: dict[str, Any] = {}
    for provider in _get(cfg, "providers", ()):
        for task in _queue_call(
            queue,
            "eligible_tasks",
            cycle,
            provider_id=str(_get(provider, "id")),
            capabilities=_get(provider, "capabilities", ()),
        ):
            task_id = str(_get(task, "id", ""))
            if task_id:
                eligible_by_id[task_id] = task
    eligible = tuple(sorted(
        eligible_by_id.values(),
        key=lambda task: (
            int(_get(task, "priority", 99)),
            _get(task, "kind") == "recurring",
            str(_get(task, "created_at", "")),
            str(_get(task, "id", "")),
        ),
    ))
    return ViewerSnapshot(
        generated_at=now,
        accounts=tuple(accounts),
        tasks=tasks,
        eligible=eligible,
        disabled=tuple(task for task in tasks if not bool(_get(task, "active", False))),
        runs=_queue_call(queue, "runs", limit=60),
        claims=_queue_collection(queue, "claims"),
        inflight=_queue_collection(queue, "inflight"),
    )


@dataclass(frozen=True)
class Session:
    token: str
    csrf_token: str
    expires_at: int

    @property
    def set_cookie(self) -> str:
        max_age = max(0, self.expires_at - int(time.time()))
        return (
            f"{SESSION_COOKIE}={self.token}; Path=/; Max-Age={max_age}; "
            "Secure; HttpOnly; SameSite=Strict"
        )


@dataclass
class SecurityPolicy:
    bind: str
    remote: bool
    mutations_enabled: bool
    trusted_loopback_proxy: bool = False
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    _auth_digest: bytes | None = field(default=None, repr=False)
    _sessions: dict[str, Session] = field(default_factory=dict, repr=False)
    _login_failures: dict[str, tuple[int, float]] = field(default_factory=dict, repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

    @classmethod
    def from_config(
        cls,
        raw: Any,
        *,
        remote: bool,
        resolve_secret: Callable[[str], str | None] | None = None,
    ) -> "SecurityPolicy":
        bind = str(_get(raw, "bind", "127.0.0.1"))
        if not remote:
            if not _loopback(bind):
                raise SecurityError("local viewer must bind to a loopback address")
            local_hosts = tuple(dict.fromkeys((bind, "localhost", "127.0.0.1", "[::1]")))
            return cls(
                bind=bind,
                remote=False,
                mutations_enabled=bool(_get(raw, "mutations_enabled", False)),
                allowed_hosts=local_hosts,
            )

        remote_config = _get(raw, "remote")
        if remote_config is None:
            raise SecurityError("remote viewer requires an explicit remote configuration")
        secret_ref = _get(remote_config, "auth_secret_ref")
        trusted_loopback_proxy = _get(remote_config, "trusted_loopback_proxy", False)
        hosts = tuple(_get(remote_config, "allowed_hosts", ()) or ())
        origins = tuple(_get(remote_config, "allowed_origins", ()) or ())
        if not isinstance(trusted_loopback_proxy, bool):
            raise SecurityError("trusted_loopback_proxy must be boolean")
        if not hosts or any(not _exact_host(host) for host in hosts):
            raise SecurityError("remote viewer requires exact allowed_hosts")
        if not origins:
            raise SecurityError("remote viewer requires exact HTTPS allowed_origins")
        if not _loopback(bind) and _get(remote_config, "https_terminated", False) is not True:
            raise SecurityError(
                "non-loopback remote bind requires explicit https_terminated=true"
            )
        for origin in origins:
            try:
                parsed = urlsplit(origin)
                parsed_port = parsed.port
            except ValueError as exc:
                raise SecurityError("remote viewer origin is invalid") from exc
            if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
                raise SecurityError("remote viewer origins must be exact HTTPS origins")
            if (
                parsed.query or parsed.fragment or parsed.username or parsed.password
                or any(char.isspace() for char in origin) or "*" in origin
                or (parsed_port is not None and not 1 <= parsed_port <= 65535)
            ):
                raise SecurityError("remote viewer origin is invalid")
        mutations_enabled = bool(_get(raw, "mutations_enabled", False))
        digest = None
        if trusted_loopback_proxy:
            if secret_ref is not None:
                raise SecurityError(
                    "trusted loopback proxy mode cannot also configure auth_secret_ref"
                )
            if not _loopback(bind):
                raise SecurityError("trusted loopback proxy mode requires a loopback bind")
            if mutations_enabled:
                raise SecurityError("trusted loopback proxy mode requires mutations disabled")
        else:
            if not isinstance(secret_ref, str) or not secret_ref:
                raise SecurityError("remote viewer requires auth_secret_ref")
            if resolve_secret is None:
                raise SecurityError("remote viewer requires an external secret resolver")
            secret = resolve_secret(secret_ref)
            if not isinstance(secret, str) or len(secret) < 12:
                raise SecurityError(
                    "remote viewer authentication secret is unavailable or too short"
                )
            # Store only a one-way verifier, never the externally resolved credential.
            digest = hashlib.sha256(secret.encode("utf-8")).digest()
        return cls(
            bind=bind,
            remote=True,
            mutations_enabled=mutations_enabled,
            trusted_loopback_proxy=trusted_loopback_proxy,
            allowed_hosts=hosts,
            allowed_origins=origins,
            _auth_digest=digest,
        )

    @property
    def cors_headers(self) -> dict[str, str]:
        return {}

    def host_allowed(self, host: str | None) -> bool:
        if not isinstance(host, str) or not host:
            return False
        if self.remote:
            return host in self.allowed_hosts
        try:
            parsed = urlsplit(f"//{host}")
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return False
        if not hostname or parsed.username or parsed.password or parsed.path:
            return False
        if port is not None and not 1 <= port <= 65535:
            return False
        allowed = {item.strip("[]").lower() for item in self.allowed_hosts}
        return hostname.strip("[]").lower() in allowed

    def login_allowed(self, client_id: str) -> bool:
        """Apply a small, bounded in-memory throttle before hashing credentials."""

        now = time.monotonic()
        with self._lock:
            self._prune_login_failures_locked(now)
            count, _started = self._login_failures.get(client_id, (0, now))
            return count < LOGIN_MAX_FAILURES

    def record_login_failure(self, client_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune_login_failures_locked(now)
            count, started = self._login_failures.get(client_id, (0, now))
            if len(self._login_failures) >= MAX_LOGIN_CLIENTS and client_id not in self._login_failures:
                oldest = min(self._login_failures, key=lambda key: self._login_failures[key][1])
                self._login_failures.pop(oldest, None)
            self._login_failures[client_id] = (min(count + 1, LOGIN_MAX_FAILURES), started)

    def clear_login_failures(self, client_id: str) -> None:
        with self._lock:
            self._login_failures.pop(client_id, None)

    def _prune_login_failures_locked(self, now: float) -> None:
        for client_id, (_count, started) in list(self._login_failures.items()):
            if now - started >= LOGIN_WINDOW_SECONDS:
                self._login_failures.pop(client_id, None)

    def origin_allowed(self, origin: str | None, *, required: bool) -> bool:
        if not self.remote:
            return True
        if origin is None:
            return not required
        return origin in self.allowed_origins

    def new_session(self, presented_secret: str) -> Session:
        if not self.remote or self._auth_digest is None:
            raise SecurityError("sessions are available only in authenticated remote mode")
        if not isinstance(presented_secret, str):
            raise SecurityError("authentication failed")
        presented = hashlib.sha256(presented_secret.encode("utf-8")).digest()
        if not hmac.compare_digest(presented, self._auth_digest):
            raise SecurityError("authentication failed")
        now = int(time.time())
        session = Session(
            token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + SESSION_TTL_SECONDS,
        )
        with self._lock:
            self._expire_locked(now)
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda item: item.expires_at)
                self._sessions.pop(oldest.token, None)
            self._sessions[session.token] = session
        return session

    def _expire_locked(self, now: int) -> None:
        for token, session in list(self._sessions.items()):
            if session.expires_at <= now:
                self._sessions.pop(token, None)

    def session(self, token: str | None) -> Session | None:
        if not self.remote:
            return None
        if not token:
            return None
        now = int(time.time())
        with self._lock:
            self._expire_locked(now)
            return self._sessions.get(token)

    def authorize_read(self, session: Session | None, host: str | None, origin: str | None = None) -> bool:
        if not self.remote:
            return self.host_allowed(host)
        if self.trusted_loopback_proxy:
            return self.host_allowed(host) and self.origin_allowed(origin, required=False)
        return (
            session is not None
            and self.host_allowed(host)
            and self.origin_allowed(origin, required=False)
        )

    def authorize_mutation(
        self,
        session: Session | None,
        csrf_token: str | None,
        host: str | None,
        origin: str | None,
    ) -> bool:
        # This predicate proves the request's security properties.  Route registration
        # separately enforces ``mutations_enabled`` so callers can test authentication
        # policy without accidentally turning a mutation surface on.
        if self.trusted_loopback_proxy or session is None:
            return False
        try:
            csrf_matches = bool(csrf_token) and hmac.compare_digest(
                csrf_token, session.csrf_token
            )
        except TypeError:
            csrf_matches = False
        return bool(
            csrf_matches
            and self.host_allowed(host)
            and self.origin_allowed(origin, required=self.remote)
        )


_VIEWER_CSS = """
:root{color-scheme:dark;--bg:#0a0a0b;--panel:#101012;--panel2:#141416;--line:rgba(255,255,255,.09);--line2:rgba(255,255,255,.055);--fg:#e9e7e2;--dim:rgba(233,231,226,.66);--dim2:rgba(233,231,226,.48);--acc:#e4a83b;--ok:#68c58c;--warn:#e36d55;--mono:'Geist Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg)}body{color:var(--fg);font:13px/1.5 var(--mono);-webkit-text-size-adjust:100%}.wrap{max-width:1400px;margin:auto;padding:0 clamp(14px,3vw,30px) 80px}.topbar{position:sticky;top:0;z-index:10;min-height:55px;display:flex;align-items:center;justify-content:space-between;background:var(--bg);border-bottom:1px solid var(--line);margin:0 calc(-1*clamp(14px,3vw,30px));padding:0 clamp(14px,3vw,30px)}.brand,.stamp{font-size:10.5px;letter-spacing:.11em;text-transform:uppercase;color:var(--dim)}.hd{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap;margin:28px 0 20px}.hd h1{font-size:clamp(23px,4vw,31px);font-weight:500;letter-spacing:-.035em;margin:0}.sub{margin-top:5px;color:var(--dim);font-size:11px;letter-spacing:.04em}.pill{display:flex;align-items:center;gap:9px;padding:8px 12px;border:1px solid rgba(228,168,59,.4);color:var(--acc);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase}.dot{width:7px;height:7px;border-radius:50%;background:currentColor}.summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:1px solid var(--line);background:var(--panel);margin-bottom:14px}.metric{padding:14px 16px;border-left:1px solid var(--line)}.metric:first-child{border-left:0}.metric b{display:block;font-weight:400;font-size:22px}.metric span{font-size:9.5px;color:var(--dim2);text-transform:uppercase;letter-spacing:.12em}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:10px}.card{background:var(--panel);border:1px solid var(--line);padding:16px}.cname{display:flex;justify-content:space-between;gap:12px;align-items:center}.engine{font-size:12px;letter-spacing:.08em}.tag{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--ok);border:1px solid rgba(104,197,140,.35);padding:1px 6px}.tag.unknown{color:var(--warn);border-color:rgba(227,109,85,.4)}.limit{margin-top:15px}.limithead{display:flex;justify-content:space-between;gap:10px;color:var(--dim);font-size:10.5px}.limithead b{font-size:14px;color:var(--fg);font-weight:400}.bar{height:7px;background:rgba(255,255,255,.075);margin-top:7px;overflow:hidden}.fill{height:100%;background:var(--acc)}.reset{margin-top:7px;color:var(--dim2);font-size:10px}.sec{margin-top:28px}.sech{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:9px}.sech h2{font-weight:400;font-size:12px;letter-spacing:.1em;text-transform:uppercase;margin:0}.note{font-size:10px;color:var(--dim2)}.rows{background:var(--panel);border:1px solid var(--line)}.empty{padding:16px;color:var(--dim)}.flight,.run{display:grid;grid-template-columns:minmax(160px,1fr) 120px 110px minmax(180px,1.4fr);gap:14px;padding:10px 14px;border-top:1px solid var(--line2);align-items:baseline}.flight:first-child,.run:first-child{border-top:0}.status{font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--acc)}.status.done{color:var(--ok)}.status.failed{color:var(--warn)}.muted{color:var(--dim2);font-size:10.5px}.bands{border-top:1px solid var(--line)}.band{border:1px solid var(--line);border-top:0;background:var(--panel)}.band>summary{list-style:none;cursor:pointer;padding:10px 14px;color:var(--dim);letter-spacing:.1em;text-transform:uppercase;font-size:10px}.band>summary::-webkit-details-marker{display:none}.band>summary:before{content:'▸';margin-right:9px;color:var(--acc)}.band[open]>summary:before{content:'▾'}.qrow{padding:13px 15px;border-top:1px solid var(--line2)}.qtop{display:flex;justify-content:space-between;gap:15px;align-items:flex-start}.qtitle{font-size:13px}.qmeta{white-space:nowrap;color:var(--dim2);font-size:10px}.qgoal{margin-top:6px;color:var(--dim);font-size:11px;max-width:1050px}.qdetails{margin-top:9px}.qdetails summary{cursor:pointer;color:var(--dim2);font-size:10px}.qdetails pre{white-space:pre-wrap;word-break:break-word;color:var(--dim);background:var(--panel2);border:1px solid var(--line2);padding:10px;font:10.5px/1.45 var(--mono);max-height:230px;overflow:auto}.disabled{opacity:.62}.footer{margin-top:32px;color:var(--dim2);font-size:10px}@media(max-width:760px){.summary{grid-template-columns:repeat(2,1fr)}.metric:nth-child(3){border-left:0;border-top:1px solid var(--line)}.metric:nth-child(4){border-top:1px solid var(--line)}.flight,.run{grid-template-columns:1fr 90px}.flight>*:nth-child(4),.run>*:nth-child(4){grid-column:1/-1}.stamp{display:none}}
"""


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _relative_time(epoch: Any, now: int) -> str:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return "unknown"
    seconds = int(epoch) - now
    suffix = "from now" if seconds >= 0 else "ago"
    seconds = abs(seconds)
    if seconds < 90:
        amount = f"{seconds}s"
    elif seconds < 7200:
        amount = f"{seconds // 60}m"
    elif seconds < 172800:
        amount = f"{seconds // 3600}h"
    else:
        amount = f"{seconds // 86400}d {seconds % 86400 // 3600}h"
    return f"{amount} {suffix}"


def _run_epoch(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _task_rows(tasks: tuple[Any, ...], *, disabled: bool = False) -> str:
    grouped: dict[int, list[Any]] = {}
    for task in tasks:
        grouped.setdefault(int(_get(task, "priority", 9)), []).append(task)
    if not grouped:
        return '<div class="empty">none</div>'
    bands: list[str] = []
    for priority in sorted(grouped):
        rows: list[str] = []
        for task in grouped[priority]:
            detail_parts = []
            for label, key in (("Context", "context"), ("Constraints", "constraints"), ("Precondition", "precondition"), ("Done when", "done_when")):
                value = _get(task, key)
                if value:
                    detail_parts.append(f"{label}:\n{value}")
            detail = "\n\n".join(detail_parts)
            detail_html = (
                f'<details class="qdetails"><summary>execution contract</summary><pre>{_escape(detail)}</pre></details>'
                if detail else ""
            )
            rows.append(
                f'<div class="qrow{" disabled" if disabled else ""}"><div class="qtop">'
                f'<div><div class="qtitle">{_escape(_get(task, "title") or _get(task, "id"))}</div>'
                f'<div class="qgoal">{_escape(_get(task, "goal"))}</div></div>'
                f'<div class="qmeta">{_escape(_get(task, "kind"))} · {_escape(_get(task, "id"))}</div>'
                f'</div>{detail_html}</div>'
            )
        bands.append(
            f'<details class="band" open><summary>P{priority} · {len(rows)} jobs</summary>{"".join(rows)}</details>'
        )
    return '<div class="bands">' + "".join(bands) + "</div>"


def _page(data: ViewerSnapshot, csrf: str | None) -> bytes:
    now = data.generated_at
    account_cards: list[str] = []
    for account in data.accounts:
        limits: list[str] = []
        for limit_id, raw in account.limits.items():
            reading = raw if isinstance(raw, Mapping) else {}
            used_raw = reading.get("used_percent")
            used = float(used_raw) if isinstance(used_raw, (int, float)) and not isinstance(used_raw, bool) else None
            width = max(0.0, min(100.0, used or 0.0))
            limits.append(
                f'<div class="limit"><div class="limithead"><span>{_escape(limit_id)}</span>'
                f'<b>{"unknown" if used is None else f"{used:.1f}%"}</b></div>'
                f'<div class="bar"><div class="fill" style="width:{width:.2f}%"></div></div>'
                f'<div class="reset">resets {_escape(_relative_time(reading.get("resets_at"), now))}</div></div>'
            )
        state = "fresh" if account.fresh else "unknown"
        account_cards.append(
            f'<div class="card"><div class="cname"><span class="engine">{_escape(account.provider_id)} · {_escape(account.account_id)}</span>'
            f'<span class="tag{" unknown" if not account.fresh else ""}">{state}</span></div>'
            f'{"".join(limits) if limits else "<div class=\"empty\">usage unavailable</div>"}</div>'
        )

    flight_rows = []
    for event in data.inflight:
        started = _run_epoch(_get(event, "ts"))
        flight_rows.append(
            f'<div class="flight"><span>{_escape(_get(event, "task"))}</span>'
            f'<span class="status">{_escape(_get(event, "provider_id") or _get(event, "engine") or "dispatch")}</span>'
            f'<span class="muted">{_escape(_relative_time(started, now))}</span>'
            f'<span class="muted">{_escape(_get(event, "summary"))}</span></div>'
        )
    run_rows = []
    for event in data.runs[:40]:
        status = str(_get(event, "status", ""))
        run_rows.append(
            f'<div class="run"><span>{_escape(_get(event, "task"))}</span>'
            f'<span class="status {_escape(status)}">{_escape(status)}</span>'
            f'<span class="muted">{_escape(_relative_time(_run_epoch(_get(event, "ts")), now))}</span>'
            f'<span class="muted">{_escape(_get(event, "summary"))}</span></div>'
        )
    csrf_meta = f'<meta name="csrf-token" content="{html.escape(csrf)}">' if csrf else ""
    state_label = f"draining · {len(data.inflight)} in flight" if data.inflight else "holding · no jobs in flight"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="refresh" content="60">' + csrf_meta + '<title>Bonus Drain</title>'
        f'<style>{_VIEWER_CSS}</style></head><body><div class="wrap">'
        f'<div class="topbar"><span class="brand">background jobs · bonus drain</span><span class="stamp">cache + sqlite only · refresh 60s</span></div>'
        f'<div class="hd"><div><h1>bonus-drain</h1><div class="sub">independent provider capacity console</div></div>'
        f'<div class="pill"><span class="dot"></span>{_escape(state_label)}</div></div>'
        f'<div class="summary"><div class="metric"><b>{len(data.accounts)}</b><span>accounts</span></div>'
        f'<div class="metric"><b>{len(data.eligible)}</b><span>eligible queue</span></div>'
        f'<div class="metric"><b>{len(data.inflight)}</b><span>in flight</span></div>'
        f'<div class="metric"><b>{len(data.runs)}</b><span>recent events</span></div></div>'
        f'<div class="grid">{"".join(account_cards)}</div>'
        f'<section class="sec"><div class="sech"><h2>in flight</h2><span class="note">dispatched without a later terminal event</span></div>'
        f'<div class="rows">{"".join(flight_rows) if flight_rows else "<div class=\"empty\">nothing running</div>"}</div></section>'
        f'<section class="sec"><div class="sech"><h2>queue</h2><span class="note">eligible now · nearest reset dispatch remains authoritative</span></div>{_task_rows(data.eligible)}</section>'
        f'<section class="sec"><div class="sech"><h2>run log</h2><span class="note">latest 40 events</span></div><div class="rows">{"".join(run_rows) if run_rows else "<div class=\"empty\">no runs</div>"}</div></section>'
        f'<section class="sec"><details><summary class="note">disabled jobs · {len(data.disabled)}</summary>{_task_rows(data.disabled, disabled=True)}</details></section>'
        f'<div class="footer">Generated {_escape(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)))} · viewer requests never call provider APIs</div>'
        '</div></body></html>'
    ).encode("utf-8")


def _login_page() -> bytes:
    return b"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Bonus Drain sign in</title></head><body><h1>Bonus Drain</h1>
<form action="/session" method="post" enctype="application/x-www-form-urlencoded">
<label>Access secret <input name="secret" type="password" required autocomplete="current-password"></label>
<button type="submit">Sign in</button></form></body></html>"""


def make_handler(
    cfg: Any,
    queue: Any,
    cache_root: str | Path,
    policy: SecurityPolicy,
) -> type[BaseHTTPRequestHandler]:
    """Create an isolated handler class.  It captures no provider adapter."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "BonusDrainViewer/1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _session(self) -> Session | None:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                return None
            morsel = cookie.get(SESSION_COOKIE)
            return policy.session(morsel.value if morsel else None)

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")

        def _send(self, status: HTTPStatus, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json_body(self) -> dict[str, Any] | None:
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, b'{"error":"application/json required"}')
                return None
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_REQUEST_BODY:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b'{"error":"invalid body size"}')
                return None
            try:
                body = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
                self._send(HTTPStatus.BAD_REQUEST, b'{"error":"invalid json"}')
                return None
            if not isinstance(body, dict):
                self._send(HTTPStatus.BAD_REQUEST, b'{"error":"json object required"}')
                return None
            return body

        def _login_body(self) -> dict[str, Any] | None:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type == "application/json":
                return self._json_body()
            if content_type != "application/x-www-form-urlencoded":
                self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, b'{"error":"unsupported body"}')
                return None
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_REQUEST_BODY:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b'{"error":"invalid body size"}')
                return None
            try:
                parsed = parse_qs(
                    self.rfile.read(length).decode("utf-8"),
                    keep_blank_values=False,
                    strict_parsing=True,
                    max_num_fields=2,
                )
            except (UnicodeDecodeError, ValueError):
                self._send(HTTPStatus.BAD_REQUEST, b'{"error":"invalid form"}')
                return None
            values = parsed.get("secret", [])
            if len(values) != 1:
                self._send(HTTPStatus.BAD_REQUEST, b'{"error":"secret required"}')
                return None
            return {"secret": values[0], "form": True}

        def _authorized_read(self) -> tuple[bool, Session | None]:
            session = self._session()
            allowed = policy.authorize_read(
                session, self.headers.get("Host"), self.headers.get("Origin")
            )
            if not allowed:
                self._send(HTTPStatus.UNAUTHORIZED, b'{"error":"authentication required"}')
            return allowed, session

        def do_GET(self) -> None:
            if not policy.host_allowed(self.headers.get("Host")):
                self._send(HTTPStatus.FORBIDDEN, b'{"error":"host rejected"}')
                return
            if self.path == "/health":
                self._send(HTTPStatus.OK, b'{"ok":true}')
                return
            if self.path == "/login" and policy.remote and not policy.trusted_loopback_proxy:
                if not policy.host_allowed(self.headers.get("Host")):
                    self._send(HTTPStatus.FORBIDDEN, b'{"error":"host rejected"}')
                    return
                self._send(HTTPStatus.OK, _login_page(), "text/html; charset=utf-8")
                return
            allowed, session = self._authorized_read()
            if not allowed:
                return
            data = snapshot(cfg, queue, cache_root)
            if self.path in ("/", "/index.html"):
                self._send(
                    HTTPStatus.OK,
                    _page(data, session.csrf_token if session else None),
                    "text/html; charset=utf-8",
                )
            elif self.path == "/api/snapshot":
                self._send(
                    HTTPStatus.OK,
                    json.dumps(data.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8"),
                )
            else:
                self._send(HTTPStatus.NOT_FOUND, b'{"error":"not found"}')

        def do_POST(self) -> None:
            # Session establishment is the only POST available when mutations are off.
            if self.path == "/session" and policy.remote and not policy.trusted_loopback_proxy:
                if not policy.host_allowed(self.headers.get("Host")) or not policy.origin_allowed(
                    self.headers.get("Origin"), required=True
                ):
                    self._send(HTTPStatus.FORBIDDEN, b'{"error":"host or origin rejected"}')
                    return
                client_id = str(self.client_address[0])
                if not policy.login_allowed(client_id):
                    LOGGER.warning("viewer login throttled for client %s", client_id)
                    self._send(HTTPStatus.TOO_MANY_REQUESTS, b'{"error":"login throttled"}')
                    return
                body = self._login_body()
                if body is None:
                    return
                presented = body.get("secret")
                if not isinstance(presented, str):
                    self._send(HTTPStatus.BAD_REQUEST, b'{"error":"secret required"}')
                    return
                try:
                    session = policy.new_session(presented)
                except SecurityError:
                    policy.record_login_failure(client_id)
                    LOGGER.warning("viewer login rejected for client %s", client_id)
                    self._send(HTTPStatus.UNAUTHORIZED, b'{"error":"authentication failed"}')
                    return
                policy.clear_login_failures(client_id)
                is_form = body.get("form") is True
                payload = (
                    b"" if is_form else
                    json.dumps({"ok": True, "csrf_token": session.csrf_token}).encode("utf-8")
                )
                self.send_response(HTTPStatus.SEE_OTHER if is_form else HTTPStatus.OK)
                self._security_headers()
                self.send_header("Set-Cookie", session.set_cookie)
                if is_form:
                    self.send_header("Location", "/")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if not policy.mutations_enabled:
                self._send(HTTPStatus.METHOD_NOT_ALLOWED, b'{"error":"mutations disabled"}')
                return
            session = self._session()
            if not policy.authorize_mutation(
                session,
                self.headers.get("X-CSRF-Token"),
                self.headers.get("Host"),
                self.headers.get("Origin"),
            ):
                self._send(HTTPStatus.FORBIDDEN, b'{"error":"request rejected"}')
                return
            body = self._json_body()
            if body is None:
                return
            task_id = body.get("task_id")
            try:
                from .db import is_safe_task_id
            except ImportError:
                is_safe_task_id = lambda _value: False
            if not is_safe_task_id(task_id):
                self._send(HTTPStatus.BAD_REQUEST, b'{"error":"invalid task_id"}')
                return
            action = {
                "/api/task/activate": True,
                "/api/task/deactivate": False,
                "/api/task/requeue": None,
            }.get(self.path, "missing")
            if action == "missing":
                self._send(HTTPStatus.NOT_FOUND, b'{"error":"not found"}')
                return
            try:
                if action is None:
                    queue.requeue(task_id)
                else:
                    queue.set_active(task_id, action)
            except Exception:
                self._send(HTTPStatus.CONFLICT, b'{"error":"operation refused"}')
                return
            self._send(HTTPStatus.OK, b'{"ok":true}')

    return Handler


def serve(
    cfg: Any,
    queue: Any,
    cache_root: str | Path,
    policy: SecurityPolicy,
    *,
    port: int = 8766,
) -> None:
    server = ThreadingHTTPServer((policy.bind, port), make_handler(cfg, queue, cache_root, policy))
    try:
        server.serve_forever()
    finally:
        server.server_close()
