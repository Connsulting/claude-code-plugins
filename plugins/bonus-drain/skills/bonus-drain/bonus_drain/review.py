"""Queue-time readiness reviews and explicit grants: pure validation, no I/O.

A one-off or recurring task is admitted to the queue only with a review recording what
was read before queueing (the source issue, the ADRs it cites, the governing AGENTS.md or
CLAUDE.md files), which acceptance criteria were judged feasible, and how each blocker
knowable at queue time was resolved: an explicit grant, a prerequisite task, a rewritten
done_when, or a launch check.  The queue stamps ``reviewed_at`` and ``review_digest``; the
digest binds the review to the contract it was written against.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from . import checks

if TYPE_CHECKING:
    from .db import Task

GRANT_KINDS = (
    "merge", "release", "sacred_path", "contract_change", "external_infra", "credential", "scope", "other",
)
FINDING_CATEGORIES = ("authority", "contradiction", "feasibility", "external_dependency")
DEPENDENCY_KINDS = ("credential", "provider_credit", "mcp_auth", "cluster_resource", "vendor_capability", "other")
RESOLUTION_KINDS = ("grant", "prerequisite", "done_when", "check")
MAX_GRANTS = 16
MAX_ITEMS = 50
MAX_REVIEW_BYTES = 65_536
MISSING = "readiness review missing"
STALE = "readiness review predates the current contract"

_GRANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_REVIEW_KEYS = frozenset({"issue", "adrs", "instructions", "acceptance_criteria", "findings"})
# Stamped by the queue; ignored on input and overwritten when stored.
_STAMP_KEYS = frozenset({"reviewed_at", "review_digest"})
# The reviewed contract: only these fields are hashed. Routing and display controls (title, priority,
# size, work_group, active, model, mcp, use_implement, claude_only, allowed_providers,
# required_capabilities, created_at) and the review itself are outside it.
REVIEWED_FIELDS = frozenset({
    "id", "kind", "cadence", "cwd", "goal", "context", "constraints", "precondition", "done_when",
    "source_ref", "start_ref", "depends_on", "merged_depends_on", "checks", "grants",
})


class ReviewError(ValueError):
    """A grant or readiness review is malformed."""


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f"{name} must be non-empty text")
    value = value.strip()
    if len(value) > limit:
        raise ReviewError(f"{name} must be at most {limit} characters")
    return value


def _object(value: Any, name: str, required: frozenset[str], optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewError(f"{name} must be an object")
    keys = set(value)
    if unknown := sorted(keys - required - optional):
        raise ReviewError(f"{name} has unknown keys: {', '.join(unknown)}")
    if missing := sorted(required - keys):
        raise ReviewError(f"{name} is missing: {', '.join(missing)}")
    return dict(value)


def _list(value: Any, name: str, *, limit: int = MAX_ITEMS, non_empty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise ReviewError(f"{name} must be a list")
    if non_empty and not value:
        raise ReviewError(f"{name} must not be empty")
    if len(value) > limit:
        raise ReviewError(f"{name} allows at most {limit} items")
    return value


def normalize_grants(raw: Any) -> tuple[str, ...]:
    """Validate grants into canonical JSON per grant, in the order given."""

    if raw is None:
        return ()
    grants = _list(raw, "grants", limit=MAX_GRANTS)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in grants:
        grant = _object(item, "grant", frozenset({"id", "kind", "scope"}))
        grant_id = grant["id"]
        if not isinstance(grant_id, str) or _GRANT_ID_RE.fullmatch(grant_id) is None:
            raise ReviewError("grant id must be 1-64 lowercase letters, digits, '.', '_' or '-'")
        if grant_id in seen:
            raise ReviewError(f"duplicate grant id: {grant_id}")
        seen.add(grant_id)
        if grant["kind"] not in GRANT_KINDS:
            raise ReviewError(f"grant kind must be one of: {', '.join(GRANT_KINDS)}")
        normalized.append(checks.canonical({
            "id": grant_id, "kind": grant["kind"], "scope": _text(grant["scope"], "grant scope", 2_000),
        }))
    return tuple(normalized)


def _resolution(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or len(raw) != 1:
        raise ReviewError(f"finding resolution must be an object with exactly one of: {', '.join(RESOLUTION_KINDS)}")
    (kind, value), = raw.items()
    if kind not in RESOLUTION_KINDS:
        raise ReviewError(f"finding resolution must be one of: {', '.join(RESOLUTION_KINDS)}")
    if kind == "check":
        try:
            return {"check": checks.normalize_check(value)}
        except checks.CheckError as exc:
            raise ReviewError(f"finding resolution check: {exc}") from exc
    limit = 2_000 if kind == "done_when" else 128
    return {kind: _text(value, f"finding resolution {kind}", limit)}


def _finding(raw: Any) -> dict[str, Any]:
    finding = _object(raw, "finding", frozenset({"category", "detail", "resolution"}), frozenset({"dependency"}))
    category = finding["category"]
    if category not in FINDING_CATEGORIES:
        raise ReviewError(f"finding category must be one of: {', '.join(FINDING_CATEGORIES)}")
    value: dict[str, Any] = {
        "category": category,
        "detail": _text(finding["detail"], "finding detail", 2_000),
        "resolution": _resolution(finding["resolution"]),
    }
    if category == "external_dependency":
        if finding.get("dependency") not in DEPENDENCY_KINDS:
            raise ReviewError(
                f"an external_dependency finding needs dependency: one of {', '.join(DEPENDENCY_KINDS)}"
            )
        value["dependency"] = finding["dependency"]
    elif "dependency" in finding:
        raise ReviewError("dependency is only valid on external_dependency findings")
    return value


def normalize_review(raw: Any) -> dict[str, Any]:
    """Validate a review's structure; queue stamps on input are dropped."""

    review = _object(raw, "readiness review", _REVIEW_KEYS, frozenset({"reviewer"}) | _STAMP_KEYS)
    issue = review["issue"]
    value: dict[str, Any] = {
        "issue": None if issue is None else _text(issue, "issue", 300),
        "adrs": [_text(item, "adr", 300) for item in _list(review["adrs"], "adrs")],
        "instructions": [
            _text(item, "instruction file", 500)
            for item in _list(review["instructions"], "instructions", non_empty=True)
        ],
        "acceptance_criteria": [
            {
                "criterion": _text(item["criterion"], "criterion", 2_000),
                "basis": _text(item["basis"], "criterion basis", 2_000),
            }
            for item in (
                _object(entry, "acceptance criterion", frozenset({"criterion", "basis"}))
                for entry in _list(review["acceptance_criteria"], "acceptance_criteria", non_empty=True)
            )
        ],
        "findings": [_finding(item) for item in _list(review["findings"], "findings")],
    }
    if "reviewer" in review:
        value["reviewer"] = _text(review["reviewer"], "reviewer", 200)
    if len(checks.canonical(value).encode("utf-8")) > MAX_REVIEW_BYTES:
        raise ReviewError("readiness review exceeds 64 KiB")
    return value


def contract_digest(task: Task) -> str:
    """sha256 of the contract as reviewed: exactly the REVIEWED_FIELDS of the task."""

    value = {key: item for key, item in task.to_dict().items() if key in REVIEWED_FIELDS}
    return hashlib.sha256(checks.canonical(value).encode("utf-8")).hexdigest()


def review_problems(task: Task) -> list[str]:
    """Cross-check the task's stored review against the task; an empty list means valid."""

    if task.readiness_review is None:
        return [MISSING]
    stored = json.loads(task.readiness_review)
    try:
        review = normalize_review(stored)
    except ReviewError as exc:
        return [f"readiness review is malformed: {exc}"]
    problems: list[str] = []
    grant_ids = {json.loads(item)["id"] for item in task.grants}
    for finding in review["findings"]:
        (kind, target), = finding["resolution"].items()
        if kind == "grant" and target not in grant_ids:
            problems.append(f"finding resolution grant {target} is not a grant on this task")
        elif kind == "prerequisite" and target not in task.depends_on:
            problems.append(f"finding resolution prerequisite {target} is not in depends_on")
        elif kind == "done_when" and target != (task.done_when or "").strip():
            problems.append("finding resolution done_when does not match the task done_when")
        elif kind == "check" and checks.canonical(target) not in task.checks:
            problems.append(f"finding resolution check {checks.describe(target)} is not a task check")
    parsed = checks.parse_issue_ref(task.source_ref)
    if parsed is not None and review["issue"] != f"{parsed[0]}#{parsed[1]}":
        problems.append(f"review issue must be the source issue {parsed[0]}#{parsed[1]}")
    if stored.get("review_digest") != contract_digest(task):
        problems.append(STALE)
    return problems
