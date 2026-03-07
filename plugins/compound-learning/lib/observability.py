"""
Shared observability utilities for compound-learning.

Emits structured JSONL events to a local file with low overhead and no stdout output.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping, MutableMapping, Optional

from lib.observability_taxonomy import normalize_event_taxonomy

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows
    fcntl = None  # type: ignore[assignment]


DEFAULT_LOG_PATH = os.path.expanduser(
    "~/.claude/plugins/compound-learning/observability.jsonl"
)

_LEVEL_RANK = {
    "debug": 10,
    "info": 20,
    "warn": 30,
    "warning": 30,
    "error": 40,
}

_APPEND_LOCK = threading.Lock()


@dataclass(frozen=True)
class ObservabilitySettings:
    enabled: bool
    level: str
    log_path: str
    context: Dict[str, Any]


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _normalize_level(level: Any) -> str:
    normalized = str(level or "info").strip().lower()
    if normalized == "warning":
        normalized = "warn"
    if normalized not in {"debug", "info", "warn", "error"}:
        return "info"
    return normalized


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted(_to_jsonable(v) for v in value)
    return str(value)


def _clean_fields(fields: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not fields:
        return {}
    cleaned: Dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        cleaned[str(key)] = _to_jsonable(value)
    return cleaned


def _error_payload(error: Any) -> Dict[str, Any]:
    if isinstance(error, BaseException):
        message = str(error).strip() or repr(error)
        return {
            "type": error.__class__.__name__,
            "message": message[:1000],
        }
    if isinstance(error, Mapping):
        return _clean_fields(error)
    return {"message": str(error)[:1000]}


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def now_perf() -> float:
    """Return a monotonic timer value for duration calculations."""
    return time.perf_counter()


def elapsed_ms(start_perf: float) -> int:
    """Return elapsed milliseconds from a perf-counter start value."""
    return max(0, int(round((time.perf_counter() - start_perf) * 1000)))


def resolve_settings(config: Optional[Mapping[str, Any]]) -> ObservabilitySettings:
    observability_cfg: Mapping[str, Any] = {}
    if isinstance(config, Mapping):
        maybe = config.get("observability")
        if isinstance(maybe, Mapping):
            observability_cfg = maybe

    enabled = _coerce_bool(observability_cfg.get("enabled"), default=False)
    level = _normalize_level(observability_cfg.get("level", "info"))
    log_path = os.path.expanduser(
        str(observability_cfg.get("logPath") or DEFAULT_LOG_PATH)
    )

    context = observability_cfg.get("context")
    if not isinstance(context, Mapping):
        context = {}

    return ObservabilitySettings(
        enabled=enabled,
        level=level,
        log_path=log_path,
        context=_clean_fields(context),
    )


def attach_runtime_context(
    config: MutableMapping[str, Any],
    **extra_fields: Any,
) -> Dict[str, Any]:
    """
    Ensure observability context includes runtime correlation/session identifiers.

    Precedence for session_id:
      1) LEARNINGS_OBS_SESSION_ID
      2) CLAUDE_SESSION_ID
      3) CLAUDE_SESSION

    Correlation ID comes from LEARNINGS_OBS_CORRELATION_ID when present,
    otherwise an existing config context value, otherwise a generated UUID.
    """
    obs_cfg = config.get("observability")
    if not isinstance(obs_cfg, MutableMapping):
        obs_cfg = {}
        config["observability"] = obs_cfg

    context = obs_cfg.get("context")
    if not isinstance(context, MutableMapping):
        context = {}

    correlation_id = (
        os.environ.get("LEARNINGS_OBS_CORRELATION_ID")
        or str(context.get("correlation_id") or "").strip()
        or uuid.uuid4().hex
    )
    context["correlation_id"] = correlation_id

    session_id = (
        os.environ.get("LEARNINGS_OBS_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION")
        or str(context.get("session_id") or "").strip()
    )
    if session_id:
        context["session_id"] = session_id
    else:
        context.pop("session_id", None)

    context.update(_clean_fields(extra_fields))
    cleaned = _clean_fields(context)
    obs_cfg["context"] = cleaned
    return cleaned


def _append_event(path: str, event: Mapping[str, Any]) -> None:
    line = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
    log_path = Path(path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with _APPEND_LOCK:
        with open(log_path, "a", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(line)
            handle.write("\n")
            handle.flush()
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class StructuredLogger:
    """Structured JSONL logger with level gating and optional default fields."""

    def __init__(
        self,
        component: str,
        settings: ObservabilitySettings,
        fields: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.component = component
        self.settings = settings
        self._fields = _clean_fields(fields)

    def bind(self, **fields: Any) -> "StructuredLogger":
        merged: Dict[str, Any] = dict(self._fields)
        merged.update(_clean_fields(fields))
        return StructuredLogger(self.component, self.settings, merged)

    def _allows(self, level: str) -> bool:
        if not self.settings.enabled:
            return False
        requested = _LEVEL_RANK.get(_normalize_level(level), 20)
        minimum = _LEVEL_RANK.get(self.settings.level, 20)
        return requested >= minimum

    def emit(
        self,
        operation: str,
        status: str,
        *,
        level: str = "info",
        duration_ms: Optional[int] = None,
        counts: Optional[Mapping[str, Any]] = None,
        message: Optional[str] = None,
        error: Any = None,
        **fields: Any,
    ) -> None:
        if not self._allows(level):
            return

        normalized_level = _normalize_level(level)
        normalized_taxonomy = normalize_event_taxonomy(operation, status)
        normalized_operation = str(normalized_taxonomy.pop("operation"))
        normalized_status = str(normalized_taxonomy.pop("status"))
        event: Dict[str, Any] = {
            "timestamp": _now_timestamp(),
            "level": normalized_level,
            "component": self.component,
            "operation": normalized_operation,
            "status": normalized_status,
        }

        if duration_ms is not None:
            event["duration_ms"] = max(0, int(duration_ms))

        if message:
            event["message"] = str(message)

        if counts:
            cleaned_counts = _clean_fields(counts)
            if cleaned_counts:
                event["counts"] = cleaned_counts

        if error is not None:
            payload = _error_payload(error)
            if payload:
                event["error"] = payload

        context = self.settings.context
        if context:
            event.update(context)

        if self._fields:
            event.update(self._fields)

        extra = _clean_fields(fields)
        if extra:
            event.update(extra)

        # Keep taxonomy keys authoritative even if callers pass colliding fields.
        event["operation"] = normalized_operation
        event["status"] = normalized_status
        for key, value in normalized_taxonomy.items():
            event[key] = value

        try:
            _append_event(self.settings.log_path, event)
        except Exception:
            # Observability must never change runtime behavior.
            return

    @contextmanager
    def timed(
        self,
        operation: str,
        *,
        level: str = "info",
        start_level: str = "debug",
        start_status: str = "start",
        success_status: str = "success",
        **fields: Any,
    ) -> Iterator[None]:
        start = now_perf()
        self.emit(operation, start_status, level=start_level, **fields)
        try:
            yield
        except Exception as exc:
            self.emit(
                operation,
                "error",
                level="error",
                duration_ms=elapsed_ms(start),
                error=exc,
                **fields,
            )
            raise
        else:
            self.emit(
                operation,
                success_status,
                level=level,
                duration_ms=elapsed_ms(start),
                **fields,
            )


def get_logger(
    component: str,
    config: Optional[Mapping[str, Any]] = None,
    **fields: Any,
) -> StructuredLogger:
    settings = resolve_settings(config)
    return StructuredLogger(component, settings, fields)
