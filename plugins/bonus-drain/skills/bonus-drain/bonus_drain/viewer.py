"""Cache/DB-only Bonus Drain viewer with a fail-closed remote mode.

No request path invokes a usage adapter, provider executable, router, or service
manager.  Remote access is explicit and authenticated.  Mutating routes are not
registered unless the operator opts in, and then require a session, CSRF token,
and exact Host and Origin matches.
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
    runs: tuple[Any, ...]
    claims: tuple[Any, ...]

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
    return ViewerSnapshot(
        generated_at=now,
        accounts=tuple(accounts),
        tasks=_queue_collection(queue, "tasks"),
        runs=_queue_collection(queue, "runs"),
        claims=_queue_collection(queue, "claims"),
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
        hosts = tuple(_get(remote_config, "allowed_hosts", ()) or ())
        origins = tuple(_get(remote_config, "allowed_origins", ()) or ())
        if not isinstance(secret_ref, str) or not secret_ref:
            raise SecurityError("remote viewer requires auth_secret_ref")
        if resolve_secret is None:
            raise SecurityError("remote viewer requires an external secret resolver")
        secret = resolve_secret(secret_ref)
        if not isinstance(secret, str) or len(secret) < 12:
            raise SecurityError("remote viewer authentication secret is unavailable or too short")
        if not hosts or any(not isinstance(host, str) or not host or "/" in host for host in hosts):
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
            except ValueError as exc:
                raise SecurityError("remote viewer origin is invalid") from exc
            if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
                raise SecurityError("remote viewer origins must be exact HTTPS origins")
            if parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise SecurityError("remote viewer origin is invalid")
        # Store only a one-way verifier, never the externally resolved credential.
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        return cls(
            bind=bind,
            remote=True,
            mutations_enabled=bool(_get(raw, "mutations_enabled", False)),
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
        if session is None:
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


def _page(data: ViewerSnapshot, csrf: str | None) -> bytes:
    rows = []
    for account in data.accounts:
        state = "fresh" if account.fresh else "closed"
        rows.append(
            "<tr><td>" + html.escape(account.provider_id) + "</td><td>" +
            html.escape(account.account_id) + "</td><td>" + state + "</td></tr>"
        )
    csrf_meta = f'<meta name="csrf-token" content="{html.escape(csrf)}">' if csrf else ""
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        + csrf_meta + "<title>Bonus Drain</title></head><body>"
        "<h1>Bonus Drain</h1><table><thead><tr><th>Provider</th><th>Account</th>"
        "<th>Cache</th></tr></thead><tbody>" + "".join(rows) +
        "</tbody></table></body></html>"
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
            if self.path == "/login" and policy.remote:
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
            if self.path == "/session" and policy.remote:
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
