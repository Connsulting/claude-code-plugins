"""Structured preflight checks, merged dependency edges, collisions, backoff, and resume.

Every gh/git observation in this file goes through ``FakeRunner``, which replays the
real tool output recorded for the plan. The fake never parses output; it only maps an
exact argv tuple to a recorded ``RunnerResult`` (or raises the runner's tool error).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import unittest
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from unittest import mock

from tests.test_bonus_dependency_recovery import (
    KEY, NOW, RecoveryCase, iso, reason, rows, task, verified,
)
from bonus_drain import checks, db, goals
from tests.readiness_fixture import rereviewed, reviewed

HOUR = 3_600
REPO = "curie-eng/curie"

# --- Recorded tool output shapes ------------------------------------------------------
# Captured 2026-10-02 with gh/git on this host, see plan Recorded tool output shapes.
# The milestone description is shortened as the plan allows; every key is kept. gh and
# git terminate stdout with a newline when writing to a pipe.
ISSUE_2855_CLOSED = (
    '{"milestone":{"number":18,"title":"v0.10.0","description":"Dark factory: ...",'
    '"dueOn":null},"state":"CLOSED"}\n'
)
ISSUE_3815_OPEN = '{"milestone":null,"state":"OPEN"}\n'
ISSUE_MISSING_STDERR = (
    "GraphQL: Could not resolve to an issue or pull request with the number of 99999999. "
    "(repository.issue)\n"
)
PR_2994_MERGED = (
    '{"baseRefName":"epic/aws-secrets","headRefName":"task/aws-sec-provider",'
    '"mergedAt":"2026-09-23T10:42:50Z","number":2994,"state":"MERGED"}\n'
)
PR_3795_OPEN = (
    '{"baseRefName":"main","headRefName":"dependabot/uv/python-8362e5fa5b",'
    '"mergedAt":null,"number":3795,"state":"OPEN"}\n'
)
PR_MISSING_STDERR = 'no pull requests found for branch "task/does-not-exist-xyz"\n'
RELEASE_0_11_1 = (
    '{"isDraft":false,"isPrerelease":false,"publishedAt":"2026-10-01T04:23:15Z",'
    '"tagName":"v0.11.1"}\n'
)
RELEASE_MISSING_STDERR = "release not found\n"
README_RAW = "# Curie\n\n[![CI](...\n"
CONTENTS_404_STDOUT = (
    '{"message":"Not Found","documentation_url":'
    '"https://docs.github.com/rest/repos/contents#get-repository-content","status":"404"}'
)
CONTENTS_404_STDERR = "gh: Not Found (HTTP 404)\n"
LS_REMOTE_MAIN = "1619a0a4f220f2058591497d89b0304a962c9b4e\trefs/heads/main\n"
LS_REMOTE_128_STDERR = "fatal: cannot change to '/nonexistent': No such file or directory\n"
# Not part of the recorded set: gh's standard offline diagnostic, used only to prove a
# network failure is unknown rather than a definitive negative.
GH_OFFLINE_STDERR = (
    "error connecting to api.github.com\n"
    "check your internet connection or https://githubstatus.com\n"
)


def with_fields(recorded: str, **changes: object) -> str:
    """Re-key one recorded JSON shape (for example a different PR number or head)."""

    value = json.loads(recorded)
    value.update(changes)
    return json.dumps(value, separators=(",", ":")) + "\n"


def gh_issue(number: int, repo: str = REPO) -> tuple[str, ...]:
    return ("gh", "issue", "view", str(number), "-R", repo, "--json", "state,milestone")


def gh_pr(selector: object, repo: str = REPO) -> tuple[str, ...]:
    return (
        "gh", "pr", "view", str(selector), "-R", repo,
        "--json", "number,state,mergedAt,baseRefName,headRefName",
    )


def gh_release(tag: str, repo: str = REPO) -> tuple[str, ...]:
    return ("gh", "release", "view", tag, "-R", repo, "--json", "tagName,isDraft,publishedAt")


def gh_contents(path: str, ref: str = "main", repo: str = REPO) -> tuple[str, ...]:
    return (
        "gh", "api", "-H", "Accept: application/vnd.github.raw",
        f"repos/{repo}/contents/{path}?ref={ref}",
    )


def git_ls_remote(cwd: object, ref: str = "refs/heads/main") -> tuple[str, ...]:
    return ("git", "-C", str(cwd), "ls-remote", "--exit-code", "--heads", "origin", ref)


def ok(stdout: str) -> "checks.RunnerResult":
    return checks.RunnerResult(returncode=0, stdout=stdout, stderr="")


def exited(code: int, stderr: str = "", stdout: str = "") -> "checks.RunnerResult":
    return checks.RunnerResult(returncode=code, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Replay recorded tool results by exact argv; record every call.

    A response is a ``RunnerResult``, an exception instance to raise, a callable taking
    the argv tuple, or a list consumed in order (its last item repeats). ``git remote
    get-url origin`` is answered with a GitHub remote unless registered explicitly,
    because evaluation records the observed origin as context, not as a verdict.
    """

    def __init__(self, responses: Mapping[tuple[str, ...], Any] | None = None):
        self.responses: dict[tuple[str, ...], Any] = dict(responses or {})
        self.calls: list[tuple[str, ...]] = []
        self.cwds: list[str | None] = []
        self.unexpected: list[tuple[str, ...]] = []

    def __call__(self, argv: Iterable[str], cwd: str | None, timeout: float):
        key = tuple(argv)
        self.calls.append(key)
        self.cwds.append(cwd)
        if key not in self.responses:
            if len(key) == 6 and key[0] == "git" and key[3:] == ("remote", "get-url", "origin"):
                return ok(f"git@github.com:{REPO}.git\n")
            self.unexpected.append(key)
            return exited(127, "unexpected argv in test fake\n")
        response = self.responses[key]
        if isinstance(response, list):
            response = response.pop(0) if len(response) > 1 else response[0]
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(key)
        return response

    def tool_calls(self, marker: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if marker in call]


ISSUE_OPEN_2855 = {"type": "issue_open", "repo": REPO, "number": 2855}
ISSUE_OPEN_3815 = {"type": "issue_open", "repo": REPO, "number": 3815}
PR_2994_INTO_EPIC = {"type": "pr_merged", "repo": REPO, "pr": 2994, "base": "epic/aws-secrets"}
RELEASE_V0_11_1 = {"type": "release_exists", "repo": REPO, "tag": "v0.11.1"}


def handoff(state: str = "unmerged", remote: str = "git@github.com:owner/repo.git") -> dict[str, str]:
    return {
        "remote": remote,
        "target_ref": "refs/heads/main",
        "branch_ref": "refs/heads/task/x",
        "integration_state": state,
    }


# Issue 2855's recorded shape re-keyed to OPEN: open and in milestone v0.10.0, the answer
# both issue_open and issue_in_milestone read from one ``gh issue view`` call.
ISSUE_OPEN_IN_V0_10_0 = with_fields(ISSUE_2855_CLOSED, state="OPEN")
# The recorded ls-remote shape for a second branch on the same origin.
LS_REMOTE_NEXT = LS_REMOTE_MAIN.replace("refs/heads/main", "refs/heads/next")


def in_milestone(number: int, repo: str = "owner/repo", milestone: str = "v0.10.0") -> dict[str, object]:
    return {"type": "issue_in_milestone", "repo": repo, "number": number, "milestone": milestone}


def git_get_url(cwd: object) -> tuple[str, ...]:
    return ("git", "-C", str(cwd), "remote", "get-url", "origin")


TASK_X_PR_OPEN = with_fields(
    PR_3795_OPEN, baseRefName="main", headRefName="task/x", number=41,
)
TASK_X_PR_MERGED = with_fields(
    PR_2994_MERGED, baseRefName="main", headRefName="task/x", number=41,
)


def authority(
    detail: str = "Missing GitHub test actor",
    *,
    resume_when: list[dict[str, object]] | None = None,
    signature: str = "authority_required:test-actor",
) -> dict[str, object]:
    value: dict[str, object] = {
        "reason": {
            "code": "authority_required", "detail": detail, "signature": signature,
            "queue_time_knowable": False,
        },
    }
    if resume_when is not None:
        value["resume_when"] = resume_when
    return value


def table_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        names = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in names
        }


def downgrade_to_v2(path: Path) -> None:
    """Turn a freshly initialized database back into the v2 shape the live queue has."""

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        for column in ("checks_json", "merged_depends_on_json"):
            if column in columns:
                connection.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
        for table in ("account_backoff", "check_results", "blocker_notices"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("DELETE FROM schema_migrations WHERE version=3")


def legacy_contract_hash(item: db.Task) -> str:
    """The base release's contract identity: the task dict without the v3 fields."""

    value = item.to_dict()
    for field in ("priority", "size", "active"):
        value.pop(field)
    if value["start_ref"] is None:
        value.pop("start_ref")
    value.pop("checks", None)
    value.pop("merged_depends_on", None)
    value.pop("readiness_review", None)
    if not value.get("grants"):
        value.pop("grants", None)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def add_goal(queue: db.QueueDB, goal_id: str, members: Iterable[str], *, max_inflight: int = 2) -> None:
    contract = {"deadline": NOW + 10_000_000, "max_inflight": max_inflight}
    with sqlite3.connect(queue.path) as connection:
        connection.execute(
            "INSERT INTO goals(id,contract_json,revision,state,turn,wait_json,summary,created_at) "
            "VALUES(?,?,0,'queued',0,'[]','fixture goal',?)",
            (goal_id, json.dumps(contract), iso(NOW - 600)),
        )
        for member in members:
            connection.execute(
                "INSERT INTO goal_members(goal_id,task_id,role,managed) "
                "VALUES(?,?,'implementation',1)",
                (goal_id, member),
            )


class PreflightCase(RecoveryCase):
    def setUp(self) -> None:
        super().setUp()
        # Repository context is the realpath of the task cwd, so fixtures use realpaths.
        self.root = Path(os.path.realpath(self.root))

    def add(self, task_id: str, **changes: object):
        cwd = changes.pop("cwd", self.root)
        return self.queue.add_task(reviewed(task(task_id, cwd, **changes)))

    def runner(self, responses: Mapping[tuple[str, ...], Any] | None = None) -> FakeRunner:
        fake = FakeRunner(responses)
        self.addCleanup(lambda: self.assertEqual(fake.unexpected, [], "unexpected tool argv"))
        return fake

    def refresh(self, runner: FakeRunner, *, now: int = NOW, **kwargs: Any) -> dict[str, Any]:
        return checks.refresh(self.queue, runner=runner, now_epoch=now, **kwargs)

    def ready(self, task_id: str, now: int = NOW + 10) -> dict[str, Any]:
        return self.queue.readiness(task_id, now_epoch=now)

    def try_claim(self, task_id: str, now: int = NOW + 10):
        return self.queue.claim(
            task_id, f"manual/{task_id}/{now}", "alpha", "alpha-account", now_epoch=now,
        )

    def eligible_ids(self, now: int = NOW + 10) -> list[str]:
        return [item.id for item in self.queue.eligible_tasks(0, now_epoch=now)]

    def complete(self, task_id: str, repository: dict[str, str] | None = None, *, now: int = NOW):
        attempt = self.claim(task_id, now=now)
        self.terminal(task_id, attempt, "done", verified(repository), now=now)
        return attempt

    def block(self, task_id: str, outcome: dict[str, object] | None = None, *, now: int = NOW):
        attempt = self.claim(task_id, now=now)
        self.terminal(task_id, attempt, "failed", outcome or authority(), now=now)
        return attempt

    def mkdir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        return Path(os.path.realpath(path))


class CheckSpecTests(PreflightCase):
    def test_normalize_accepts_each_type(self) -> None:
        examples = [
            ({"type": "base_ref_exists", "ref": "main"},
             {"type": "base_ref_exists", "ref": "refs/heads/main"}),
            (ISSUE_OPEN_2855, ISSUE_OPEN_2855),
            ({"type": "issue_in_milestone", "repo": REPO, "number": 2855, "milestone": "v0.10.0"},
             {"type": "issue_in_milestone", "repo": REPO, "number": 2855, "milestone": "v0.10.0"}),
            (PR_2994_INTO_EPIC, PR_2994_INTO_EPIC),
            ({"type": "pr_merged", "repo": REPO, "head": "task/aws-sec-provider"},
             {"type": "pr_merged", "repo": REPO, "head": "task/aws-sec-provider"}),
            (RELEASE_V0_11_1, RELEASE_V0_11_1),
        ]
        for raw, expected in examples:
            with self.subTest(type=raw["type"]):
                self.assertEqual(checks.normalize_check(raw), expected)
        contents = checks.normalize_check({
            "type": "file_matches", "repo": REPO, "ref": "main",
            "path": "README.md", "pattern": "^# Curie",
        })
        self.assertEqual(contents["type"], "file_matches")
        self.assertEqual(contents["path"], "README.md")
        self.assertIn(contents.get("present", True), (True,))
        self.assertEqual(checks.normalize_checks(None), ())
        self.assertEqual(checks.normalize_checks([]), ())
        twice = checks.normalize_checks([ISSUE_OPEN_2855, dict(ISSUE_OPEN_2855), RELEASE_V0_11_1])
        self.assertEqual([json.loads(item) for item in twice], [ISSUE_OPEN_2855, RELEASE_V0_11_1])
        self.assertEqual(set(checks.CHECK_FIELDS), {
            "base_ref_exists", "issue_open", "issue_in_milestone",
            "pr_merged", "release_exists", "file_matches",
            "mcp_authenticated", "k8s_resource_exists", "openrouter_credit",
        })

    def test_normalize_rejects_invalid_specs(self) -> None:
        invalid = {
            "unknown type": {"type": "deploy_green", "repo": REPO},
            "extra key": {**ISSUE_OPEN_2855, "note": "extra"},
            "bad repo": {"type": "issue_open", "repo": "not a repo", "number": 1},
            "bool number": {"type": "issue_open", "repo": REPO, "number": True},
            "zero number": {"type": "issue_open", "repo": REPO, "number": 0},
            "parent path": {
                "type": "file_matches", "repo": REPO, "ref": "main",
                "path": "../secrets", "pattern": "x",
            },
            "invalid regex": {
                "type": "file_matches", "repo": REPO, "ref": "main",
                "path": "README.md", "pattern": "(",
            },
            "remote-tracking ref": {"type": "base_ref_exists", "ref": "origin/main"},
            "remote-tracking full ref": {"type": "base_ref_exists", "ref": "refs/heads/origin/main"},
            "pr and head": {"type": "pr_merged", "repo": REPO, "pr": 1, "head": "task/x"},
            "neither pr nor head": {"type": "pr_merged", "repo": REPO},
            "missing field": {"type": "issue_in_milestone", "repo": REPO, "number": 1},
            "not an object": ["issue_open"],
        }
        for label, raw in invalid.items():
            with self.subTest(label), self.assertRaises(checks.CheckError):
                checks.normalize_check(raw)
        too_many = [
            {"type": "issue_open", "repo": REPO, "number": number}
            for number in range(1, checks.MAX_CHECKS_PER_TASK + 2)
        ]
        self.assertEqual(checks.MAX_CHECKS_PER_TASK, 16)
        with self.assertRaises(checks.CheckError):
            checks.normalize_checks(too_many)
        self.assertEqual(len(checks.normalize_checks(too_many[:16])), 16)

    def test_parse_issue_ref_forms(self) -> None:
        self.assertEqual(
            checks.parse_issue_ref("https://github.com/curie-eng/curie/issues/2855"),
            ("curie-eng/curie", 2855),
        )
        self.assertEqual(checks.parse_issue_ref("owner/repo#12"), ("owner/repo", 12))
        self.assertEqual(checks.parse_issue_ref("owner/repo#12, notes"), ("owner/repo", 12))
        self.assertEqual(checks.parse_issue_ref("  owner/repo#12; context"), ("owner/repo", 12))
        for text in (
            # Live free text that merely mentions an issue.
            "epic aws-secrets planning thread 2026-09-22 (issue curie-eng/curie#2956, see notes)",
            "https://github.com/curie-eng/curie/pull/2994",
            "Claude thread 2026-10-01 planning the report export",
            "owner/repo#12abc",
            "",
        ):
            with self.subTest(text=text):
                self.assertIsNone(checks.parse_issue_ref(text))


class CheckEvaluateTests(PreflightCase):
    def evaluate(self, raw: dict[str, object], responses: Mapping[tuple[str, ...], Any], *, cwd: str | None = None):
        fake = self.runner(responses)
        spec = checks.normalize_check(raw)
        result = checks.evaluate(spec, cwd=cwd or str(self.root), runner=fake)
        return result, fake

    def assertVerdict(self, result, status: str, detail: str | None = None) -> None:
        self.assertEqual(result.status, status, result)
        if detail is not None:
            self.assertIn(detail, result.detail)

    def test_issue_shapes(self) -> None:
        responses = {
            gh_issue(2855): ok(ISSUE_2855_CLOSED),
            gh_issue(3815): ok(ISSUE_3815_OPEN),
            gh_issue(99999999): exited(1, ISSUE_MISSING_STDERR),
        }
        result, fake = self.evaluate(ISSUE_OPEN_2855, responses)
        self.assertVerdict(result, "fail", "issue is CLOSED")
        self.assertEqual(fake.calls, [gh_issue(2855)])
        self.assertVerdict(self.evaluate(ISSUE_OPEN_3815, responses)[0], "pass")
        self.assertVerdict(self.evaluate(
            {"type": "issue_in_milestone", "repo": REPO, "number": 3815, "milestone": "v0.10.0"},
            responses,
        )[0], "fail", "issue milestone is none")
        self.assertVerdict(self.evaluate(
            {"type": "issue_in_milestone", "repo": REPO, "number": 2855, "milestone": "v0.10.0"},
            responses,
        )[0], "pass")
        self.assertVerdict(self.evaluate(
            {"type": "issue_in_milestone", "repo": REPO, "number": 2855, "milestone": "v0.11.0"},
            responses,
        )[0], "fail", "v0.10.0")
        self.assertVerdict(self.evaluate(
            {"type": "issue_open", "repo": REPO, "number": 99999999}, responses,
        )[0], "fail", "issue not found")

    def test_pr_shapes(self) -> None:
        responses = {
            gh_pr(2994): ok(PR_2994_MERGED),
            gh_pr(3795): ok(PR_3795_OPEN),
            gh_pr("task/does-not-exist-xyz"): exited(1, PR_MISSING_STDERR),
        }
        self.assertVerdict(self.evaluate(PR_2994_INTO_EPIC, responses)[0], "pass")
        self.assertVerdict(
            self.evaluate({"type": "pr_merged", "repo": REPO, "pr": 2994}, responses)[0], "pass",
        )
        self.assertVerdict(self.evaluate(
            {"type": "pr_merged", "repo": REPO, "pr": 2994, "base": "main"}, responses,
        )[0], "fail", "PR #2994 merged into epic/aws-secrets, not main")
        self.assertVerdict(self.evaluate(
            {"type": "pr_merged", "repo": REPO, "pr": 3795}, responses,
        )[0], "fail", "PR #3795 is OPEN")
        self.assertVerdict(self.evaluate(
            {"type": "pr_merged", "repo": REPO, "head": "task/does-not-exist-xyz"}, responses,
        )[0], "fail", "no pull request found")

    def test_release_shapes(self) -> None:
        responses = {
            gh_release("v0.11.1"): ok(RELEASE_0_11_1),
            gh_release("v99.99.99"): exited(1, RELEASE_MISSING_STDERR),
            gh_release("v0.12.0-rc1"): ok(with_fields(
                RELEASE_0_11_1, tagName="v0.12.0-rc1", isDraft=True, publishedAt=None,
            )),
        }
        self.assertVerdict(self.evaluate(RELEASE_V0_11_1, responses)[0], "pass")
        self.assertVerdict(self.evaluate(
            {"type": "release_exists", "repo": REPO, "tag": "v99.99.99"}, responses,
        )[0], "fail")
        self.assertVerdict(self.evaluate(
            {"type": "release_exists", "repo": REPO, "tag": "v0.12.0-rc1"}, responses,
        )[0], "fail", "draft")

    def test_file_contents_shapes(self) -> None:
        responses = {
            gh_contents("README.md"): ok(README_RAW),
            gh_contents("docs/missing.md"): exited(1, CONTENTS_404_STDERR, CONTENTS_404_STDOUT),
        }
        readme = {"type": "file_matches", "repo": REPO, "ref": "main", "path": "README.md"}
        missing = {"type": "file_matches", "repo": REPO, "ref": "main", "path": "docs/missing.md"}
        self.assertVerdict(self.evaluate({**readme, "pattern": "^# Curie"}, responses)[0], "pass")
        self.assertVerdict(self.evaluate({**readme, "pattern": "^# Other"}, responses)[0], "fail")
        self.assertVerdict(self.evaluate(
            {**readme, "pattern": "^# Curie", "present": False}, responses,
        )[0], "fail")
        self.assertVerdict(self.evaluate(
            {**missing, "pattern": "anything", "present": False}, responses,
        )[0], "pass")
        self.assertVerdict(self.evaluate({**missing, "pattern": "anything"}, responses)[0], "fail")

    def test_ls_remote_shapes(self) -> None:
        here = str(self.root)
        responses = {
            git_ls_remote(here): ok(LS_REMOTE_MAIN),
            # The evidence-3 case: a remote-tracking name written as a branch.
            git_ls_remote(here, "refs/heads/epic/missing"): exited(2),
            git_ls_remote("/nonexistent"): exited(128, LS_REMOTE_128_STDERR),
        }
        result, fake = self.evaluate({"type": "base_ref_exists", "ref": "main"}, responses, cwd=here)
        self.assertVerdict(result, "pass")
        self.assertEqual(fake.tool_calls("ls-remote"), [git_ls_remote(here)])
        self.assertVerdict(self.evaluate(
            {"type": "base_ref_exists", "ref": "epic/missing"}, responses, cwd=here,
        )[0], "fail", "missing on origin")
        self.assertVerdict(self.evaluate(
            {"type": "base_ref_exists", "ref": "main"}, responses, cwd="/nonexistent",
        )[0], "unknown")

    def test_tool_error_and_malformed_json_are_unknown(self) -> None:
        responses = {
            gh_issue(2855): checks.CheckToolError("gh timed out after 20s"),
            gh_issue(3815): ok("<html>rate limited</html>\n"),
            gh_issue(4000): exited(1, GH_OFFLINE_STDERR),
            gh_pr(2994): ok('{"state":'),
        }
        for raw in (
            ISSUE_OPEN_2855,
            ISSUE_OPEN_3815,
            {"type": "issue_open", "repo": REPO, "number": 4000},
            {"type": "pr_merged", "repo": REPO, "pr": 2994},
        ):
            with self.subTest(raw=raw):
                self.assertVerdict(self.evaluate(raw, responses)[0], "unknown")


class CheckGatingTests(PreflightCase):
    def test_failing_check_waits_without_attempt(self) -> None:
        self.add("gated", checks=[ISSUE_OPEN_2855])
        fake = self.runner({gh_issue(2855): ok(ISSUE_2855_CLOSED)})

        result = self.refresh(fake)

        self.assertEqual(
            [(item["task_id"], item["type"], item["status"]) for item in result["evaluated"]],
            [("gated", "issue_open", "fail")],
        )
        status = self.ready("gated")
        self.assertFalse(status["ready"])
        self.assertEqual((status["state"], status["hold_reason"]), ("waiting", "check_failed"))
        self.assertIn("issue is CLOSED", status["reason"])
        self.assertIsNone(self.try_claim("gated"))
        self.assertEqual(self.queue.attempts(task_id="gated"), [])
        self.assertNotIn("gated", self.eligible_ids())

    def test_pending_and_recent_unknown_wait(self) -> None:
        self.add("pending", checks=[ISSUE_OPEN_2855])
        status = self.ready("pending")
        self.assertEqual((status["state"], status["hold_reason"]), ("waiting", "check_unchecked"))
        self.assertTrue(status["reason"].startswith("Not checked yet: "), status["reason"])
        self.assertEqual(
            [(item["status"], item["checked_at"]) for item in status["checks"]],
            [("unchecked", None)],
        )
        self.assertIsNone(self.try_claim("pending"))

        self.add("flaky", checks=[ISSUE_OPEN_3815])
        self.add("offline", checks=[{"type": "issue_open", "repo": REPO, "number": 4000}])
        fake = self.runner({
            gh_issue(2855): ok(ISSUE_3815_OPEN),
            gh_issue(3815): checks.CheckToolError("gh timed out after 20s"),
            gh_issue(4000): exited(1, GH_OFFLINE_STDERR),
        })
        self.refresh(fake)
        for task_id in ("flaky", "offline"):
            with self.subTest(task_id=task_id):
                status = self.ready(task_id)
                self.assertEqual(
                    (status["state"], status["hold_reason"]), ("waiting", "check_unknown"),
                )
                self.assertIn("Check could not be verified yet", status["reason"])
                self.assertEqual([item["status"] for item in status["checks"]], ["unknown"])
                self.assertIsNone(self.try_claim(task_id))
                self.assertEqual(self.queue.attempts(task_id=task_id), [])
        self.assertTrue(self.ready("pending")["ready"])

    def test_unknown_past_grace_is_unverified_and_claimable(self) -> None:
        self.add("degraded", checks=[ISSUE_OPEN_3815])
        fake = self.runner({gh_issue(3815): exited(1, GH_OFFLINE_STDERR)})
        self.refresh(fake, now=NOW)
        self.refresh(fake, now=NOW + 3_000)
        self.assertEqual(len(fake.tool_calls("issue")), 2)

        later = NOW + checks.UNKNOWN_GRACE_SECONDS + 100
        status = self.ready("degraded", now=later)
        self.assertTrue(status["ready"], status)
        self.assertEqual([item["status"] for item in status["checks"]], ["unverified"])
        self.assertIn("degraded", self.eligible_ids(now=later))
        self.assertIsNotNone(self.try_claim("degraded", now=later))

    def test_task_without_checks_launches_without_runner(self) -> None:
        self.add("plain", source_ref="Claude thread 2026-10-01 planning the report export")
        fake = self.runner()

        self.assertEqual(self.queue.check_work(now_epoch=NOW), [])
        result = self.refresh(fake)

        self.assertEqual(result["evaluated"], [])
        self.assertEqual(fake.calls, [])
        status = self.ready("plain")
        self.assertTrue(status["ready"], status)
        self.assertEqual(status["checks"], [])
        self.assertIsNone(status["root_blocker"])
        self.assertIsNotNone(self.try_claim("plain"))

    def test_builtin_checks_derivation(self) -> None:
        self.add("url", source_ref="https://github.com/curie-eng/curie/issues/2855")
        self.add("short", source_ref="curie-eng/curie#3815, follow-up notes")
        self.add(
            "mention",
            source_ref="epic aws-secrets planning thread 2026-09-22 (issue curie-eng/curie#2956, see notes)",
        )
        self.add("pull", source_ref="https://github.com/curie-eng/curie/pull/2994")
        self.add("based", start_ref="main")

        def builtins(task_id: str) -> list[tuple[str, dict[str, object]]]:
            return [(item["origin"], item["spec"]) for item in self.ready(task_id)["checks"]]

        self.assertEqual(builtins("url"), [("builtin", ISSUE_OPEN_2855)])
        self.assertEqual(builtins("short"), [("builtin", ISSUE_OPEN_3815)])
        self.assertEqual(builtins("mention"), [])
        self.assertEqual(builtins("pull"), [])
        self.assertEqual(
            builtins("based"),
            [("builtin", {"type": "base_ref_exists", "ref": "refs/heads/main"})],
        )
        self.assertTrue(self.ready("mention")["ready"])
        self.assertTrue(self.ready("pull")["ready"])
        work = {item.task_id: item for item in self.queue.check_work(now_epoch=NOW)}
        self.assertEqual(set(work), {"url", "short", "based"})
        self.assertEqual(work["url"].origin, "builtin")
        self.assertEqual(work["based"].context, {"cwd": str(self.root)})
        self.assertEqual(work["url"].context, {})

    def test_goal_owned_task_gets_no_builtins(self) -> None:
        self.add(
            "member", work_group="Goal work",
            source_ref="https://github.com/curie-eng/curie/issues/2855",
            start_ref="main", checks=[RELEASE_V0_11_1],
        )
        add_goal(self.queue, "goal-1", ["member"])

        status = self.ready("member")
        self.assertEqual(
            [(item["origin"], item["type"]) for item in status["checks"]],
            [("declared", "release_exists")],
        )
        fake = self.runner({gh_release("v0.11.1"): ok(RELEASE_0_11_1)})
        self.refresh(fake)
        self.assertEqual(fake.tool_calls("gh"), [gh_release("v0.11.1")])
        self.assertEqual(fake.tool_calls("ls-remote"), [])
        self.assertTrue(self.ready("member")["ready"])

    def test_readiness_lists_check_origins(self) -> None:
        self.add(
            "both", checks=[RELEASE_V0_11_1],
            source_ref="https://github.com/curie-eng/curie/issues/2855",
        )
        listed = self.ready("both")["checks"]
        self.assertEqual(
            sorted((item["origin"], item["type"]) for item in listed),
            [("builtin", "issue_open"), ("declared", "release_exists")],
        )
        for item in listed:
            self.assertEqual(item["status"], "unchecked")
            self.assertIsNone(item["detail"])
            self.assertIsNone(item["checked_at"])
            self.assertEqual(
                set(item), {"origin", "type", "spec", "status", "detail", "checked_at"},
            )

    def _two_repositories(self) -> tuple[Path, Path, FakeRunner]:
        repo_a, repo_b = self.mkdir("repo-a"), self.mkdir("repo-b")
        self.add("t1", cwd=str(repo_a), start_ref="main")
        self.add("t2", cwd=str(repo_b), start_ref="main")
        fake = self.runner({
            git_ls_remote(repo_a): ok(LS_REMOTE_MAIN),
            git_ls_remote(repo_b): exited(2),
        })
        self.refresh(fake)
        return repo_a, repo_b, fake

    def test_identical_specs_in_two_repositories_do_not_share(self) -> None:
        repo_a, repo_b, fake = self._two_repositories()

        self.assertCountEqual(
            fake.tool_calls("ls-remote"), [git_ls_remote(repo_a), git_ls_remote(repo_b)],
        )
        self.assertTrue(self.ready("t1")["ready"])
        failed = self.ready("t2")
        self.assertEqual(failed["hold_reason"], "check_failed")
        self.assertIn("refs/heads/main is missing on origin", failed["reason"])
        stored = rows(self.queue, "SELECT task_id,check_id FROM check_results ORDER BY task_id")
        self.assertEqual([row["task_id"] for row in stored], ["t1", "t2"])
        self.assertNotEqual(stored[0]["check_id"], stored[1]["check_id"])
        spec = {"type": "base_ref_exists", "ref": "refs/heads/main"}
        self.assertNotEqual(
            checks.check_id(spec, checks.check_context(spec, cwd=str(repo_a))),
            checks.check_id(spec, checks.check_context(spec, cwd=str(repo_b))),
        )

    def test_cwd_edit_makes_builtin_unchecked(self) -> None:
        _repo_a, repo_b, _fake = self._two_repositories()
        self.assertTrue(self.ready("t1")["ready"])

        self.queue.edit_task("t1", rereviewed(self.queue.task("t1"), {"cwd": str(repo_b)}))

        status = self.ready("t1")
        self.assertEqual(status["hold_reason"], "check_unchecked")
        self.assertEqual([item["status"] for item in status["checks"]], ["unchecked"])
        again = self.runner({git_ls_remote(repo_b): exited(2)})
        self.refresh(again, now=NOW + 20)
        self.assertEqual(again.tool_calls("ls-remote"), [git_ls_remote(repo_b)])
        self.assertEqual(self.ready("t1", now=NOW + 30)["hold_reason"], "check_failed")

    def test_same_issue_in_two_tasks_shares_one_call_per_refresh(self) -> None:
        self.add("first", checks=[ISSUE_OPEN_3815])
        self.add("second", checks=[ISSUE_OPEN_3815])
        fake = self.runner({gh_issue(3815): ok(ISSUE_3815_OPEN)})

        result = self.refresh(fake)

        self.assertEqual(fake.tool_calls("issue"), [gh_issue(3815)])
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(
            sorted((item["task_id"], item["status"]) for item in result["evaluated"]),
            [("first", "pass"), ("second", "pass")],
        )
        stored = rows(self.queue, "SELECT task_id,status FROM check_results ORDER BY task_id")
        self.assertEqual(
            [(row["task_id"], row["status"]) for row in stored],
            [("first", "pass"), ("second", "pass")],
        )
        self.assertTrue(self.ready("first")["ready"])
        self.assertTrue(self.ready("second")["ready"])

    def test_obsolete_completion_is_discarded(self) -> None:
        for order, (nested_shape, outer_shape, expected) in enumerate((
            (ISSUE_2855_CLOSED, ISSUE_3815_OPEN, "fail"),
            (ISSUE_3815_OPEN, ISSUE_2855_CLOSED, "pass"),
        )):
            with self.subTest(nested=json.loads(nested_shape)["state"]):
                task_id = f"cas-{order}"
                spec = {"type": "issue_open", "repo": REPO, "number": 2855 + order}
                argv = gh_issue(2855 + order)
                self.add(task_id, checks=[spec])
                seeded = self.runner({argv: ok(ISSUE_3815_OPEN)})
                self.refresh(seeded, now=NOW - checks.PASS_REFRESH_SECONDS - 100, task_ids=(task_id,))
                self.assertTrue(self.ready(task_id, now=NOW - 10)["ready"])

                manual = self.runner({argv: ok(nested_shape)})

                def scout_answer(_argv: tuple[str, ...], task_id: str = task_id, manual=manual):
                    # A manual dispatch evaluates the same row while the scout's call is in
                    # flight; it began later, so its result must win.
                    checks.refresh(self.queue, runner=manual, now_epoch=NOW, task_ids=(task_id,))
                    return ok(outer_shape)

                scout = self.runner({argv: scout_answer})
                outer = self.refresh(scout, now=NOW, task_ids=(task_id,))

                self.assertEqual(outer["discarded"], 1)
                self.assertEqual(len(manual.calls), 1)
                stored = rows(
                    self.queue, "SELECT status,detail FROM check_results WHERE task_id=?", (task_id,),
                )
                self.assertEqual([row["status"] for row in stored], [expected])
                status = self.ready(task_id, now=NOW + 10)
                if expected == "fail":
                    self.assertIn("CLOSED", stored[0]["detail"])
                    self.assertEqual(status["hold_reason"], "check_failed")
                    self.assertIsNone(self.try_claim(task_id, now=NOW + 10))
                else:
                    self.assertTrue(status["ready"], status)

    def test_two_task_interleaving_never_reuses_observation(self) -> None:
        spec = {"type": "issue_open", "repo": "owner/repo", "number": 12}
        argv = gh_issue(12, "owner/repo")
        self.add("a", checks=[spec], created_at=iso(NOW - 120))
        self.add("b", checks=[spec], created_at=iso(NOW - 60))
        manual = self.runner({argv: ok(ISSUE_2855_CLOSED)})
        manual_store: list[str] = []

        def scout_answer(_argv: tuple[str, ...]):
            # The scout reserved both generations before this single shared call; a manual
            # refresh of b that begins while the call is in flight must still win for b.
            if not manual_store:
                checks.refresh(self.queue, runner=manual, now_epoch=NOW, task_ids=("b",))
                manual_store.append(rows(
                    self.queue, "SELECT checked_at FROM check_results WHERE task_id='b'",
                )[0]["checked_at"])
            return ok(ISSUE_3815_OPEN)

        scout = self.runner({argv: scout_answer})
        outer = self.refresh(scout, now=NOW)

        self.assertEqual(len(scout.calls), 1)
        self.assertEqual(len(manual.calls), 1)
        self.assertEqual(outer["discarded"], 1)
        self.assertEqual(
            [(item["task_id"], item["status"]) for item in outer["evaluated"]], [("a", "pass")],
        )
        stored = {
            row["task_id"]: row for row in rows(
                self.queue, "SELECT task_id,status,checked_at FROM check_results",
            )
        }
        self.assertEqual(stored["a"]["status"], "pass")
        self.assertEqual(stored["b"]["status"], "fail")
        self.assertGreaterEqual(
            db._timestamp_epoch(stored["b"]["checked_at"]),
            db._timestamp_epoch(manual_store[0]),
        )
        self.assertEqual(self.ready("b")["hold_reason"], "check_failed")
        self.assertTrue(self.ready("a")["ready"])

    def test_expired_passes_beyond_budget_wait(self) -> None:
        ids = [f"fresh-{index:02d}" for index in range(30)]
        responses = {}
        for index, task_id in enumerate(ids):
            number = 1001 + index
            self.add(
                task_id, created_at=iso(NOW - 10_000 + index),
                checks=[{"type": "issue_open", "repo": "owner/repo", "number": number}],
            )
            responses[gh_issue(number, "owner/repo")] = ok(ISSUE_3815_OPEN)
        self.refresh(self.runner(responses), now=NOW - 4_000, max_calls=100)

        result = self.refresh(self.runner(responses), now=NOW, max_calls=25)

        self.assertEqual(len(result["evaluated"]), 25)
        self.assertEqual(result["deferred"], 5)
        statuses = {task_id: self.ready(task_id, now=NOW + 10) for task_id in ids}
        waiting = sorted(task_id for task_id, status in statuses.items() if not status["ready"])
        self.assertEqual(len(waiting), 5)
        for task_id in waiting:
            self.assertEqual(statuses[task_id]["hold_reason"], "check_pending")
            self.assertEqual([item["status"] for item in statuses[task_id]["checks"]], ["pending"])
        eligible = self.eligible_ids(now=NOW + 10)
        self.assertEqual(sorted(set(ids) - set(eligible)), waiting)
        self.assertIsNone(self.try_claim(waiting[0], now=NOW + 10))

    def test_recent_pass_stays_ready_and_is_refreshed(self) -> None:
        self.add("recent", checks=[ISSUE_OPEN_3815])
        fake = self.runner({gh_issue(3815): ok(ISSUE_3815_OPEN)})
        self.refresh(fake, now=NOW - 3_000)

        status = self.ready("recent", now=NOW)
        self.assertTrue(status["ready"], status)
        self.assertEqual([item["status"] for item in status["checks"]], ["pass"])
        due = self.queue.check_work(now_epoch=NOW)
        self.assertEqual([(item.task_id, item.spec) for item in due], [("recent", ISSUE_OPEN_3815)])
        self.refresh(fake, now=NOW)
        self.assertEqual(len(fake.tool_calls("issue")), 2)
        self.assertEqual(self.queue.check_work(now_epoch=NOW + 10), [])

    def test_contract_hash_unchanged_without_new_fields(self) -> None:
        plain = self.add("plain")
        based = self.add("based", start_ref="main")
        for item in (plain, based):
            with self.subTest(task=item.id):
                self.assertEqual(item.checks, ())
                self.assertEqual(item.merged_depends_on, ())
                self.assertEqual(db._contract_hash(item), legacy_contract_hash(item))
                self.assertEqual(goals._contract_hash(item), legacy_contract_hash(item))
        checked = self.add("checked", checks=[ISSUE_OPEN_3815])
        self.assertNotEqual(db._contract_hash(checked), legacy_contract_hash(checked))

    def test_retained_recovery_stays_eligible_after_migration(self) -> None:
        self.add("retained")
        source = self.fail_task("retained")
        decision = self.queue.requeue("retained", attempt_id=source.id, now_epoch=NOW + 1)
        self.assertEqual(decision.state, "scheduled")
        legacy = legacy_contract_hash(self.queue.task("retained"))
        # The live row was written by the base release, whose hash had no v3 keys.
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute("UPDATE task_recovery SET contract_hash=? WHERE task_id='retained'", (legacy,))
            connection.execute("UPDATE task_attempts SET contract_hash=? WHERE task_id='retained'", (legacy,))
        downgrade_to_v2(self.queue.path)

        migrated = db.QueueDB(self.queue.path)
        migrated.initialize()

        status = migrated.readiness("retained", now_epoch=NOW + 10)
        self.assertTrue(status["ready"], status)
        attempt = migrated.claim(
            "retained", "manual/after-migration", "alpha", "alpha-account", now_epoch=NOW + 10,
        )
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.recovery_of, source.id)

    def test_numbered_pr_merge_pass_is_permanent(self) -> None:
        self.add("after-merge", checks=[PR_2994_INTO_EPIC])
        self.refresh(self.runner({gh_pr(2994): ok(PR_2994_MERGED)}), now=NOW - 3 * HOUR)

        status = self.ready("after-merge", now=NOW)
        self.assertTrue(status["ready"], status)
        self.assertEqual([item["status"] for item in status["checks"]], ["pass"])
        self.assertEqual(self.queue.check_work(now_epoch=NOW), [])
        idle = self.runner()
        self.refresh(idle, now=NOW)
        self.assertEqual(idle.calls, [])

    def test_head_selected_merge_pass_refreshes_after_branch_reuse(self) -> None:
        spec = {"type": "pr_merged", "repo": REPO, "head": "task/aws-sec-provider"}
        self.add("monthly", kind="recurring", cadence="monthly", checks=[spec])
        reused = with_fields(PR_3795_OPEN, headRefName="task/aws-sec-provider")
        fake = self.runner({gh_pr("task/aws-sec-provider"): [ok(PR_2994_MERGED), ok(reused)]})
        self.refresh(fake, now=NOW)
        self.assertTrue(self.ready("monthly", now=NOW + 10)["ready"])

        # Without a refresh, a head-selected pass lapses like any other result.
        lapsed = self.ready("monthly", now=NOW + checks.RESULT_TTL_SECONDS + 1)
        self.assertEqual(lapsed["hold_reason"], "check_pending")

        later = NOW + checks.PASS_REFRESH_SECONDS + 1
        self.assertEqual(
            [item.spec for item in self.queue.check_work(now_epoch=later)], [spec],
        )
        self.refresh(fake, now=later)
        status = self.ready("monthly", now=later + 10)
        self.assertEqual(status["hold_reason"], "check_failed")
        self.assertIn("PR #3795 is OPEN", status["reason"])


class MergedEdgeTests(PreflightCase):
    def setUp(self) -> None:
        super().setUp()
        self.add("parent")

    def merged_child(self, task_id: str = "child", parent: str = "parent") -> None:
        self.add(task_id, depends_on=[parent], merged_depends_on=[parent])

    def edge(self, status: dict[str, Any], parent: str = "parent") -> dict[str, Any]:
        return next(item for item in status["dependencies"] if item["id"] == parent)

    def test_unmerged_parent_blocks_child_without_attempt(self) -> None:
        self.merged_child()
        self.complete("parent", handoff())

        pending = self.ready("child")
        item = self.edge(pending)
        self.assertEqual((item["satisfy"], item["satisfied"]), ("merged", False))
        self.assertEqual(item["status"], "merge_check_pending")
        self.assertTrue(item["verified_completion"])
        self.assertEqual(pending["state"], "waiting")
        self.assertIsNone(self.try_claim("child"))

        fake = self.runner({gh_pr("task/x", "owner/repo"): ok(TASK_X_PR_OPEN)})
        self.refresh(fake, now=NOW + 20)

        status = self.ready("child", now=NOW + 30)
        item = self.edge(status)
        self.assertEqual(item["status"], "unmerged")
        self.assertIn("task/x not merged into main", item["detail"])
        self.assertIn("task/x not merged into main", status["reason"])
        self.assertIsNone(self.try_claim("child", now=NOW + 30))
        self.assertEqual(self.queue.attempts(task_id="child"), [])

    def test_merge_pass_unblocks_and_selects_target_base(self) -> None:
        self.merged_child()
        self.complete("parent", handoff())
        fake = self.runner({gh_pr("task/x", "owner/repo"): ok(TASK_X_PR_MERGED)})
        self.refresh(fake, now=NOW + 20)

        status = self.ready("child", now=NOW + 30)
        self.assertTrue(status["ready"], status)
        self.assertTrue(self.edge(status)["satisfied"])
        self.assertEqual(status["dependency_base"]["branch_ref"], "refs/heads/main")
        self.assertEqual(self.queue.dependency_base("child")["branch_ref"], "refs/heads/main")
        self.assertIsNotNone(self.try_claim("child", now=NOW + 30))

    def test_recorded_merge_needs_no_check(self) -> None:
        self.merged_child()
        self.complete("parent", handoff(state="merged"))
        idle = self.runner()

        self.refresh(idle, now=NOW + 20)

        self.assertEqual(idle.calls, [])
        status = self.ready("child", now=NOW + 30)
        self.assertTrue(status["ready"], status)
        self.assertTrue(self.edge(status)["satisfied"])

    def test_no_handoff_and_non_github_remote_are_satisfied(self) -> None:
        self.add("local-parent")
        self.merged_child("plain-child")
        self.merged_child("local-child", "local-parent")
        self.complete("parent")
        self.complete("local-parent", handoff(remote=str(self.root / "origin.git")))
        idle = self.runner()
        self.refresh(idle, now=NOW + 20)
        self.assertEqual(idle.calls, [])

        plain = self.ready("plain-child", now=NOW + 30)
        self.assertTrue(plain["ready"], plain)
        local = self.ready("local-child", now=NOW + 30)
        self.assertTrue(local["ready"], local)
        self.assertIn("merge unverifiable", self.edge(local, "local-parent")["detail"])

    def test_done_edge_unchanged(self) -> None:
        self.add("child", depends_on=["parent"])
        self.complete("parent", handoff())

        status = self.ready("child")
        item = self.edge(status)
        self.assertTrue(status["ready"], status)
        self.assertEqual((item["satisfy"], item["satisfied"], item["status"]), ("done", True, "done"))
        self.assertIsNone(item["detail"])
        self.assertEqual(status["dependency_base"]["branch_ref"], "refs/heads/task/x")
        stored = rows(
            self.queue, "SELECT depends_on_json,merged_depends_on_json FROM tasks WHERE id='child'",
        )
        self.assertEqual(stored, [{"depends_on_json": '["parent"]', "merged_depends_on_json": None}])
        self.assertEqual(self.queue.task("child").merged_depends_on, ())

    def test_unverified_merge_starts_from_parent_branch(self) -> None:
        self.merged_child()
        self.complete("parent", handoff())
        fake = self.runner({gh_pr("task/x", "owner/repo"): exited(1, GH_OFFLINE_STDERR)})
        self.refresh(fake, now=NOW + 20)
        self.refresh(fake, now=NOW + 3_020)
        self.assertEqual(len(fake.tool_calls("pr")), 2)

        later = NOW + 20 + checks.UNKNOWN_GRACE_SECONDS + 100
        status = self.ready("child", now=later)
        self.assertTrue(status["ready"], status)
        self.assertTrue(self.edge(status)["satisfied"])
        self.assertIn("merge unverified", self.edge(status)["detail"])
        self.assertEqual(status["dependency_base"]["branch_ref"], "refs/heads/task/x")

    def test_check_work_emits_merge_spec_after_parent_done(self) -> None:
        self.merged_child()
        self.assertEqual(self.queue.check_work(now_epoch=NOW, task_ids=["child"]), [])

        self.complete("parent", handoff())

        work = self.queue.check_work(now_epoch=NOW + 10, task_ids=["child"])
        self.assertEqual(
            [(item.origin, item.spec) for item in work],
            [("dependency", {"type": "pr_merged", "repo": "owner/repo", "head": "task/x", "base": "main"})],
        )
        self.assertEqual(work[0].context, {})


class QueueValidationTests(PreflightCase):
    def test_remote_tracking_start_ref_rejected(self) -> None:
        for value in ("origin/main", "refs/heads/origin/main", "upstream/main", "remotes/origin/main"):
            with self.subTest(start_ref=value):
                with self.assertRaisesRegex(db.QueueError, "remote-tracking"):
                    self.add(f"bad-{len(value)}", start_ref=value)
        self.assertEqual(self.queue.tasks(), [])
        self.assertEqual(self.add("good", start_ref="main").start_ref, "refs/heads/main")
        self.assertEqual(self.add("epic", start_ref="epic/next").start_ref, "refs/heads/epic/next")

    def test_unrelated_edit_of_legacy_origin_start_ref_allowed(self) -> None:
        self.add("legacy", start_ref="main")
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute("UPDATE tasks SET start_ref='refs/heads/origin/main' WHERE id='legacy'")

        edited = self.queue.edit_task("legacy", {"title": "Renamed legacy task"})

        self.assertEqual(edited.title, "Renamed legacy task")
        self.assertEqual(edited.start_ref, "refs/heads/origin/main")
        with self.assertRaisesRegex(db.QueueError, "remote-tracking"):
            self.queue.edit_task("legacy", {"start_ref": "origin/main"})
        self.assertEqual(
            self.queue.edit_task("legacy", rereviewed(edited, {"start_ref": "main"})).start_ref, "refs/heads/main",
        )

    def test_invalid_checks_rejected(self) -> None:
        with self.assertRaisesRegex(db.QueueError, "checks"):
            self.add("bad", checks=[{"type": "issue_open", "repo": REPO}])
        self.assertIsNone(self.queue.task("bad"))
        self.add("good", checks=[ISSUE_OPEN_3815])
        with self.assertRaisesRegex(db.QueueError, "checks"):
            self.queue.edit_task("good", {"checks": [{"type": "nope"}]})
        edited = self.queue.edit_task("good", rereviewed(self.queue.task("good"), {"checks": [RELEASE_V0_11_1]}))
        self.assertEqual(edited.to_dict()["checks"], [RELEASE_V0_11_1])

    def test_merged_depends_on_must_be_subset(self) -> None:
        self.add("parent")
        self.add("other")
        with self.assertRaisesRegex(db.QueueError, "merged_depends_on"):
            self.add("child", depends_on=["parent"], merged_depends_on=["other"])
        self.assertIsNone(self.queue.task("child"))
        child = self.add("child", depends_on=["parent", "other"], merged_depends_on=["parent"])
        self.assertEqual(child.merged_depends_on, ("parent",))
        self.assertEqual(child.to_dict()["merged_depends_on"], ["parent"])
        # Dropping a merged edge from depends_on also drops its mode.
        self.assertEqual(
            self.queue.edit_task("child", rereviewed(child, {"depends_on": ["other"]})).merged_depends_on, (),
        )

    def test_unknown_dependency_rejected(self) -> None:
        with self.assertRaisesRegex(db.QueueError, "unknown prerequisite"):
            self.add("child", depends_on=["ghost"], merged_depends_on=["ghost"])
        self.assertIsNone(self.queue.task("child"))


class NoNetworkTests(PreflightCase):
    def test_local_reader_snapshot_never_runs_tools(self) -> None:
        self.add("declared", checks=[ISSUE_OPEN_3815])
        self.add("builtin", source_ref="https://github.com/curie-eng/curie/issues/2855", start_ref="main")
        self.add("parent")
        self.add("child", depends_on=["parent"], merged_depends_on=["parent"])
        self.complete("parent", handoff())
        forbidden = AssertionError("a view load ran a tool")

        with (
            mock.patch.object(subprocess, "run", side_effect=forbidden),
            mock.patch.object(subprocess, "Popen", side_effect=forbidden),
            mock.patch.object(checks, "subprocess_runner", side_effect=forbidden),
        ):
            snapshot = db.LocalQueueReader(self.queue.path).snapshot(now_epoch=NOW + 10)

        readiness = snapshot["readiness"]
        self.assertEqual(readiness["declared"]["hold_reason"], "check_unchecked")
        self.assertEqual(readiness["builtin"]["hold_reason"], "check_unchecked")
        child_edge = next(item for item in readiness["child"]["dependencies"] if item["id"] == "parent")
        self.assertEqual(child_edge["status"], "merge_check_pending")
        self.assertEqual(rows(self.queue, "SELECT * FROM check_results"), [])


class RefreshBudgetTests(PreflightCase):
    def _forty(self) -> tuple[list[str], FakeRunner]:
        ids, responses = [], {}
        for index in range(40):
            task_id = f"budget-{index:02d}"
            number = 2001 + index
            self.add(task_id, checks=[{"type": "issue_open", "repo": "owner/repo", "number": number}])
            responses[gh_issue(number, "owner/repo")] = ok(ISSUE_3815_OPEN)
            ids.append(task_id)
        return ids, self.runner(responses)

    def test_refresh_respects_max_calls_and_defers(self) -> None:
        ids, fake = self._forty()

        first = self.refresh(fake, now=NOW, max_calls=25)

        self.assertEqual(len(fake.tool_calls("issue")), 25)
        self.assertEqual(first["due"], 40)
        self.assertEqual(len(first["evaluated"]), 25)
        self.assertEqual(first["deferred"], 15)
        second = self.refresh(fake, now=NOW + 10, max_calls=25)
        self.assertEqual(len(second["evaluated"]), 15)
        self.assertEqual(second["deferred"], 0)
        self.assertEqual(len(fake.tool_calls("issue")), 40)
        self.assertTrue(all(self.ready(task_id, now=NOW + 20)["ready"] for task_id in ids))

    def test_refresh_stops_at_budget_seconds(self) -> None:
        _ids, fake = self._forty()
        clock = iter([0.0, 0.0, 0.0] + [1_000.0] * 200)

        result = self.refresh(
            fake, now=NOW, max_calls=100, budget_seconds=45.0, monotonic=lambda: next(clock),
        )

        evaluated = len(result["evaluated"])
        self.assertLess(evaluated, 40)
        self.assertEqual(result["deferred"], 40 - evaluated)
        self.assertEqual(len(fake.tool_calls("issue")), evaluated)


class RefreshSharingTests(PreflightCase):
    """One refresh makes each identical tool call once; nothing carries across refreshes."""

    def test_thirty_five_tasks_share_calls_in_one_tick(self) -> None:
        ids, responses = [], {
            git_ls_remote(self.root, "refs/heads/main"): ok(LS_REMOTE_MAIN),
            git_ls_remote(self.root, "refs/heads/next"): ok(LS_REMOTE_NEXT),
        }
        for index in range(35):
            task_id = f"share-{index:02d}"
            number = 3001 + index
            self.add(
                task_id, created_at=iso(NOW - 1_000 + index),
                start_ref="main" if index % 2 == 0 else "next",
                source_ref=f"owner/repo#{number}",
                checks=[in_milestone(number)],
            )
            responses[gh_issue(number, "owner/repo")] = ok(ISSUE_OPEN_IN_V0_10_0)
            ids.append(task_id)
        fake = self.runner(responses)

        result = checks.refresh(self.queue, runner=fake, now_epoch=NOW)

        self.assertEqual(len(fake.calls), 38, fake.calls)
        self.assertEqual(result["tool_calls"], 38)
        self.assertCountEqual(fake.tool_calls("ls-remote"), [
            git_ls_remote(self.root, "refs/heads/main"),
            git_ls_remote(self.root, "refs/heads/next"),
        ])
        self.assertEqual(fake.tool_calls("get-url"), [git_get_url(self.root)])
        self.assertEqual(len(fake.tool_calls("issue")), 35)
        self.assertEqual(len(set(fake.tool_calls("issue"))), 35)
        self.assertEqual(result["due"], 105)
        self.assertEqual(len(result["evaluated"]), 105)
        self.assertEqual(result["deferred"], 0)
        self.assertEqual(result["discarded"], 0)
        stored = rows(self.queue, "SELECT task_id,check_id,status,generation FROM check_results")
        per_task: dict[str, list[dict[str, Any]]] = {}
        for row in stored:
            per_task.setdefault(row["task_id"], []).append(row)
        self.assertEqual(sorted(per_task), ids)
        for task_id in ids:
            with self.subTest(task_id=task_id):
                own = per_task[task_id]
                self.assertEqual(len(own), 3)
                self.assertEqual(len({row["check_id"] for row in own}), 3)
                self.assertEqual({row["status"] for row in own}, {"pass"})
                self.assertEqual({row["generation"] for row in own}, {1})
                status = self.ready(task_id)
                self.assertTrue(status["ready"], status)
                self.assertEqual(
                    sorted(item["type"] for item in status["checks"]),
                    ["base_ref_exists", "issue_in_milestone", "issue_open"],
                )

    def test_cap_counts_tool_calls_and_covered_items_still_run(self) -> None:
        open_one = {"type": "issue_open", "repo": "owner/repo", "number": 1}
        self.add("t1", created_at=iso(NOW - 400), checks=[open_one])
        self.add("t2", created_at=iso(NOW - 300), checks=[{**open_one, "number": 2}])
        self.add("t3", created_at=iso(NOW - 200), checks=[{**open_one, "number": 3}])
        # Covered by t1's call, so it runs even though the cap is reached before it.
        self.add("t4", created_at=iso(NOW - 100), checks=[in_milestone(1)])
        fake = self.runner({
            gh_issue(1, "owner/repo"): ok(ISSUE_OPEN_IN_V0_10_0),
            gh_issue(2, "owner/repo"): ok(ISSUE_3815_OPEN),
            gh_issue(3, "owner/repo"): ok(ISSUE_3815_OPEN),
        })

        result = self.refresh(fake, max_calls=2)

        self.assertLessEqual(len(fake.calls), 2)
        self.assertCountEqual(fake.calls, [gh_issue(1, "owner/repo"), gh_issue(2, "owner/repo")])
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(
            sorted(item["task_id"] for item in result["evaluated"]), ["t1", "t2", "t4"],
        )
        self.assertEqual(result["deferred"], 1)
        for task_id in ("t1", "t2", "t4"):
            self.assertTrue(self.ready(task_id)["ready"], task_id)
        deferred = self.ready("t3")
        self.assertEqual(deferred["hold_reason"], "check_unchecked")
        self.assertEqual(rows(self.queue, "SELECT status FROM check_results WHERE task_id='t3' AND status IS NOT NULL"), [])

        # The deferred item runs on the next refresh, which makes its own call.
        later = self.refresh(fake, now=NOW + 5, max_calls=2)
        self.assertEqual(later["tool_calls"], 1)
        self.assertEqual(fake.calls[-1], gh_issue(3, "owner/repo"))
        self.assertTrue(self.ready("t3", now=NOW + 15)["ready"])

    def test_none_caps_are_unlimited(self) -> None:
        responses = {}
        for index in range(70):
            number = 4001 + index
            self.add(f"many-{index:02d}", checks=[{"type": "issue_open", "repo": "owner/repo", "number": number}])
            responses[gh_issue(number, "owner/repo")] = ok(ISSUE_3815_OPEN)
        fake = self.runner(responses)
        # A clock that leaps an hour per reading would exhaust any finite budget at once.
        ticks = iter(float(step * 3_600) for step in range(10_000))

        result = self.refresh(
            fake, max_calls=None, budget_seconds=None, monotonic=lambda: next(ticks),
        )

        self.assertEqual(len(fake.calls), 70)
        self.assertEqual(result["tool_calls"], 70)
        self.assertEqual(len(result["evaluated"]), 70)
        self.assertEqual(result["deferred"], 0)

    def test_default_cap_counts_calls(self) -> None:
        self.assertFalse(hasattr(checks, "TICK_MAX_CHECKS"))
        self.assertGreaterEqual(checks.TICK_MAX_CALLS, 38)

    def test_nothing_is_reused_across_refreshes(self) -> None:
        self.add("a", checks=[ISSUE_OPEN_3815])
        fake = self.runner({gh_issue(3815): ok(ISSUE_3815_OPEN)})
        first = self.refresh(fake, now=NOW)
        self.assertEqual(first["tool_calls"], 1)

        self.add("b", checks=[ISSUE_OPEN_3815])
        second = self.refresh(fake, now=NOW + 5)

        self.assertEqual(fake.tool_calls("issue"), [gh_issue(3815), gh_issue(3815)])
        self.assertEqual(second["tool_calls"], 1)
        self.assertEqual([item["task_id"] for item in second["evaluated"]], ["b"])
        self.assertTrue(self.ready("a", now=NOW + 15)["ready"])
        self.assertTrue(self.ready("b", now=NOW + 15)["ready"])

    def test_evaluated_entries_describe_each_check(self) -> None:
        self.add("described", checks=[ISSUE_OPEN_2855])
        fake = self.runner({gh_issue(2855): ok(ISSUE_2855_CLOSED)})

        result = self.refresh(fake, task_ids=("described",), max_calls=None, budget_seconds=None)

        self.assertEqual(len(result["evaluated"]), 1)
        entry = result["evaluated"][0]
        self.assertEqual(entry["status"], "fail")
        self.assertEqual(entry["check"], checks.describe(ISSUE_OPEN_2855))
        self.assertEqual(entry["origin"], "declared")
        self.assertIn("issue is CLOSED", entry["detail"])


class RefreshBudgetConcurrencyTests(PreflightCase):
    """A budget cutoff never reserves a generation it will not store under."""

    def test_exhausted_budget_does_not_discard_a_concurrent_manual_result(self) -> None:
        self.add("b", checks=[ISSUE_OPEN_3815])
        scout = self.runner()
        scout_results: list[dict[str, Any]] = []

        def manual_answer(_argv: tuple[str, ...]):
            # While the manual call is in flight, a scout tick whose budget is already
            # spent runs: it must make no call and reserve nothing for b.
            clock = iter([0.0] + [1_000.0] * 50)
            scout_results.append(checks.refresh(
                self.queue, runner=scout, now_epoch=NOW, budget_seconds=45.0,
                monotonic=lambda: next(clock),
            ))
            return ok(ISSUE_3815_OPEN)

        manual = self.runner({gh_issue(3815): manual_answer})
        result = self.refresh(
            manual, task_ids=("b",), max_calls=None, budget_seconds=None,
        )

        self.assertEqual(scout.calls, [])
        self.assertEqual(scout_results[0]["tool_calls"], 0)
        self.assertEqual(scout_results[0]["evaluated"], [])
        self.assertEqual(len(manual.calls), 1)
        self.assertEqual(result["discarded"], 0)
        self.assertEqual([(item["task_id"], item["status"]) for item in result["evaluated"]], [("b", "pass")])
        stored = rows(self.queue, "SELECT status,generation FROM check_results WHERE task_id='b'")
        self.assertEqual([(row["status"], row["generation"]) for row in stored], [("pass", 1)])
        status = self.ready("b")
        self.assertTrue(status["ready"], status)

    def test_budget_cutoff_finishes_shared_calls_and_defers_the_rest_untouched(self) -> None:
        open_one = {"type": "issue_open", "repo": "owner/repo", "number": 1}
        open_three = {"type": "issue_open", "repo": "owner/repo", "number": 3}
        self.add("t1", created_at=iso(NOW - 400), checks=[open_one])
        self.add("t3", created_at=iso(NOW - 300), checks=[open_three])
        # Ordered after the unrelated t3, but covered by t1's call.
        self.add("t2", created_at=iso(NOW - 200), checks=[in_milestone(1)])
        seeded_at = NOW - checks.RETRY_TTL_SECONDS - 10
        self.refresh(
            self.runner({gh_issue(3, "owner/repo"): ok(ISSUE_2855_CLOSED)}),
            now=seeded_at, task_ids=("t3",),
        )
        before = rows(self.queue, "SELECT status,generation,checked_at FROM check_results WHERE task_id='t3'")
        self.assertEqual(len(before), 1)
        fake = self.runner({
            gh_issue(1, "owner/repo"): ok(ISSUE_OPEN_IN_V0_10_0),
            gh_issue(3, "owner/repo"): ok(ISSUE_3815_OPEN),
        })
        # The budget is spent once the first item has run.
        clock = iter([0.0, 0.0] + [1_000.0] * 50)

        result = self.refresh(fake, budget_seconds=45.0, monotonic=lambda: next(clock))

        self.assertEqual(fake.calls, [gh_issue(1, "owner/repo")])
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(
            sorted((item["task_id"], item["status"]) for item in result["evaluated"]),
            [("t1", "pass"), ("t2", "pass")],
        )
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["discarded"], 0)
        self.assertTrue(self.ready("t1")["ready"])
        self.assertTrue(self.ready("t2")["ready"])
        after = rows(self.queue, "SELECT status,generation,checked_at FROM check_results WHERE task_id='t3'")
        self.assertEqual(after, before)


class QueueReadinessTests(PreflightCase):
    def test_fail_then_pass_flips_ready_after_retry_ttl(self) -> None:
        self.add("retry", checks=[ISSUE_OPEN_3815])
        fake = self.runner({gh_issue(3815): [ok(ISSUE_2855_CLOSED), ok(ISSUE_3815_OPEN)]})
        # The add-time evaluation: scoped to the task, uncapped.
        self.refresh(fake, now=NOW, task_ids=("retry",), max_calls=None, budget_seconds=None)
        failed = self.ready("retry")
        self.assertEqual((failed["state"], failed["hold_reason"]), ("waiting", "check_failed"))

        # A scout tick before the retry window makes no call and changes nothing.
        early = checks.refresh(
            self.queue, runner=fake, now_epoch=NOW + checks.RETRY_TTL_SECONDS - 1,
        )
        self.assertEqual(early["tool_calls"], 0)
        self.assertEqual(len(fake.tool_calls("issue")), 1)
        self.assertEqual(
            self.ready("retry", now=NOW + checks.RETRY_TTL_SECONDS - 1)["hold_reason"], "check_failed",
        )

        later = NOW + checks.RETRY_TTL_SECONDS
        tick = checks.refresh(self.queue, runner=fake, now_epoch=later)

        self.assertEqual(tick["tool_calls"], 1)
        self.assertEqual(len(fake.tool_calls("issue")), 2)
        status = self.ready("retry", now=later + 10)
        self.assertTrue(status["ready"], status)
        self.assertEqual((status["state"], status["hold_reason"]), ("ready", None))

    def test_never_evaluated_reads_not_checked_yet(self) -> None:
        self.add("fresh", checks=[ISSUE_OPEN_3815])

        status = self.ready("fresh")

        self.assertEqual((status["state"], status["hold_reason"]), ("waiting", "check_unchecked"))
        self.assertEqual(
            status["reason"], f"Not checked yet: {checks.describe(ISSUE_OPEN_3815)}",
        )
        self.assertEqual([item["status"] for item in status["checks"]], ["unchecked"])
        for other in ("Check failed:", "Check could not be verified yet:", "Waiting for preflight"):
            self.assertFalse(status["reason"].startswith(other), status["reason"])
        self.assertIsNone(self.try_claim("fresh"))

    def test_lapsed_result_reads_waiting_for_re_evaluation(self) -> None:
        self.add("lapsed", checks=[ISSUE_OPEN_3815])
        self.refresh(
            self.runner({gh_issue(3815): ok(ISSUE_3815_OPEN)}),
            now=NOW - checks.RESULT_TTL_SECONDS - 100,
        )

        status = self.ready("lapsed", now=NOW)

        self.assertEqual((status["state"], status["hold_reason"]), ("waiting", "check_pending"))
        self.assertEqual(
            status["reason"],
            f"Waiting for preflight check re-evaluation: {checks.describe(ISSUE_OPEN_3815)}",
        )
        self.assertEqual([item["status"] for item in status["checks"]], ["pending"])
        for other in ("Check failed:", "Check could not be verified yet:", "Not checked yet:"):
            self.assertFalse(status["reason"].startswith(other), status["reason"])

    def test_passing_checks_waiting_only_on_capacity_read_ready(self) -> None:
        # Readiness reads no capacity or pacing state: with every check passing the task is
        # ready even though no usage snapshot exists and nothing is dispatching.
        self.add("passing", source_ref="owner/repo#12", start_ref="main", checks=[in_milestone(12)])
        fake = self.runner({
            git_ls_remote(self.root): ok(LS_REMOTE_MAIN),
            gh_issue(12, "owner/repo"): ok(ISSUE_OPEN_IN_V0_10_0),
        })
        self.refresh(fake)

        status = self.ready("passing")

        self.assertTrue(status["ready"], status)
        self.assertEqual((status["state"], status["hold_reason"]), ("ready", None))
        self.assertEqual({item["status"] for item in status["checks"]}, {"pass"})
        self.assertIn("passing", self.eligible_ids())


class CollisionTests(PreflightCase):
    def test_same_work_group_serializes(self) -> None:
        self.add("first", work_group="Report work", priority=1)
        self.add("second", work_group="Report work", priority=2)
        attempt = self.claim("first")

        status = self.ready("second")
        self.assertEqual((status["state"], status["hold_reason"]), ("waiting", "collision"))
        self.assertEqual(status["reason"], "Serialized behind first (shared group:Report work)")
        self.assertIsNone(self.try_claim("second"))
        self.assertNotIn("second", self.eligible_ids())
        self.assertEqual(self.queue.collision_keys(["first", "second"]), {
            "first": frozenset({"group:Report work"}),
            "second": frozenset({"group:Report work"}),
        })

        self.terminal("first", attempt, "failed", reason(), now=NOW + 5)
        self.assertTrue(self.ready("second")["ready"])
        self.assertIsNotNone(self.try_claim("second"))

    def test_same_issue_key_serializes_across_forms(self) -> None:
        self.add("first", work_group="Alpha", source_ref="owner/repo#12")
        self.add("second", work_group="Beta", source_ref="https://github.com/owner/repo/issues/12")
        fake = self.runner({gh_issue(12, "owner/repo"): ok(ISSUE_3815_OPEN)})
        self.refresh(fake)
        self.assertTrue(self.ready("second")["ready"])
        self.assertIsNotNone(self.try_claim("first"))

        status = self.ready("second")
        self.assertEqual(status["hold_reason"], "collision")
        self.assertEqual(status["reason"], "Serialized behind first (shared issue:owner/repo#12)")
        self.assertIsNone(self.try_claim("second"))

    def test_unrelated_tasks_run_concurrently(self) -> None:
        self.add("alpha-work", work_group="Alpha")
        self.add("beta-work", work_group="Beta")
        self.add("loose")
        self.assertIsNotNone(self.try_claim("alpha-work"))
        self.assertIsNotNone(self.try_claim("beta-work"))
        self.assertIsNotNone(self.try_claim("loose"))
        self.assertEqual(self.queue.collision_keys(["loose"]), {"loose": frozenset()})

    def test_goal_members_exempt_from_collision(self) -> None:
        self.add("member-a", work_group="Goal work")
        self.add("member-b", work_group="Goal work")
        self.add("outsider", work_group="Goal work")
        add_goal(self.queue, "goal-1", ["member-a", "member-b"], max_inflight=2)

        self.assertEqual(self.queue.collision_keys(["member-a"]), {"member-a": frozenset()})
        self.assertIsNotNone(self.try_claim("member-a"))
        self.assertTrue(self.ready("member-b")["ready"])
        self.assertIsNotNone(self.try_claim("member-b"))
        # A non-goal task still waits behind goal work holding the same key.
        outsider = self.ready("outsider")
        self.assertEqual(outsider["hold_reason"], "collision")
        self.assertIsNone(self.try_claim("outsider"))


class AccountBackoffStoreTests(PreflightCase):
    def hold(self, cause: str = "activation_unswitched") -> db.AccountHold:
        return db.AccountHold("alpha", "alpha-account", cause, "requested account did not become active")

    def backoff_rows(self) -> list[dict[str, Any]]:
        return rows(self.queue, "SELECT * FROM account_backoff ORDER BY provider_id,account_id")

    def test_hold_deletes_claimed_attempt_and_records_backoff(self) -> None:
        self.add("held")
        attempt = self.claim("held")
        with mock.patch.object(db.time, "time", return_value=NOW):
            changed = self.queue.abort_unlaunched_attempt(
                "held", KEY, attempt.id, "activation unavailable", account_hold=self.hold(),
            )

        self.assertTrue(changed)
        self.assertEqual(self.queue.attempts(task_id="held"), [])
        self.assertIsNone(self.queue.claim_for("held"))
        stored = self.backoff_rows()
        self.assertEqual(
            [(row["provider_id"], row["account_id"], row["cause"], row["failures"]) for row in stored],
            [("alpha", "alpha-account", "activation_unswitched", 1)],
        )
        self.assertEqual(db._timestamp_epoch(stored[0]["not_before"]), NOW + 1_800)
        self.assertEqual(len(self.queue.account_backoffs(now_epoch=NOW + 1)), 1)
        self.assertEqual(self.queue.account_backoffs(now_epoch=NOW + 1_801), [])
        self.assertTrue(self.ready("held")["ready"])

        # A held recovery attempt restores its scheduled projection before the delete.
        self.add("retry")
        source = self.fail_task("retry")
        self.queue.requeue("retry", attempt_id=source.id, now_epoch=NOW + 1)
        consumed = self.claim("retry", now=NOW + 2)
        self.assertEqual(self.queue.recovery_for("retry").state, "consumed")
        with mock.patch.object(db.time, "time", return_value=NOW + 3):
            self.queue.abort_unlaunched_attempt(
                "retry", KEY, consumed.id, "provider launcher unavailable",
                account_hold=self.hold("codex_daemon_start"),
            )
        self.assertEqual([item.id for item in self.queue.attempts(task_id="retry")], [source.id])
        recovery = self.queue.recovery_for("retry")
        self.assertEqual((recovery.state, recovery.consumed_by_attempt_id), ("scheduled", None))

        again = self.claim("held", now=NOW + 4)
        with self.assertRaises(db.QueueError):
            self.queue.abort_unlaunched_attempt(
                "held", KEY, again.id, "unknown", account_hold=self.hold("bogus_cause"),
            )

    def test_backoff_doubles_and_caps(self) -> None:
        self.add("flapping")
        deltas = []
        for index in range(5):
            moment = NOW + index * 20_000
            attempt = self.claim("flapping", now=moment)
            with mock.patch.object(db.time, "time", return_value=moment):
                self.queue.abort_unlaunched_attempt(
                    "flapping", KEY, attempt.id, "activation unavailable", account_hold=self.hold(),
                )
            row = self.backoff_rows()[0]
            self.assertEqual(row["failures"], index + 1)
            deltas.append(db._timestamp_epoch(row["not_before"]) - moment)
        self.assertEqual(deltas, [1_800, 3_600, 7_200, 14_400, 14_400])
        self.assertEqual((db.BACKOFF_INITIAL_SECONDS, db.BACKOFF_MAX_SECONDS), (1_800, 14_400))
        self.assertEqual(self.queue.attempts(task_id="flapping"), [])

    def test_dispatched_record_clears_backoff(self) -> None:
        self.add("held")
        attempt = self.claim("held")
        with mock.patch.object(db.time, "time", return_value=NOW):
            self.queue.abort_unlaunched_attempt(
                "held", KEY, attempt.id, "activation unavailable", account_hold=self.hold(),
            )
        self.assertEqual(len(self.backoff_rows()), 1)

        launched = self.claim("held", now=NOW + 10)
        self.queue.record(
            "held", KEY, attempt_id=launched.id, status="dispatched",
            provider_id="alpha", account_id="alpha-account", router_job_id="job-held",
            timestamp=iso(NOW + 10), now_epoch=NOW + 10,
        )

        self.assertEqual(self.backoff_rows(), [])
        self.assertEqual(self.queue.account_backoffs(now_epoch=NOW + 11), [])


class ResumeWhenTests(PreflightCase):
    def test_resume_when_accepted_on_failed_authority_required(self) -> None:
        value = db.validate_outcome("failed", authority(resume_when=[
            {"type": "base_ref_exists", "ref": "epic/next"}, PR_2994_INTO_EPIC,
        ]))
        self.assertEqual(value["resume_when"], [
            {"type": "base_ref_exists", "ref": "refs/heads/epic/next"}, PR_2994_INTO_EPIC,
        ])
        self.add("blocked")
        attempt = self.block("blocked", authority(resume_when=[PR_2994_INTO_EPIC]))
        stored = json.loads(rows(
            self.queue, "SELECT outcome_json FROM task_attempts WHERE id=?", (attempt.id,),
        )[0]["outcome_json"])
        self.assertEqual(stored["resume_when"], [PR_2994_INTO_EPIC])
        self.assertEqual(stored["reason"]["code"], "authority_required")

    def test_resume_when_rejected_elsewhere(self) -> None:
        resume = [PR_2994_INTO_EPIC]
        cases = {
            "skipped": ("skipped", authority(resume_when=resume)),
            "retryable": ("failed", {
                "reason": {"code": "retryable", "detail": "flaky", "signature": "retryable:x"},
                "resume_when": resume,
            }),
            "invalid spec": ("failed", authority(resume_when=[{"type": "issue_open", "repo": REPO}])),
            "empty": ("failed", authority(resume_when=[])),
        }
        for label, (status, outcome) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(db.QueueError, "resume_when"):
                db.validate_outcome(status, outcome)


class ResumeTests(PreflightCase):
    def resumable(self, task_id: str = "blocked", spec: dict[str, object] = PR_2994_INTO_EPIC):
        self.add(task_id)
        return self.block(task_id, authority(resume_when=[spec]))

    def test_resume_schedules_on_fresh_pass(self) -> None:
        attempt = self.resumable()
        still_open = with_fields(PR_3795_OPEN, number=2994, baseRefName="epic/aws-secrets")
        fake = self.runner({gh_pr(2994): [ok(still_open), ok(PR_2994_MERGED)]})
        work = self.queue.check_work(now_epoch=NOW + 10, task_ids=["blocked"])
        self.assertEqual([(item.origin, item.spec) for item in work], [("resume", PR_2994_INTO_EPIC)])

        self.refresh(fake, now=NOW + 10)
        self.assertEqual(self.queue.resume_satisfied_holds(now_epoch=NOW + 20), [])
        self.assertIsNone(self.queue.recovery_for("blocked"))

        self.refresh(fake, now=NOW + 700)
        resumed = self.queue.resume_satisfied_holds(now_epoch=NOW + 710)

        self.assertEqual([(item["task_id"], item["after_attempt_id"]) for item in resumed],
                         [("blocked", attempt.id)])
        self.assertIn("resume_when satisfied", resumed[0]["detail"])
        recovery = self.queue.recovery_for("blocked")
        self.assertEqual(
            (recovery.state, recovery.origin, recovery.mode, recovery.reason_code),
            ("scheduled", "automatic", "verification", "authority_required"),
        )
        self.assertTrue(self.ready("blocked", now=NOW + 720)["ready"])
        self.assertIsNotNone(self.try_claim("blocked", now=NOW + 720))

    def test_resume_ignores_pass_older_than_failure(self) -> None:
        self.resumable()
        # Evaluated in the same second the failure was recorded: not evidence the blocker cleared.
        self.refresh(self.runner({gh_pr(2994): ok(PR_2994_MERGED)}), now=NOW)

        self.assertEqual(self.queue.resume_satisfied_holds(now_epoch=NOW + 5), [])
        self.assertIsNone(self.queue.recovery_for("blocked"))
        self.assertEqual(
            [item.spec for item in self.queue.check_work(now_epoch=NOW + 5, task_ids=["blocked"])],
            [PR_2994_INTO_EPIC],
        )

    def test_resume_ignores_expired_pass(self) -> None:
        self.resumable(spec=RELEASE_V0_11_1)
        self.refresh(self.runner({gh_release("v0.11.1"): ok(RELEASE_0_11_1)}), now=NOW + 10)

        expired = NOW + 10 + checks.RESULT_TTL_SECONDS + 1
        self.assertEqual(self.queue.resume_satisfied_holds(now_epoch=expired), [])
        self.assertIsNone(self.queue.recovery_for("blocked"))

    def test_resume_overwrites_automatic_held_row(self) -> None:
        self.resumable()
        self.add("child", depends_on=["blocked"])
        self.queue.reconcile_recoveries(now_epoch=NOW + 5, dry_run=False)
        self.assertEqual(self.queue.recovery_for("blocked").state, "held")

        self.refresh(self.runner({gh_pr(2994): ok(PR_2994_MERGED)}), now=NOW + 10)
        self.assertEqual(len(self.queue.resume_satisfied_holds(now_epoch=NOW + 20)), 1)
        self.assertEqual(self.queue.recovery_for("blocked").state, "scheduled")

        self.queue.reconcile_recoveries(now_epoch=NOW + 30, dry_run=False)
        self.assertEqual(self.queue.recovery_for("blocked").state, "scheduled")
        self.assertTrue(self.ready("blocked", now=NOW + 40)["ready"])

    def test_repeated_blocker_is_not_resumed_twice(self) -> None:
        self.resumable()
        fake = self.runner({gh_pr(2994): ok(PR_2994_MERGED)})
        self.refresh(fake, now=NOW + 10)
        self.assertEqual(len(self.queue.resume_satisfied_holds(now_epoch=NOW + 20)), 1)
        second = self.claim("blocked", now=NOW + 30)
        self.terminal(
            "blocked", second, "failed", authority(resume_when=[PR_2994_INTO_EPIC]), now=NOW + 40,
        )

        self.refresh(fake, now=NOW + 50)
        self.assertEqual(self.queue.resume_satisfied_holds(now_epoch=NOW + 60), [])
        recovery = self.queue.recovery_for("blocked")
        self.assertTrue(recovery is None or recovery.state != "scheduled", recovery)
        notices = self.queue.reserve_blocker_notices(now_epoch=NOW + 70)
        self.assertEqual(notices, [{"task_id": "blocked", "attempt_id": second.id}])


class BlockerNoticeStoreTests(PreflightCase):
    def test_reserve_returns_each_blocker_once(self) -> None:
        self.add("standalone")
        blocked = self.block("standalone")
        self.add("resumable")
        self.block("resumable", authority(resume_when=[PR_2994_INTO_EPIC]))
        self.add("flaky")
        self.fail_task("flaky")

        first = self.queue.reserve_blocker_notices(now_epoch=NOW + 10)
        self.assertEqual(first, [{"task_id": "standalone", "attempt_id": blocked.id}])
        self.assertEqual(self.queue.reserve_blocker_notices(now_epoch=NOW + 20), [])
        self.assertEqual(
            rows(self.queue, "SELECT attempt_id,task_id,seeded FROM blocker_notices"),
            [{"attempt_id": blocked.id, "task_id": "standalone", "seeded": 0}],
        )

    def test_seeded_attempts_are_never_reserved(self) -> None:
        self.add("historic")
        self.block("historic")
        downgrade_to_v2(self.queue.path)

        migrated = db.QueueDB(self.queue.path)
        migrated.initialize()

        self.assertEqual(migrated.reserve_blocker_notices(now_epoch=NOW + 10), [])


class MigrationV3Tests(PreflightCase):
    def v2_database(self) -> Path:
        self.add("legacy")
        self.block("legacy")
        downgrade_to_v2(self.queue.path)
        return self.queue.path

    def test_v2_database_migrates_to_v3_and_seeds_notices(self) -> None:
        path = self.v2_database()
        attempt = rows(self.queue, "SELECT id FROM task_attempts WHERE task_id='legacy'")[0]["id"]

        db.QueueDB(path).initialize()

        with sqlite3.connect(path) as connection:
            versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            notices = connection.execute(
                "SELECT attempt_id,task_id,seeded FROM blocker_notices",
            ).fetchall()
        self.assertIn(3, versions)
        self.assertTrue({"checks_json", "merged_depends_on_json"} <= columns)
        self.assertTrue({"account_backoff", "check_results", "blocker_notices"} <= tables)
        self.assertEqual(notices, [(attempt, "legacy", 1)])
        migrated = db.QueueDB(path).task("legacy")
        self.assertEqual((migrated.checks, migrated.merged_depends_on), ((), ()))

    def test_initialize_is_idempotent(self) -> None:
        path = self.v2_database()
        db.QueueDB(path).initialize()
        before = table_counts(path)

        db.QueueDB(path).initialize()

        self.assertEqual(table_counts(path), before)
        self.assertEqual(before["blocker_notices"], 1)


class RootBlockerTests(PreflightCase):
    def held_chain(self):
        self.add("A")
        self.add("B", depends_on=["A"])
        self.add("C", depends_on=["B"])
        attempt = self.block("A", authority("Missing GitHub test actor"))
        self.queue.reconcile_recoveries(now_epoch=NOW + 5, dry_run=False)
        self.assertEqual(self.queue.recovery_for("A").state, "held")
        return attempt

    def test_root_blocker_for_held_chain(self) -> None:
        self.held_chain()
        for task_id in ("B", "C"):
            with self.subTest(task_id=task_id):
                root = self.ready(task_id)["root_blocker"]
                self.assertEqual(
                    {key: root[key] for key in ("task_id", "title", "status", "reason")},
                    {"task_id": "A", "title": "A", "status": "held", "reason": "Missing GitHub test actor"},
                )

    def test_root_blocker_done_but_unmerged_parent(self) -> None:
        self.add("P")
        self.add("C", depends_on=["P"], merged_depends_on=["P"])
        self.add("G", depends_on=["C"])
        self.complete("P", handoff())
        self.refresh(self.runner({gh_pr("task/x", "owner/repo"): ok(TASK_X_PR_OPEN)}), now=NOW + 20)

        for task_id in ("C", "G"):
            with self.subTest(task_id=task_id):
                root = self.ready(task_id, now=NOW + 30)["root_blocker"]
                self.assertEqual(root["task_id"], "P")
                self.assertIn("not merged into main", root["reason"])

    def test_held_report_lists_held_recovery_with_descendants_read_only(self) -> None:
        attempt = self.held_chain()
        before = table_counts(self.queue.path)

        report = self.queue.held_authority_report()

        self.assertEqual(table_counts(self.queue.path), before)
        self.assertEqual(len(report), 1)
        item = report[0]
        self.assertEqual(item["source"], "held_recovery")
        self.assertEqual(item["task_id"], "A")
        self.assertEqual(item["title"], "A")
        self.assertEqual(item["detail"], "Missing GitHub test actor")
        self.assertEqual(item["descendants"], ["B", "C"])
        self.assertEqual(item["blocked_descendants"], 2)
        self.assertEqual(item["source_attempt_id"], attempt.id)
        self.assertEqual(set(item), {
            "source", "task_id", "title", "held_since", "detail",
            "blocked_descendants", "descendants", "source_attempt_id", "resume_when",
            "reason_code", "queue_time_knowable",
        })

    def test_held_report_includes_standalone_failed_blocker(self) -> None:
        self.add("S")
        attempt = self.block("S", authority("Production deploy key is not provisioned"))
        decisions = self.queue.reconcile_recoveries(now_epoch=NOW + 5, dry_run=False)
        self.assertEqual([item for item in decisions if item.task_id == "S"], [])
        self.assertIsNone(self.queue.recovery_for("S"))
        self.assertEqual(
            self.queue.reserve_blocker_notices(now_epoch=NOW + 10),
            [{"task_id": "S", "attempt_id": attempt.id}],
        )

        report = self.queue.held_authority_report()

        self.assertEqual([(item["task_id"], item["source"]) for item in report], [("S", "failed_attempt")])
        terminal_at = self.queue.attempts(task_id="S")[0].terminal_at
        self.assertEqual(report[0]["held_since"], terminal_at)
        self.assertEqual(report[0]["detail"], "Production deploy key is not provisioned")
        self.assertEqual(report[0]["descendants"], [])

    def test_requeued_or_retryable_absent_from_report(self) -> None:
        self.add("S")
        source = self.block("S")
        self.add("R")
        self.fail_task("R")
        self.assertEqual([item["task_id"] for item in self.queue.held_authority_report()], ["S"])

        decision = self.queue.requeue(
            "S", attempt_id=source.id, now_epoch=NOW + 10,
            override_reason="Brian provisioned the test actor",
        )
        self.assertEqual(decision.state, "scheduled")

        self.assertEqual(self.queue.held_authority_report(), [])


if __name__ == "__main__":
    unittest.main()
