"""Structured preflight checks: validation, evaluation, and the scout-side refresher.

Only ``evaluate`` and ``refresh`` run tools, always through an injectable runner.  Queue
readers (readiness, eligibility, claim, the viewer) read stored results and never call
into this module's network paths, so a view load never touches ``gh`` or ``git``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence
from urllib.parse import quote

from .handoff import DependencyHandoffError, _canonical_remote, normalize_branch_ref


CHECK_FIELDS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "base_ref_exists": (frozenset({"ref"}), frozenset()),
    "issue_open": (frozenset({"repo", "number"}), frozenset()),
    "issue_in_milestone": (frozenset({"repo", "number", "milestone"}), frozenset()),
    "pr_merged": (frozenset({"repo"}), frozenset({"pr", "head", "base"})),
    "release_exists": (frozenset({"repo", "tag"}), frozenset()),
    "file_matches": (frozenset({"repo", "ref", "path", "pattern"}), frozenset({"present"})),
}
MAX_CHECKS_PER_TASK = 16
# A stored result older than this reads as pending for gating, claim, prompt, and resume.
RESULT_TTL_SECONDS = 3600
# Passes are re-evaluated before they lapse, so a healthy refresher never lets one expire.
PASS_REFRESH_SECONDS = 2700
RETRY_TTL_SECONDS = 600
# Unknown (tool or network error) blocks for this long, then the launch proceeds unverified.
UNKNOWN_GRACE_SECONDS = 3600
# The scout tick's cap counts tool calls, not checks: one call can answer several checks.
TICK_MAX_CALLS = 60
TICK_BUDGET_SECONDS = 120.0
CALL_TIMEOUT_SECONDS = 20.0
_STDOUT_CAP = 1 << 20

_SLUG = r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*"
_REPO_RE = re.compile(rf"^{_SLUG}$")
_ISSUE_REF_RE = re.compile(
    rf"(?:https://github\.com/(?P<url_repo>{_SLUG})/issues/(?P<url_number>[0-9]+)"
    rf"|(?P<short_repo>{_SLUG})#(?P<short_number>[0-9]+))(?=$|[\s,;])"
)
_REMOTE_TRACKING_PREFIXES = ("refs/heads/origin/", "refs/heads/upstream/", "refs/heads/remotes/")


class CheckError(ValueError):
    """A check specification is invalid."""


class CheckToolError(RuntimeError):
    """The runner could not run the tool at all (missing binary, timeout)."""


@dataclass(frozen=True)
class RunnerResult:
    returncode: int
    stdout: str
    stderr: str


CheckRunner = Callable[[Sequence[str], "str | None", float], RunnerResult]


@dataclass(frozen=True)
class CheckResult:
    status: Literal["pass", "fail", "unknown"]
    detail: str


@dataclass(frozen=True)
class CheckWork:
    """One due evaluation; ``origin`` is declared, builtin, dependency, or resume."""

    task_id: str
    cwd: str
    origin: str
    spec: Mapping[str, Any]
    context: Mapping[str, Any]
    check_id: str


def subprocess_runner(argv: Sequence[str], cwd: str | None, timeout: float) -> RunnerResult:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    try:
        completed = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout,
            check=False, stdin=subprocess.DEVNULL, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckToolError(f"{argv[0]} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise CheckToolError(f"{argv[0]} could not run: {exc}") from exc
    return RunnerResult(
        completed.returncode, (completed.stdout or "")[:_STDOUT_CAP], completed.stderr or "",
    )


def canonical(spec: Mapping[str, Any]) -> str:
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))


def normalize_queue_ref(value: Any, label: str) -> str:
    """Return a full heads ref, rejecting remote-tracking names written as branches."""

    try:
        ref = normalize_branch_ref(value, label)
    except DependencyHandoffError as exc:
        raise CheckError(exc.detail) from exc
    if ref == "refs/heads/HEAD" or ref.startswith(_REMOTE_TRACKING_PREFIXES):
        raise CheckError(
            f"{label} names a remote-tracking ref; use the branch name "
            "(for example main), not origin/main"
        )
    return ref


def _short(ref: str) -> str:
    return ref.removeprefix("refs/heads/")


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CheckError(f"{name} must be non-empty text of at most {limit} characters")
    return value


def _positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise CheckError(f"{name} must be a positive integer")
    return value


def normalize_check(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CheckError("a check must be a JSON object")
    kind = raw.get("type")
    if kind not in CHECK_FIELDS:
        raise CheckError(f"unsupported check type: {kind!r}; use one of {', '.join(sorted(CHECK_FIELDS))}")
    required, optional = CHECK_FIELDS[kind]
    fields = set(raw) - {"type"}
    if missing := required - fields:
        raise CheckError(f"{kind} requires {', '.join(sorted(missing))}")
    if extra := fields - required - optional:
        raise CheckError(f"{kind} does not accept {', '.join(sorted(extra))}")
    spec: dict[str, Any] = {"type": kind}
    if "repo" in required:
        repo = raw["repo"]
        if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
            raise CheckError("repo must be owner/repo")
        spec["repo"] = repo
    if kind == "base_ref_exists":
        spec["ref"] = normalize_queue_ref(raw["ref"], "ref")
    elif kind in {"issue_open", "issue_in_milestone"}:
        spec["number"] = _positive_int(raw["number"], "number")
        if kind == "issue_in_milestone":
            spec["milestone"] = _text(raw["milestone"], "milestone", 200)
    elif kind == "pr_merged":
        if ("pr" in raw) == ("head" in raw):
            raise CheckError("pr_merged requires exactly one of pr or head")
        if "pr" in raw:
            spec["pr"] = _positive_int(raw["pr"], "pr")
        else:
            spec["head"] = _short(normalize_queue_ref(raw["head"], "head"))
        if "base" in raw:
            spec["base"] = _short(normalize_queue_ref(raw["base"], "base"))
    elif kind == "release_exists":
        spec["tag"] = _text(raw["tag"], "tag", 200)
    else:
        spec["ref"] = normalize_queue_ref(raw["ref"], "ref")
        path = _text(raw["path"], "path", 500)
        if path.startswith("/") or ".." in path.split("/"):
            raise CheckError("path must be relative and must not contain ..")
        spec["path"] = path
        pattern = _text(raw["pattern"], "pattern", 500)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise CheckError(f"pattern is not a valid regular expression: {exc}") from exc
        spec["pattern"] = pattern
        present = raw.get("present", True)
        if not isinstance(present, bool):
            raise CheckError("present must be true or false")
        # The default is omitted so equivalent specs share one canonical form.
        if present is False:
            spec["present"] = False
    return spec


def normalize_checks(raw: Any) -> tuple[str, ...]:
    """Validate a check list into canonical JSON strings, de-duplicated in order."""

    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise CheckError("checks must be a list of check objects")
    if len(raw) > MAX_CHECKS_PER_TASK:
        raise CheckError(f"at most {MAX_CHECKS_PER_TASK} checks are allowed")
    return tuple(dict.fromkeys(canonical(normalize_check(item)) for item in raw))


def check_context(spec: Mapping[str, Any], *, cwd: str) -> dict[str, Any]:
    """Repository context that changes the answer; only base_ref_exists reads the checkout."""

    if spec.get("type") == "base_ref_exists":
        return {"cwd": os.path.realpath(cwd)}
    return {}


def check_id(spec: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical({"spec": spec, "context": context}).encode("utf-8")
    ).hexdigest()[:16]


def _origin_argv(cwd: str) -> tuple[str, ...]:
    return ("git", "-C", cwd, "remote", "get-url", "origin")


def _ref_argv(spec: Mapping[str, Any], cwd: str) -> tuple[str, ...]:
    return ("git", "-C", cwd, "ls-remote", "--exit-code", "--heads", "origin", spec["ref"])


def _issue_argv(spec: Mapping[str, Any]) -> tuple[str, ...]:
    # issue_open and issue_in_milestone read the same fields, so one call answers both.
    return ("gh", "issue", "view", str(spec["number"]), "-R", spec["repo"], "--json", "state,milestone")


def _pr_selector(spec: Mapping[str, Any]) -> str:
    return str(spec["pr"]) if "pr" in spec else str(spec["head"])


def _pr_argv(spec: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        "gh", "pr", "view", _pr_selector(spec), "-R", spec["repo"],
        "--json", "number,state,mergedAt,baseRefName,headRefName",
    )


def _release_argv(spec: Mapping[str, Any]) -> tuple[str, ...]:
    return ("gh", "release", "view", spec["tag"], "-R", spec["repo"], "--json", "tagName,isDraft,publishedAt")


def _file_argv(spec: Mapping[str, Any]) -> tuple[str, ...]:
    ref = _short(spec["ref"])
    return (
        "gh", "api", "-H", "Accept: application/vnd.github.raw",
        f"repos/{spec['repo']}/contents/{quote(spec['path'])}?ref={quote(ref)}",
    )


def planned_calls(spec: Mapping[str, Any], cwd: str) -> tuple[tuple[str, ...], ...]:
    """Every argv that evaluating ``spec`` (plus its observed context) will run.

    Built from the same helpers the evaluators use, so planning cannot drift from evaluation.
    """

    kind = spec.get("type")
    if kind == "base_ref_exists":
        return (_ref_argv(spec, cwd), _origin_argv(cwd))
    if kind in {"issue_open", "issue_in_milestone"}:
        return (_issue_argv(spec),)
    if kind == "pr_merged":
        return (_pr_argv(spec),)
    if kind == "release_exists":
        return (_release_argv(spec),)
    if kind == "file_matches":
        return (_file_argv(spec),)
    return ()


def origin_url(cwd: str, runner: CheckRunner) -> str | None:
    """Read the checkout's origin URL locally (no network); None when unavailable."""

    try:
        result = runner(_origin_argv(cwd), None, CALL_TIMEOUT_SECONDS)
    except CheckToolError:
        return None
    url = result.stdout.strip()
    return url if result.returncode == 0 and url else None


def parse_issue_ref(source_ref: str | None) -> tuple[str, int] | None:
    """Return (owner/repo, number) when source_ref starts with an issue reference."""

    if not isinstance(source_ref, str):
        return None
    match = _ISSUE_REF_RE.match(source_ref.strip())
    if match is None:
        return None
    repo = match.group("url_repo") or match.group("short_repo")
    number = int(match.group("url_number") or match.group("short_number"))
    return (repo, number) if number > 0 else None


def github_slug(remote: Any) -> str | None:
    if not isinstance(remote, str):
        return None
    try:
        canonical_remote = _canonical_remote(remote)
    except DependencyHandoffError:
        return None
    if not canonical_remote.startswith("github.com/"):
        return None
    return canonical_remote.removeprefix("github.com/")


def describe(spec: Mapping[str, Any]) -> str:
    kind = spec.get("type")
    if kind == "base_ref_exists":
        return f"base_ref_exists {spec.get('ref')}"
    if kind == "issue_open":
        return f"issue_open {spec.get('repo')}#{spec.get('number')}"
    if kind == "issue_in_milestone":
        return f"issue_in_milestone {spec.get('repo')}#{spec.get('number')} in {spec.get('milestone')}"
    if kind == "pr_merged":
        selector = f"#{spec['pr']}" if "pr" in spec else f" {spec.get('head')}"
        into = f" into {spec['base']}" if spec.get("base") else ""
        return f"pr_merged {spec.get('repo')}{selector}{into}"
    if kind == "release_exists":
        return f"release_exists {spec.get('repo')}@{spec.get('tag')}"
    if kind == "file_matches":
        absent = " (absent)" if spec.get("present") is False else ""
        return (
            f"file_matches {spec.get('repo')}:{spec.get('path')}@{_short(str(spec.get('ref')))} "
            f"/{spec.get('pattern')}/{absent}"
        )
    return canonical(spec)


def check_type_reference() -> dict[str, dict[str, list[str]]]:
    return {
        kind: {"required": sorted(required), "optional": sorted(optional)}
        for kind, (required, optional) in sorted(CHECK_FIELDS.items())
    }


def _last_line(result: RunnerResult) -> str:
    for text in (result.stderr, result.stdout):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[-1][:500]
    return f"exit {result.returncode}"


def _json_object(result: RunnerResult) -> dict[str, Any] | None:
    try:
        value = json.loads(result.stdout)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _unknown(detail: str) -> CheckResult:
    return CheckResult("unknown", detail[:500])


def _evaluate_ref(spec: Mapping[str, Any], cwd: str, runner: CheckRunner) -> CheckResult:
    ref = spec["ref"]
    result = runner(_ref_argv(spec, cwd), None, CALL_TIMEOUT_SECONDS)
    if result.returncode == 0:
        listed = {line.split("\t", 1)[-1].strip() for line in result.stdout.splitlines()}
        if ref in listed:
            return CheckResult("pass", f"{ref} exists on origin")
        return _unknown(f"ls-remote did not list {ref}")
    if result.returncode == 2:
        return CheckResult("fail", f"ref {ref} is missing on origin")
    return _unknown(_last_line(result))


def _gh_view(
    argv: Sequence[str], not_found_markers: Sequence[str], not_found: str, label: str,
    runner: CheckRunner,
) -> dict[str, Any] | CheckResult:
    """Run one ``gh ... view --json`` call: parsed object, or a fail/unknown result.

    A nonzero exit whose output names a not-found marker fails, any other nonzero exit is
    unknown, and output that is not a JSON object is unknown.
    """

    result = runner(argv, None, CALL_TIMEOUT_SECONDS)
    if result.returncode != 0:
        output = result.stdout + result.stderr
        if any(marker in output for marker in not_found_markers):
            return CheckResult("fail", not_found)
        return _unknown(_last_line(result))
    value = _json_object(result)
    return value if value is not None else _unknown(f"gh {label} view returned unparseable output")


def _evaluate_issue(spec: Mapping[str, Any], runner: CheckRunner) -> CheckResult:
    value = _gh_view(
        _issue_argv(spec),
        ("Could not resolve to an issue",), "issue not found", "issue", runner,
    )
    if isinstance(value, CheckResult):
        return value
    state = value.get("state")
    if not isinstance(state, str):
        return _unknown("gh issue view returned unparseable output")
    if spec["type"] == "issue_open":
        if state == "OPEN":
            return CheckResult("pass", "issue is OPEN")
        return CheckResult("fail", f"issue is {state}")
    milestone = value.get("milestone")
    title = milestone.get("title") if isinstance(milestone, dict) else None
    if title == spec["milestone"]:
        return CheckResult("pass", f"issue milestone is {title}")
    return CheckResult("fail", f"issue milestone is {title or 'none'}, not {spec['milestone']}")


def _evaluate_pr(spec: Mapping[str, Any], runner: CheckRunner) -> CheckResult:
    selector = _pr_selector(spec)
    value = _gh_view(
        _pr_argv(spec),
        ("no pull requests found", "Could not resolve"), "no pull request found", "pr", runner,
    )
    if isinstance(value, CheckResult):
        return value
    state = value.get("state")
    if not isinstance(state, str):
        return _unknown("gh pr view returned unparseable output")
    number = value.get("number", selector)
    if state != "MERGED":
        return CheckResult("fail", f"PR #{number} is {state}")
    base = spec.get("base")
    merged_into = value.get("baseRefName")
    if base and merged_into != base:
        return CheckResult("fail", f"PR #{number} merged into {merged_into}, not {base}")
    return CheckResult("pass", f"PR #{number} merged into {merged_into}")


def _evaluate_release(spec: Mapping[str, Any], runner: CheckRunner) -> CheckResult:
    value = _gh_view(
        _release_argv(spec),
        ("release not found",), "release not found", "release", runner,
    )
    if isinstance(value, CheckResult):
        return value
    if not isinstance(value.get("isDraft"), bool):
        return _unknown("gh release view returned unparseable output")
    if value["isDraft"]:
        return CheckResult("fail", "release is a draft")
    if not value.get("publishedAt"):
        return CheckResult("fail", "release is not published")
    return CheckResult("pass", f"release {spec['tag']} is published")


def _evaluate_file(spec: Mapping[str, Any], runner: CheckRunner) -> CheckResult:
    ref = _short(spec["ref"])
    result = runner(_file_argv(spec), None, CALL_TIMEOUT_SECONDS)
    if result.returncode == 0:
        found = True
        matched = re.search(spec["pattern"], result.stdout, re.MULTILINE) is not None
    elif "HTTP 404" in result.stdout + result.stderr:
        found, matched = False, False
    else:
        return _unknown(_last_line(result))
    present = spec.get("present", True)
    where = f"{spec['path']} on {ref}"
    if matched == present:
        return CheckResult("pass", f"{where} {'matches' if matched else 'does not match'} the pattern")
    if not found:
        return CheckResult("fail", f"{where} does not exist")
    return CheckResult("fail", f"{where} {'matches' if matched else 'does not match'} the pattern")


def evaluate(spec: Mapping[str, Any], *, cwd: str, runner: CheckRunner) -> CheckResult:
    """Run one check; tool failures and unparseable output are unknown, never raised."""

    try:
        kind = spec.get("type")
        if kind == "base_ref_exists":
            return _evaluate_ref(spec, cwd, runner)
        if kind in {"issue_open", "issue_in_milestone"}:
            return _evaluate_issue(spec, runner)
        if kind == "pr_merged":
            return _evaluate_pr(spec, runner)
        if kind == "release_exists":
            return _evaluate_release(spec, runner)
        if kind == "file_matches":
            return _evaluate_file(spec, runner)
    except CheckToolError as exc:
        return _unknown(str(exc) or "check tool error")
    return _unknown(f"unsupported check type: {kind}")


class _MemoRunner:
    """Run each (argv, cwd) key once per component; a CheckToolError is memoized too."""

    def __init__(self, runner: CheckRunner):
        self._runner = runner
        self._memo: dict[tuple[tuple[str, ...], str | None], RunnerResult | CheckToolError] = {}

    @property
    def calls(self) -> int:
        return len(self._memo)

    def __call__(self, argv: Sequence[str], cwd: str | None, timeout: float) -> RunnerResult:
        key = (tuple(argv), cwd)
        if key not in self._memo:
            try:
                self._memo[key] = self._runner(key[0], cwd, timeout)
            except CheckToolError as exc:
                self._memo[key] = exc
        outcome = self._memo[key]
        if isinstance(outcome, CheckToolError):
            raise outcome
        return outcome


def _components(
    items: Sequence[tuple[CheckWork, tuple[tuple[str, ...], ...]]],
) -> list[list[CheckWork]]:
    """Group (item, planned calls) pairs connected by shared calls, in first-item order."""

    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owner: dict[tuple[str, ...], int] = {}
    for index, (_item, calls) in enumerate(items):
        for argv in calls:
            if argv in owner:
                first, second = find(owner[argv]), find(index)
                parent[max(first, second)] = min(first, second)
            else:
                owner[argv] = index
    groups: dict[int, list[CheckWork]] = {}
    for index, (item, _calls) in enumerate(items):
        groups.setdefault(find(index), []).append(item)
    return [groups[root] for root in sorted(groups)]


def refresh(
    queue: Any,
    *,
    runner: CheckRunner,
    now_epoch: int,
    task_ids: Iterable[str] | None = None,
    due_only: bool = True,
    max_calls: int | None = TICK_MAX_CALLS,
    budget_seconds: float | None = TICK_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Evaluate checks within a tool-call cap and time budget; ``None`` means unlimited.

    ``due_only=False`` evaluates every launch check of ``task_ids`` (a queue mutation);
    the default evaluates only due checks.  Identical tool calls within one refresh are
    made once, and every item that needs one reads that shared observation.  The cap
    counts tool calls, not checks: an item is admitted only if its new calls fit, and an
    item fully covered by calls already admitted still runs.  Admitted items are grouped
    into components connected by shared calls.  Each component reserves a generation for
    every item before its first call, then evaluates and stores all of them, so a refresh
    that begins later still wins.  The budget is checked only between components: a
    started component runs to completion, and every later one is deferred without
    reserving anything.  Nothing is reused across refresh calls.
    """

    work = queue.check_work(now_epoch=now_epoch, task_ids=task_ids, due_only=due_only)
    started = monotonic()
    planned: set[tuple[str, ...]] = set()
    selected: list[tuple[CheckWork, tuple[tuple[str, ...], ...]]] = []
    for item in work:
        calls = planned_calls(item.spec, item.cwd)
        new = set(calls) - planned
        if max_calls is not None and len(planned) + len(new) > max_calls:
            continue
        planned |= new
        selected.append((item, calls))
    evaluated: list[dict[str, Any]] = []
    discarded = tool_calls = 0
    for component in _components(selected):
        if budget_seconds is not None and monotonic() - started >= budget_seconds:
            break
        # Shared calls never cross components, so each memo is dropped once its group is done.
        memo = _MemoRunner(runner)
        generations = [queue.begin_check(item, now_epoch=now_epoch) for item in component]
        for item, generation in zip(component, generations):
            result = evaluate(item.spec, cwd=item.cwd, runner=memo)
            observed = None
            if item.spec.get("type") == "base_ref_exists":
                observed = {**item.context, "origin": origin_url(item.cwd, memo)}
            stored = queue.store_check_result(
                item, result.status, result.detail, generation=generation,
                observed_context=observed, now_epoch=now_epoch,
            )
            if stored:
                evaluated.append({
                    "task_id": item.task_id, "type": item.spec.get("type"), "status": result.status,
                    "check": describe(item.spec), "origin": item.origin, "detail": result.detail,
                })
            else:
                discarded += 1
        tool_calls += memo.calls
    return {
        "due": len(work),
        "evaluated": evaluated,
        "deferred": len(work) - len(evaluated) - discarded,
        "discarded": discarded,
        "tool_calls": tool_calls,
    }
