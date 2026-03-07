"""
Event taxonomy normalization for compound-learning observability.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Tuple

_TOKEN_PATTERN = re.compile(r"[^a-z0-9]+")

CANONICAL_STATUSES = {
    "start",
    "success",
    "error",
    "skipped",
    "empty",
    "degraded",
}

STATUS_ALIASES = {
    "ok": "success",
    "loaded": "success",
    "found": "success",
    "hit": "success",
    "write": "success",
    "failure": "error",
    "failed": "error",
    "write_failed": "error",
    "exception": "error",
    "bypass": "skipped",
    "miss": "empty",
    "missing": "empty",
    "not_found": "empty",
    "fallback": "degraded",
    "stale": "degraded",
}

OPERATION_ALIASES = {
    "hook_start": "hook",
    "hook_end": "hook",
    "extract_start": "extract",
    "extract_complete": "extract",
    "search_request": "search",
    "search_complete": "search",
    "search_result": "search",
}


def _normalize_token(value: Any, *, default: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    normalized = _TOKEN_PATTERN.sub("_", raw).strip("_")
    return normalized or default


def normalize_operation(operation: Any) -> Tuple[str, str | None]:
    normalized = _normalize_token(operation, default="unknown_operation")
    canonical = OPERATION_ALIASES.get(normalized, normalized)
    operation_alias = normalized if canonical != normalized else None
    return canonical, operation_alias


def normalize_status(status: Any) -> Tuple[str, str | None]:
    normalized = _normalize_token(status, default="success")
    if normalized in CANONICAL_STATUSES:
        return normalized, None
    canonical = STATUS_ALIASES.get(normalized, "degraded")
    status_alias = normalized if canonical != normalized else None
    return canonical, status_alias


def normalize_event_taxonomy(operation: Any, status: Any) -> Dict[str, Any]:
    canonical_operation, operation_alias = normalize_operation(operation)
    canonical_status, status_alias = normalize_status(status)

    fields: Dict[str, Any] = {
        "operation": canonical_operation,
        "status": canonical_status,
    }
    if operation_alias:
        fields["operation_alias"] = operation_alias
    if status_alias:
        fields["status_alias"] = status_alias
    return fields
