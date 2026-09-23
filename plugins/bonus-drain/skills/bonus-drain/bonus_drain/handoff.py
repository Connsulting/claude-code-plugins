"""Select a child starting branch from completed parent repository metadata."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit


_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*$")


class DependencyHandoffError(RuntimeError):
    """A repository dependency cannot be selected without an explicit choice."""

    def __init__(self, reason_code: str, detail: str):
        self.reason_code = reason_code
        self.detail = detail[:500]
        super().__init__(f"{reason_code}: {self.detail}")


@dataclass(frozen=True)
class _RepositoryParent:
    parent_id: str
    remote: str
    target_ref: str
    branch_ref: str
    integration_state: str


def _fail(reason_code: str, detail: str) -> DependencyHandoffError:
    return DependencyHandoffError(reason_code, detail)


def _canonical_remote(value: str) -> str:
    """Canonicalize exact GitHub transports and absolute local paths without Git."""

    raw = value.strip()
    if not raw:
        raise _fail("dependency_ref_unavailable", "repository handoff has no exact remote")
    parsed = urlsplit(raw)
    github_path: str | None = None
    if raw.startswith("git@github.com:"):
        github_path = raw.removeprefix("git@github.com:")
    elif (
        parsed.scheme == "https" and parsed.netloc == "github.com"
        and not parsed.query and not parsed.fragment
    ):
        github_path = parsed.path.removeprefix("/")
    if github_path is not None:
        parts = github_path.split("/")
        if len(parts) == 2:
            owner, repo = parts
            repo = repo.removesuffix(".git")
            if all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) for part in (owner, repo)):
                return f"github.com/{owner}/{repo}"
    if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
        return os.path.normpath(unquote(parsed.path))
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return raw


def normalize_branch_ref(value: Any, label: str = "start_ref") -> str:
    """Accept a branch name or full heads ref and return one full heads ref."""

    if not isinstance(value, str):
        raise _fail("dependency_ref_unavailable", f"{label} must name a branch")
    ref = value if value.startswith("refs/") else f"refs/heads/{value}"
    if not _REF_RE.fullmatch(ref):
        raise _fail("dependency_ref_unavailable", f"{label} is not a valid branch ref")
    if ".." in ref or "//" in ref or ref.endswith((".", ".lock", "/")):
        raise _fail("dependency_ref_unavailable", f"{label} is not a valid branch ref")
    if any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/")):
        raise _fail("dependency_ref_unavailable", f"{label} is not a valid branch ref")
    return ref


def _repository_parent(parent_id: str, value: Any) -> _RepositoryParent | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _fail("dependency_integration_ambiguous", "repository handoff is not an object")
    raw_remote = value.get("remote")
    if not isinstance(raw_remote, str):
        raise _fail("dependency_ref_unavailable", "repository handoff has no exact remote")
    if not isinstance(value.get("target_ref"), str) or not value["target_ref"].startswith("refs/heads/"):
        raise _fail("dependency_ref_unavailable", "target_ref is not an exact branch ref")
    if not isinstance(value.get("branch_ref"), str) or not value["branch_ref"].startswith("refs/heads/"):
        raise _fail("dependency_ref_unavailable", "branch_ref is not an exact branch ref")
    target_ref = normalize_branch_ref(value["target_ref"], "target_ref")
    branch_ref = normalize_branch_ref(value["branch_ref"], "branch_ref")
    integration_state = value.get("integration_state")
    if integration_state not in {"merged", "unmerged"}:
        raise _fail(
            "dependency_integration_ambiguous", "repository integration state is not explicit",
        )
    return _RepositoryParent(
        parent_id, _canonical_remote(raw_remote), target_ref, branch_ref, integration_state,
    )


def resolve_dependency_base(
    parent_outcomes: list[tuple[str, dict[str, Any]]],
    *,
    start_ref: str | None,
) -> dict[str, Any] | None:
    """Select one branch tip using recorded metadata and a child override."""

    parents: list[_RepositoryParent] = []
    for raw_parent_id, outcome in parent_outcomes:
        if not isinstance(outcome, Mapping):
            raise _fail("dependency_integration_ambiguous", "parent outcome is not an object")
        parent = _repository_parent(str(raw_parent_id), outcome.get("repository"))
        if parent is not None:
            parents.append(parent)

    parent_ids = [parent.parent_id for parent in parents]
    if start_ref is not None:
        return {"branch_ref": normalize_branch_ref(start_ref), "parent_ids": parent_ids}
    if not parents:
        return None

    first = parents[0]
    if any(
        parent.remote != first.remote or parent.target_ref != first.target_ref
        for parent in parents[1:]
    ):
        raise _fail(
            "integration_required",
            "repository parents have different remotes or targets; set start_ref on the child",
        )

    unmerged_refs = {
        parent.branch_ref for parent in parents if parent.integration_state == "unmerged"
    }
    if len(unmerged_refs) > 1:
        raise _fail(
            "integration_required",
            "repository parents have different unmerged branches; set start_ref on the child",
        )
    branch_ref = next(iter(unmerged_refs)) if unmerged_refs else first.target_ref
    return {"branch_ref": branch_ref, "parent_ids": parent_ids}
