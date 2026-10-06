"""Queue-time readiness reviews and explicit grants: pure validation, no I/O.

A one-off or recurring task is admitted to the queue only with the readiness record of the
implementable-ticket contract (``implementable-ticket/v1``): what was read before queueing
(the source issue, the ADRs it cites, the governing AGENTS.md or CLAUDE.md files), a yes
verdict with evidence on "can it start" and "can it finish", every acceptance criterion with
the command that verifies it and where that runs, every merge gate the change will trigger
and how it is satisfied, and how each blocker knowable at queue time was resolved.  A task is queueable only if it can start and finish with no human in the
loop, so a finding is never resolved by waiting on a person: an authority finding needs a
grant already on the task or a rewritten done_when.  The queue stamps ``reviewed_at`` and
``review_digest``; the digest binds the review to the contract it was written against.
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
# Authority is Brian's decision. Only a decision already made (a grant on the task) or a done_when
# that no longer needs it resolves it; a check or a prerequisite would wait on a human.
AUTHORITY_RESOLUTIONS = ("grant", "done_when")
CONTRACT = "implementable-ticket/v1"
EXECUTORS = ("bonus-drain", "dark-factory")
ENVIRONMENTS = ("worker", "kind", "k8_namespace", "pr_ci", "factory_runner")
VERDICTS = ("yes", "no")
QUESTIONS = (("startable", "can it start?"), ("finishable", "can it finish?"))
MAX_GRANTS = 16
MAX_ITEMS = 50
MAX_REVIEW_BYTES = 65_536
MISSING = "readiness review missing"
STALE = "readiness review predates the current contract"
SCHEMA_STALE = (
    f"readiness review predates the {CONTRACT} contract; re-run the implementable-ticket skill "
    "and edit the task with its record as readiness_review"
)
NOT_IMPLEMENTABLE = "not implementable"

_GRANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_REVIEW_KEYS = frozenset({
    "contract", "executor", "issue", "adrs", "instructions", "startable", "finishable",
    "acceptance_criteria", "findings",
})
# Ticket work (a review with an issue) ends in a PR, so it must also list merge_gates.
_TICKET_KEYS = frozenset({"merge_gates"})
_ISSUE_REF_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")
MERGE_GATES_MISSING = (
    "ticket work must list merge_gates: every repository gate the change's paths trigger (a PR-body "
    "evidence tier, a required live run, a path-scoped check) with the command that satisfies it and "
    "a probe showing that command does the gate's work on the base, or a waiver naming an open issue; "
    "[] only when the repository has no gate beyond its PR checks"
)
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
    """A grant or readiness review is malformed, or records a task that is not implementable."""


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
    (kind, _target), = value["resolution"].items()
    if category == "authority" and kind not in AUTHORITY_RESOLUTIONS:
        raise ReviewError(
            f"an authority finding cannot be resolved by a {kind}: that waits on a human. Resolve it "
            "with a grant Brian already gave (on this task) or rewrite done_when so it needs no "
            f"authority ({value['detail']})"
        )
    if category == "external_dependency":
        if finding.get("dependency") not in DEPENDENCY_KINDS:
            raise ReviewError(
                f"an external_dependency finding needs dependency: one of {', '.join(DEPENDENCY_KINDS)}"
            )
        value["dependency"] = finding["dependency"]
    elif "dependency" in finding:
        raise ReviewError("dependency is only valid on external_dependency findings")
    return value


def _merge_gate(raw: Any) -> dict[str, Any]:
    gate = _object(raw, "merge gate", frozenset({"gate", "required_by", "satisfied_by"}))
    value: dict[str, Any] = {
        "gate": _text(gate["gate"], "merge gate name", 200),
        "required_by": _text(gate["required_by"], "merge gate required_by", 2_000),
    }
    satisfied = gate["satisfied_by"]
    if isinstance(satisfied, Mapping) and "waiver" in satisfied:
        satisfied = _object(satisfied, "merge gate waiver", frozenset({"waiver", "detail"}))
        waiver = satisfied["waiver"]
        if not isinstance(waiver, str) or _ISSUE_REF_RE.fullmatch(waiver.strip()) is None:
            raise ReviewError("merge gate waiver must name an issue as owner/repo#number")
        value["satisfied_by"] = {
            "waiver": waiver.strip(), "detail": _text(satisfied["detail"], "merge gate waiver detail", 2_000),
        }
        return value
    satisfied = _object(satisfied, "merge gate satisfied_by", frozenset({"command", "probe"}))
    value["satisfied_by"] = {
        "command": _text(satisfied["command"], "merge gate command", 2_000),
        "probe": _text(satisfied["probe"], "merge gate probe", 2_000),
    }
    return value


def waiver_check(gate: Mapping[str, Any]) -> dict[str, Any] | None:
    """The issue_open check a waived gate depends on: the waiver lapses when its issue closes."""

    waiver = gate["satisfied_by"].get("waiver")
    if waiver is None:
        return None
    repo, number = _ISSUE_REF_RE.fullmatch(waiver).groups()
    return {"type": "issue_open", "repo": repo, "number": int(number)}


def _verdict(raw: Any, name: str) -> dict[str, Any]:
    verdict = _object(raw, name, frozenset({"verdict", "evidence"}))
    if verdict["verdict"] not in VERDICTS:
        raise ReviewError(f"{name} verdict must be yes or no")
    evidence = [
        _text(item, f"{name} evidence", 2_000)
        for item in _list(verdict["evidence"], f"{name} evidence", non_empty=True)
    ]
    return {"verdict": verdict["verdict"], "evidence": evidence}


def _criterion(raw: Any, index: int) -> dict[str, Any]:
    entry = _object(
        raw, "acceptance criterion", frozenset({"criterion", "basis", "environment"}),
        frozenset({"verified_by"}),
    )
    criterion = _text(entry["criterion"], "criterion", 2_000)
    verified_by = entry.get("verified_by")
    if not isinstance(verified_by, str) or not verified_by.strip():
        raise ReviewError(
            f"{NOT_IMPLEMENTABLE}: can it finish? no. Acceptance criterion {index} has no verified_by, "
            f"so nothing the executor can run proves it ({criterion})"
        )
    if entry["environment"] not in ENVIRONMENTS:
        raise ReviewError(f"acceptance criterion environment must be one of: {', '.join(ENVIRONMENTS)}")
    return {
        "criterion": criterion,
        "basis": _text(entry["basis"], "criterion basis", 2_000),
        "verified_by": _text(verified_by, "criterion verified_by", 2_000),
        "environment": entry["environment"],
    }


def normalize_review(raw: Any) -> dict[str, Any]:
    """Validate a review's structure; queue stamps on input are dropped.

    A review of an earlier schema (no ``contract``) is reported as stale, not malformed.
    """

    if isinstance(raw, Mapping) and raw.get("contract") != CONTRACT:
        if "contract" not in raw:
            raise ReviewError(SCHEMA_STALE)
        raise ReviewError(f"readiness review contract must be {CONTRACT}")
    review = _object(
        raw, "readiness review", _REVIEW_KEYS, frozenset({"reviewer"}) | _TICKET_KEYS | _STAMP_KEYS,
    )
    if review["executor"] not in EXECUTORS:
        raise ReviewError(f"readiness review executor must be one of: {', '.join(EXECUTORS)}")
    issue = review["issue"]
    value: dict[str, Any] = {
        "contract": CONTRACT,
        "executor": review["executor"],
        "issue": None if issue is None else _text(issue, "issue", 300),
        "adrs": [_text(item, "adr", 300) for item in _list(review["adrs"], "adrs")],
        "instructions": [
            _text(item, "instruction file", 500)
            for item in _list(review["instructions"], "instructions", non_empty=True)
        ],
        **{name: _verdict(review[name], name) for name, _question in QUESTIONS},
        "acceptance_criteria": [
            _criterion(entry, index)
            for index, entry in enumerate(
                _list(review["acceptance_criteria"], "acceptance_criteria", non_empty=True), start=1,
            )
        ],
        "findings": [_finding(item) for item in _list(review["findings"], "findings")],
    }
    if "merge_gates" in review:
        value["merge_gates"] = [_merge_gate(item) for item in _list(review["merge_gates"], "merge_gates")]
    elif value["issue"] is not None:
        raise ReviewError(MERGE_GATES_MISSING)
    if "reviewer" in review:
        value["reviewer"] = _text(review["reviewer"], "reviewer", 200)
    if len(checks.canonical(value).encode("utf-8")) > MAX_REVIEW_BYTES:
        raise ReviewError("readiness review exceeds 64 KiB")
    return value


def verdict_refusal(review: Mapping[str, Any]) -> str | None:
    """The refusal for a normalized review whose start or finish verdict is not yes."""

    for name, question in QUESTIONS:
        verdict = review[name]
        if verdict["verdict"] != "yes":
            return (
                f"{NOT_IMPLEMENTABLE}: {question} {verdict['verdict']}. Evidence: "
                + "; ".join(verdict["evidence"])
                + ". A task that cannot start and finish with no human in the loop is not queued"
            )
    return None


def schema_stale(task: Task) -> bool:
    """True when the task carries a review written before the current review contract."""

    if task.readiness_review is None:
        return False
    stored = json.loads(task.readiness_review)
    return not isinstance(stored, Mapping) or stored.get("contract") != CONTRACT


def merge_gates_missing(task: Task) -> bool:
    """True when the task carries a ticket review (one with an issue) that lists no merge_gates."""

    if task.readiness_review is None or schema_stale(task):
        return False
    stored = json.loads(task.readiness_review)
    return stored.get("issue") is not None and "merge_gates" not in stored


def _has_waiver_check(task: Task, check: Mapping[str, Any]) -> bool:
    """GitHub repository names are case-insensitive, so the waiver's repo matches in any case."""

    for raw in task.checks:
        spec = json.loads(raw)
        if (
            spec.get("type") == "issue_open" and spec.get("number") == check["number"]
            and str(spec.get("repo", "")).lower() == check["repo"].lower()
        ):
            return True
    return False


def contract_digest(task: Task) -> str:
    """sha256 of the contract as reviewed: exactly the REVIEWED_FIELDS of the task."""

    value = {key: item for key, item in task.to_dict().items() if key in REVIEWED_FIELDS}
    return hashlib.sha256(checks.canonical(value).encode("utf-8")).hexdigest()


def review_problems(task: Task) -> list[str]:
    """Cross-check the task's stored review against the task; an empty list means valid."""

    if task.readiness_review is None:
        return [MISSING]
    if schema_stale(task):
        return [SCHEMA_STALE]
    stored = json.loads(task.readiness_review)
    try:
        review = normalize_review(stored)
    except ReviewError as exc:
        if str(exc).startswith(NOT_IMPLEMENTABLE):
            return [str(exc)]
        return [f"readiness review is malformed: {exc}"]
    problems: list[str] = []
    if (refusal := verdict_refusal(review)) is not None:
        problems.append(refusal)
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
    for gate in review.get("merge_gates", ()):
        if (check := waiver_check(gate)) is not None and not _has_waiver_check(task, check):
            problems.append(
                f"merge gate {gate['gate']} is waived by {gate['satisfied_by']['waiver']}, so the task "
                f"needs the check {checks.describe(check)}: the waiver lapses when that issue closes"
            )
    parsed = checks.parse_issue_ref(task.source_ref)
    if parsed is not None and review["issue"] != f"{parsed[0]}#{parsed[1]}":
        problems.append(f"review issue must be the source issue {parsed[0]}#{parsed[1]}")
    if stored.get("review_digest") != contract_digest(task):
        problems.append(STALE)
    return problems
