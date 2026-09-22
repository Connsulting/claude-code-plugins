"""Read-only Git verification for dependency branch handoffs.

The queue stores repository evidence supplied by completed attempts.  This
module treats that evidence as a claim to verify against the child's existing
checkout; it never fetches, checks out, merges, or otherwise changes Git state.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

_OID_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_GIT_TIMEOUT_SECONDS = 15


class DependencyHandoffError(RuntimeError):
    """A repository dependency cannot be selected without unsafe inference."""

    def __init__(self, reason_code: str, detail: str):
        self.reason_code = reason_code
        self.detail = detail[:500]
        super().__init__(f"{reason_code}: {self.detail}")


@dataclass(frozen=True)
class _ResolvedParent:
    parent_id: str
    remote: str
    target_ref: str
    target_oid: str
    branch_ref: str
    base_oid: str
    repository: Mapping[str, Any]


def _fail(reason_code: str, detail: str) -> DependencyHandoffError:
    return DependencyHandoffError(reason_code, detail)


def _git(
    cwd: Path,
    *argv: str,
    check: bool = True,
    binary: bool = False,
) -> subprocess.CompletedProcess[Any]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *argv],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=not binary,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _fail("dependency_ref_unavailable", "local Git identity is unavailable") from exc
    if check and completed.returncode != 0:
        raise _fail("dependency_ref_unavailable", "required Git reference or object is unavailable")
    return completed


def _canonical_remote(value: str, cwd: Path) -> str:
    """Canonicalize local paths while leaving network identities exact."""

    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            return raw
        return str(Path(unquote(parsed.path)).resolve(strict=False))
    if not parsed.scheme and "://" not in raw and not re.match(r"^[^/]+@[^:]+:", raw):
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = cwd / path
        return str(path.resolve(strict=False))
    return raw


def _safe_ref(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _REF_RE.fullmatch(value):
        raise _fail("dependency_ref_unavailable", f"{label} is not an exact branch ref")
    if ".." in value or "//" in value or value.endswith((".", ".lock")):
        raise _fail("dependency_ref_unavailable", f"{label} is not an exact branch ref")
    return value


def _safe_oid(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _OID_RE.fullmatch(value):
        raise _fail("dependency_ref_unavailable", f"{label} is not a full commit object id")
    return value.lower()


def _remote_name(cwd: Path, claimed_remote: str) -> tuple[str, str]:
    canonical_claim = _canonical_remote(claimed_remote, cwd)
    names = _git(cwd, "remote").stdout.splitlines()
    matches: dict[str, str] = {}
    for name in names:
        if not name:
            continue
        completed = _git(cwd, "remote", "get-url", "--all", name, check=False)
        if completed.returncode != 0:
            continue
        for url in completed.stdout.splitlines():
            if _canonical_remote(url, cwd) == canonical_claim:
                matches[name] = url.strip()
    if len(matches) != 1:
        raise _fail(
            "dependency_ref_unavailable",
            "the completed parent remote does not identify one exact configured remote",
        )
    return next(iter(matches.items()))


def _remote_ref_oid(cwd: Path, remote_name: str, ref: str) -> str:
    completed = _git(
        cwd, "ls-remote", "--exit-code", "--refs", remote_name, ref, check=False,
    )
    if completed.returncode != 0:
        raise _fail("dependency_ref_unavailable", "the exact remote branch is unavailable")
    rows = [line.split() for line in completed.stdout.splitlines() if line.strip()]
    exact = [parts for parts in rows if len(parts) == 2 and parts[1] == ref]
    if len(exact) != 1 or not _OID_RE.fullmatch(exact[0][0]):
        raise _fail("dependency_ref_unavailable", "the exact remote branch is ambiguous")
    return exact[0][0].lower()


def _require_commit(cwd: Path, oid: str) -> None:
    completed = _git(cwd, "cat-file", "-e", f"{oid}^{{commit}}", check=False)
    if completed.returncode != 0:
        raise _fail("dependency_ref_unavailable", "a required commit object is unavailable locally")


def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    completed = _git(
        cwd, "merge-base", "--is-ancestor", ancestor, descendant, check=False,
    )
    if completed.returncode not in {0, 1}:
        raise _fail("dependency_ref_unavailable", "commit ancestry could not be verified")
    return completed.returncode == 0


def _changed_paths(cwd: Path, base_oid: str, head_oid: str) -> tuple[bytes, ...]:
    completed = _git(
        cwd, "diff", "--name-only", "--no-renames", "-z", base_oid, head_oid, "--",
        binary=True,
    )
    return tuple(path for path in completed.stdout.split(b"\0") if path)


def _tree_entry(cwd: Path, oid: str, path: bytes) -> bytes:
    # Git paths are byte strings. Decode losslessly so unusual but valid names remain
    # one argv element and can never be interpreted as an option.
    decoded = os.fsdecode(path)
    completed = _git(cwd, "ls-tree", "-z", oid, "--", decoded, binary=True)
    return completed.stdout


def _delta_equivalent(cwd: Path, base_oid: str, head_oid: str, target_oid: str) -> bool:
    for path in _changed_paths(cwd, base_oid, head_oid):
        if _tree_entry(cwd, head_oid, path) != _tree_entry(cwd, target_oid, path):
            return False
    return True


def _repository(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _fail("dependency_integration_ambiguous", "repository handoff is not an object")
    return value


def _resolve_parent(cwd: Path, parent_id: str, repository: Mapping[str, Any]) -> _ResolvedParent:
    raw_remote = repository.get("remote")
    if not isinstance(raw_remote, str) or not raw_remote.strip():
        raise _fail("dependency_ref_unavailable", "repository handoff has no exact remote")
    target_ref = _safe_ref(repository.get("target_ref"), "target_ref")
    branch_ref = _safe_ref(repository.get("branch_ref"), "branch_ref")
    target_base_oid = _safe_oid(repository.get("target_base_oid"), "target_base_oid")
    head_oid = _safe_oid(repository.get("head_oid"), "head_oid")
    integration_state = repository.get("integration_state")
    if integration_state not in {"merged", "unmerged"}:
        raise _fail(
            "dependency_integration_ambiguous", "repository integration state is not explicit",
        )

    remote_name, configured_remote = _remote_name(cwd, raw_remote)
    target_oid = _remote_ref_oid(cwd, remote_name, target_ref)
    for oid in (target_base_oid, head_oid, target_oid):
        _require_commit(cwd, oid)
    if not _is_ancestor(cwd, target_base_oid, head_oid):
        raise _fail(
            "dependency_integration_ambiguous",
            "the completed parent head is not based on its recorded target base",
        )

    equivalent = _delta_equivalent(cwd, target_base_oid, head_oid, target_oid)
    receipt = repository.get("merge_receipt")
    if receipt is not None:
        if integration_state != "merged" or not isinstance(receipt, Mapping):
            raise _fail("dependency_integration_ambiguous", "merge receipt conflicts with handoff state")
        if receipt.get("kind") not in {"merge", "squash"}:
            raise _fail("dependency_integration_ambiguous", "merge receipt kind is unsupported")
        result_oid = _safe_oid(receipt.get("result_oid"), "merge_receipt.result_oid")
        _require_commit(cwd, result_oid)
        if not _is_ancestor(cwd, result_oid, target_oid) or not equivalent:
            raise _fail(
                "dependency_integration_ambiguous",
                "merge receipt does not prove the parent delta on current target history",
            )
        selected_oid, selected_ref = target_oid, target_ref
    elif equivalent or _is_ancestor(cwd, head_oid, target_oid):
        # A later commit may touch a generated file such as the ADR index.
        # Ancestry proves the recorded head is already on the target, so the
        # child starts there instead of the stale unmerged branch tip.
        selected_oid, selected_ref = target_oid, target_ref
    elif integration_state == "unmerged":
        # Only a head that is not yet on the target needs its branch. A merged
        # parent's branch is routinely deleted, so it is checked only here.
        branch_oid = _remote_ref_oid(cwd, remote_name, branch_ref)
        _require_commit(cwd, branch_oid)
        if branch_oid != head_oid:
            raise _fail(
                "dependency_integration_ambiguous",
                "the completed parent head does not match its exact branch ref",
            )
        selected_oid, selected_ref = head_oid, branch_ref
    else:
        raise _fail(
            "dependency_integration_ambiguous",
            "merged parent evidence does not match current target content",
        )

    return _ResolvedParent(
        parent_id=parent_id,
        remote=_canonical_remote(configured_remote, cwd),
        target_ref=target_ref,
        target_oid=target_oid,
        branch_ref=selected_ref,
        base_oid=selected_oid,
        repository=repository,
    )


def _contains_all(cwd: Path, candidate: _ResolvedParent, parents: Sequence[_ResolvedParent]) -> bool:
    return all(
        other.base_oid == candidate.base_oid
        or _is_ancestor(cwd, other.base_oid, candidate.base_oid)
        for other in parents
    )


def resolve_dependency_base(
    cwd: str,
    parent_outcomes: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any] | None:
    """Resolve one proven base from structured parent terminal outcomes.

    Parents without repository output do not constrain the Git base.  Every
    repository-producing parent must resolve against the same child checkout,
    and multiple heads must form one ancestry chain; this function never creates
    an integration commit for divergent work.
    """

    repositories: list[tuple[str, Mapping[str, Any]]] = []
    for raw_parent_id, outcome in parent_outcomes:
        parent_id = str(raw_parent_id)
        if not isinstance(outcome, Mapping):
            raise _fail("dependency_integration_ambiguous", "parent outcome is not an object")
        repository = _repository(outcome.get("repository"))
        if repository is not None:
            repositories.append((parent_id, repository))
    if not repositories:
        return None

    worktree = Path(cwd).expanduser().resolve(strict=False)
    if not worktree.is_dir():
        raise _fail("dependency_ref_unavailable", "child working directory is unavailable")
    inside = _git(worktree, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise _fail("dependency_ref_unavailable", "child working directory is not a Git checkout")

    resolved: list[_ResolvedParent] = []
    for parent_id, repository in repositories:
        resolved.append(_resolve_parent(worktree, parent_id, repository))

    first = resolved[0]
    if any(
        parent.remote != first.remote
        or parent.target_ref != first.target_ref
        or parent.target_oid != first.target_oid
        for parent in resolved[1:]
    ):
        raise _fail("integration_required", "repository parents do not share one exact target")

    candidates = [parent for parent in resolved if _contains_all(worktree, parent, resolved)]
    if not candidates:
        raise _fail("integration_required", "repository parent heads are divergent")
    # Equivalent candidates may carry different refs for the same object. Prefer
    # the exact target ref when available, then retain dependency order.
    selected = next(
        (candidate for candidate in candidates if candidate.branch_ref == candidate.target_ref),
        candidates[0],
    )
    return {
        "base_oid": selected.base_oid,
        "branch_ref": selected.branch_ref,
        "target_ref": selected.target_ref,
        "parent_ids": [parent.parent_id for parent in resolved],
    }
