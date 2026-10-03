"""Queue-time readiness review: the add/edit refusal, grants, measurement, and backfill.

Reviews are asserted through ``QueueDB`` public methods and the real CLI. Every refusal
path is paired with a realistic review that must be accepted, so a gate that refuses
everything cannot pass. The backfill's kubectl probe replays the shape recorded in the
launch-probe module through ``FakeRunner``.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from tests.test_bonus_dependency_recovery import KEY, NOW, captured_json, rows, runtime, task
from tests.test_bonus_drain_preflight_checks import (
    ISSUE_3815_OPEN, PreflightCase, authority, exited, gh_issue, ok, table_counts,
)
from tests.test_bonus_drain_preflight_dispatch import HermeticEnvironment
from tests.test_bonus_drain_launch_probe_checks import K8S_SERVICE, KUBECTL_NOT_FOUND_STDERR, kubectl
from tests import test_bonus_drain_package as package_tests
from tests.readiness_fixture import minimal_review, rereviewed, reviewed
from bonus_drain import checks, cli, db, dispatcher

SKILL_ROOT = package_tests.SKILL_ROOT
REVIEW_REQUIRED = "readiness review required"
STALE = "readiness review predates the current contract"

# --- The curie-v0130-3833 shape --------------------------------------------------------
# The worker stopped because apps/worker/CLAUDE.md gates kernel.py; that was knowable from
# the governing instructions at queue time and is resolved by an explicit sacred_path grant.
ISSUE_3833_URL = "https://github.com/curie-eng/curie/issues/3833"
DONE_3833 = "PR into main with green checks fixes the worker kernel retry loop"
GRANT_3833 = {
    "id": "worker-kernel",
    "kind": "sacred_path",
    "scope": "Edit apps/worker/kernel.py, the path apps/worker/CLAUDE.md reserves, only for the retry-loop fix",
}


def review_3833(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "issue": "curie-eng/curie#3833",
        "adrs": ["docs/adr/0188-worker-kernel-retries.md"],
        "instructions": ["AGENTS.md", "apps/worker/CLAUDE.md"],
        "acceptance_criteria": [
            {"criterion": DONE_3833, "basis": "the retry loop lives in kernel.py and is unit-testable"},
        ],
        "findings": [
            {
                "category": "authority",
                "detail": "apps/worker/CLAUDE.md forbids editing kernel.py without Brian's approval",
                "resolution": {"grant": "worker-kernel"},
            },
        ],
        "reviewer": "planning thread",
    }
    value.update(changes)
    return value


def finding(category: str, resolution: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"category": category, "detail": f"fixture {category} finding", "resolution": resolution, **extra}


def stored_review(queue: db.QueueDB, task_id: str) -> dict[str, Any]:
    review = queue.task(task_id).to_dict()["readiness_review"]
    assert isinstance(review, dict), review
    return review


def set_review_json(queue: db.QueueDB, task_id: str, value: dict[str, Any] | None) -> None:
    with sqlite3.connect(queue.path) as connection:
        connection.execute(
            "UPDATE tasks SET readiness_review_json=? WHERE id=?",
            (None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":")), task_id),
        )


def without_stamps(review: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in review.items() if key not in {"reviewed_at", "review_digest"}}


class ReviewCase(PreflightCase):
    def add_3833(self, task_id: str = "curie-v0130-3833", **changes: Any):
        values = {
            "source_ref": ISSUE_3833_URL, "done_when": DONE_3833,
            "grants": [GRANT_3833], "readiness_review": review_3833(),
        }
        values.update(changes)
        return self.add(task_id, **values)

    def assertRefused(self, task_id: str, values: dict[str, Any], pattern: str = REVIEW_REQUIRED) -> None:
        with self.assertRaisesRegex(db.QueueError, pattern):
            self.queue.add_task(values)
        self.assertIsNone(self.queue.task(task_id))


# --- 1, 2: the add refusal and the stored review ----------------------------------------
class AddRequiresReviewTests(ReviewCase):
    def test_add_without_review_is_refused_and_inserts_nothing(self) -> None:
        before = table_counts(self.queue.path)
        self.assertRefused("bare", task("bare", self.root))
        self.assertEqual(table_counts(self.queue.path), before)
        # Liveness: the same task with a minimal review is accepted.
        self.assertIsNotNone(self.add("bare"))

    def test_valid_review_is_stored_stamped_and_round_trips(self) -> None:
        added = self.add_3833(readiness_review=review_3833(reviewed_at="1999-01-01T00:00:00Z"))

        review = added.to_dict()["readiness_review"]
        self.assertEqual(without_stamps(review), review_3833())
        self.assertRegex(review["reviewed_at"], r"^20\d\d-\d\d-\d\dT")
        self.assertNotEqual(review["reviewed_at"], "1999-01-01T00:00:00Z")
        self.assertRegex(review["review_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(stored_review(self.queue, added.id), review)
        self.assertEqual(added.to_dict()["grants"], [GRANT_3833])
        self.assertEqual(self.queue.review_problems(added.id), [])
        json.dumps(self.queue.task(added.id).to_dict())

    def test_task_without_grants_renders_empty_list(self) -> None:
        added = self.add("plain")
        self.assertEqual(added.grants, ())
        self.assertEqual(added.to_dict()["grants"], [])


class CliAddReviewTests(HermeticEnvironment, ReviewCase):
    def setUp(self) -> None:
        super().setUp()
        self.hermetic(self.mkdir("home"))
        self.cfg = runtime(self.queue.path)

    def run_cli(self, *argv: str) -> tuple[int, list[object], str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        # An issue source_ref adds the built-in issue_open check; replay the recorded open shape.
        fake = self.runner({gh_issue(3833, "curie-eng/curie"): ok(ISSUE_3815_OPEN)})
        with (
            mock.patch.object(cli, "_queue", return_value=(self.cfg, self.queue)),
            mock.patch.object(checks, "subprocess_runner", fake),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            captured_json() as payloads,
        ):
            code = cli.main(list(argv))
        return code, payloads, stdout.getvalue(), stderr.getvalue()

    def add_argv(self, task_id: str, *extra: str) -> list[str]:
        return [
            "add", "--database", str(self.queue.path), "--id", task_id, "--title", task_id,
            "--kind", "oneoff", "--size", "small", "--cwd", str(self.root),
            "--goal", f"complete {task_id}", "--json", *extra,
        ]

    def test_cli_add_without_review_exits_invalid_input(self) -> None:
        code, payloads, out, err = self.run_cli(*self.add_argv("no-review"))

        self.assertEqual(code, 2, (payloads, out, err))
        self.assertEqual(payloads[-1]["code"], "invalid_input")
        self.assertIn(REVIEW_REQUIRED, payloads[-1]["error"])
        self.assertIsNone(self.queue.task("no-review"))

    def test_cli_add_3833_with_grant_and_review_file(self) -> None:
        review_path = self.root / "review.json"
        review_path.write_text(json.dumps(review_3833()), encoding="utf-8")

        code, payloads, out, err = self.run_cli(*self.add_argv(
            "curie-v0130-3833", "--source-ref", ISSUE_3833_URL, "--done-when", DONE_3833,
            "--grant", json.dumps(GRANT_3833), "--readiness-review", f"@{review_path}",
        ))

        self.assertEqual(code, 0, (payloads, out, err))
        stored = self.queue.task("curie-v0130-3833")
        self.assertEqual(stored.to_dict()["grants"], [GRANT_3833])
        self.assertEqual(without_stamps(stored.to_dict()["readiness_review"]), review_3833())
        self.assertEqual(payloads[0]["task"]["grants"], [GRANT_3833])

    def test_cli_add_with_dangling_grant_reference_is_refused(self) -> None:
        code, payloads, _out, _err = self.run_cli(*self.add_argv(
            "dangling", "--source-ref", ISSUE_3833_URL, "--done-when", DONE_3833,
            "--readiness-review", json.dumps(review_3833()),
        ))
        self.assertEqual(code, 2, payloads)
        self.assertEqual(payloads[-1]["code"], "invalid_input")
        self.assertIsNone(self.queue.task("dangling"))


# --- 3: each resolution kind is cross-checked against the task ---------------------------
class ResolutionTests(ReviewCase):
    def test_3833_review_with_sacred_path_grant_is_accepted(self) -> None:
        added = self.add_3833()
        self.assertEqual(added.to_dict()["grants"], [GRANT_3833])
        self.assertEqual(self.queue.review_problems(added.id), [])

    def test_dangling_grant_is_refused(self) -> None:
        for label, grants in (("no grants", []), ("other id", [{**GRANT_3833, "id": "other-grant"}])):
            with self.subTest(label):
                values = task("dangling", self.root, source_ref=ISSUE_3833_URL, done_when=DONE_3833,
                              grants=grants, readiness_review=review_3833())
                self.assertRefused("dangling", values)

    def test_prerequisite_must_be_a_dependency(self) -> None:
        self.add("frozen-contract")
        review = minimal_review(done_when="proof for child is retained")
        review["findings"] = [finding("feasibility", {"prerequisite": "frozen-contract"})]
        self.add("other")
        self.assertRefused("child", task("child", self.root, readiness_review=review))
        self.assertRefused("child", task("child", self.root, depends_on=["other"], readiness_review=review))
        accepted = self.add("child", depends_on=["frozen-contract"], readiness_review=review)
        self.assertEqual(accepted.depends_on, ("frozen-contract",))

    def test_done_when_resolution_must_match_the_task(self) -> None:
        rewritten = "Release images v0.13.0 are built; publishing them is out of scope"
        review = minimal_review(done_when=rewritten)
        review["findings"] = [finding("authority", {"done_when": rewritten})]
        self.assertRefused("images", task("images", self.root, done_when="Release v0.13.0 is published",
                                          readiness_review=review))
        accepted = self.add("images", done_when=f"  {rewritten}\n", readiness_review=review)
        self.assertEqual(self.queue.review_problems(accepted.id), [])

    def test_check_resolution_must_be_a_task_check(self) -> None:
        review = minimal_review(done_when="proof for e2e is retained")
        review["findings"] = [finding(
            "external_dependency", {"check": K8S_SERVICE}, dependency="cluster_resource",
        )]
        self.assertRefused("e2e", task("e2e", self.root, readiness_review=review))
        other = {**K8S_SERVICE, "name": "curie-email-e2e"}
        self.assertRefused("e2e", task("e2e", self.root, checks=[other], readiness_review=review))
        accepted = self.add("e2e", checks=[K8S_SERVICE], readiness_review=review)
        self.assertEqual(self.queue.review_problems(accepted.id), [])


# --- 4: structural rejections -----------------------------------------------------------
class StructureTests(ReviewCase):
    def test_structural_rejections(self) -> None:
        base = minimal_review(done_when="proof for shape is retained")
        two_keys = finding("authority", {"grant": "worker-kernel", "prerequisite": "x"})
        cases = {
            "resolution with two keys": {**base, "findings": [two_keys]},
            "unknown category": {**base, "findings": [finding("vibes", {"done_when": "proof for shape is retained"})]},
            "external_dependency without dependency": {
                **base, "findings": [finding("external_dependency", {"done_when": "proof for shape is retained"})],
            },
            "dependency on authority finding": {
                **base, "findings": [finding("authority", {"done_when": "proof for shape is retained"},
                                             dependency="credential")],
            },
            "unknown dependency kind": {
                **base, "findings": [finding("external_dependency", {"done_when": "proof for shape is retained"},
                                             dependency="weather")],
            },
            "unknown resolution kind": {**base, "findings": [finding("authority", {"waive": True})]},
            "empty instructions": {**base, "instructions": []},
            "empty acceptance_criteria": {**base, "acceptance_criteria": []},
            "criterion without basis": {**base, "acceptance_criteria": [{"criterion": "x"}]},
            "unknown top-level key": {**base, "notes": "extra"},
            "unknown finding key": {**base, "findings": [{**finding("authority", {
                "done_when": "proof for shape is retained"}), "severity": "high"}]},
            "missing adrs": {key: value for key, value in base.items() if key != "adrs"},
            "not an object": ["AGENTS.md"],
        }
        for label, review in cases.items():
            with self.subTest(label):
                with self.assertRaises(db.QueueError):
                    self.queue.add_task(task("shape", self.root, readiness_review=review))
                self.assertIsNone(self.queue.task("shape"))
        # Liveness: the same base with a valid external dependency finding is accepted.
        valid = {**base, "findings": [finding(
            "external_dependency", {"done_when": "proof for shape is retained"}, dependency="provider_credit",
        )]}
        self.assertIsNotNone(self.add("shape", readiness_review=valid))

    def test_grant_shape_rejections(self) -> None:
        cases = {
            "unknown kind": [{**GRANT_3833, "kind": "anything"}],
            "bad id": [{**GRANT_3833, "id": "Worker Kernel"}],
            "duplicate id": [GRANT_3833, dict(GRANT_3833)],
            "extra key": [{**GRANT_3833, "expires": "never"}],
            "missing scope": [{"id": "worker-kernel", "kind": "sacred_path"}],
            "too many": [{"id": f"g{index}", "kind": "other", "scope": "x"} for index in range(17)],
        }
        for label, grants in cases.items():
            with self.subTest(label):
                with self.assertRaises(db.QueueError):
                    self.queue.add_task(task("granted", self.root, source_ref=ISSUE_3833_URL,
                                             done_when=DONE_3833, grants=grants, readiness_review=review_3833()))
                self.assertIsNone(self.queue.task("granted"))


# --- 5: the source issue must be the issue the review read ------------------------------
class SourceIssueTests(ReviewCase):
    def test_review_issue_must_match_source_ref(self) -> None:
        for label, issue in (("other issue", "curie-eng/curie#3822"), ("no issue", None)):
            with self.subTest(label):
                self.assertRefused("issue", task(
                    "issue", self.root, source_ref=ISSUE_3833_URL, done_when=DONE_3833,
                    grants=[GRANT_3833], readiness_review=review_3833(issue=issue),
                ))
        self.assertIsNotNone(self.add_3833("issue"))
        # A non-issue source_ref places no constraint on review.issue.
        self.assertIsNotNone(self.add("plan-sourced", source_ref="https://example.test/plan"))


# --- 6 + R1: edits re-review the contract -----------------------------------------------
class EditReviewTests(ReviewCase):
    def test_contract_edit_without_new_review_is_refused_even_without_findings(self) -> None:
        added = self.add("edited")
        self.assertEqual(stored_review(self.queue, "edited")["findings"], [])
        before = stored_review(self.queue, "edited")

        with self.assertRaisesRegex(db.QueueError, REVIEW_REQUIRED):
            self.queue.edit_task("edited", {"done_when": "a different proof"})

        self.assertEqual(self.queue.task("edited").done_when, added.done_when)
        self.assertEqual(stored_review(self.queue, "edited"), before)

    def test_contract_edit_with_new_review_is_accepted_and_restamped(self) -> None:
        self.add("edited")
        before = stored_review(self.queue, "edited")
        new_review = minimal_review(done_when="a different proof")

        edited = self.queue.edit_task("edited", {"done_when": "a different proof", "readiness_review": new_review})

        self.assertEqual(edited.done_when, "a different proof")
        after = stored_review(self.queue, "edited")
        self.assertEqual(without_stamps(after), new_review)
        self.assertRegex(after["review_digest"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(after["review_digest"], before["review_digest"])
        self.assertEqual(self.queue.review_problems("edited"), [])

    def test_title_and_priority_edits_keep_the_review_valid(self) -> None:
        self.add("renamed")
        before = stored_review(self.queue, "renamed")

        self.queue.edit_task("renamed", {"title": "Renamed task"})
        self.queue.edit_task("renamed", {"priority": 0})
        self.queue.edit_task("renamed", {"work_group": "Curie v0.13"})

        self.assertEqual(stored_review(self.queue, "renamed"), before)
        self.assertEqual(self.queue.review_problems("renamed"), [])

    def test_removing_a_check_the_review_relies_on_is_refused(self) -> None:
        review = minimal_review(done_when="proof for e2e is retained")
        review["findings"] = [finding(
            "external_dependency", {"check": K8S_SERVICE}, dependency="cluster_resource",
        )]
        self.add("e2e", checks=[K8S_SERVICE], readiness_review=review)

        with self.assertRaisesRegex(db.QueueError, REVIEW_REQUIRED):
            self.queue.edit_task("e2e", {"checks": [], "readiness_review": review})
        self.assertEqual(self.queue.task("e2e").to_dict()["checks"], [K8S_SERVICE])

        rewritten = minimal_review(done_when="proof for e2e is retained")
        edited = self.queue.edit_task("e2e", {"checks": [], "readiness_review": rewritten})
        self.assertEqual(edited.to_dict()["checks"], [])

    def test_done_when_finding_needs_a_review_of_the_new_text(self) -> None:
        old = "Release images v0.13.0 are built"
        review = minimal_review(done_when=old)
        review["findings"] = [finding("authority", {"done_when": old})]
        self.add("images", done_when=old, readiness_review=review)

        new = "Release images v0.13.0 are built and pushed to the staging registry"
        with self.assertRaisesRegex(db.QueueError, REVIEW_REQUIRED):
            self.queue.edit_task("images", {"done_when": new, "readiness_review": review})
        self.assertEqual(self.queue.task("images").done_when, old)

        renewed = minimal_review(done_when=new)
        renewed["findings"] = [finding("authority", {"done_when": new})]
        self.assertEqual(
            self.queue.edit_task("images", {"done_when": new, "readiness_review": renewed}).done_when, new,
        )

    def test_legacy_row_without_review(self) -> None:
        self.add("legacy")
        set_review_json(self.queue, "legacy", None)
        self.assertIsNone(self.queue.task("legacy").readiness_review)
        self.assertEqual(self.queue.review_problems("legacy"), ["readiness review missing"])

        self.assertEqual(self.queue.edit_task("legacy", {"title": "Legacy renamed"}).title, "Legacy renamed")
        self.assertEqual(self.queue.edit_task("legacy", {"priority": 1}).priority, 1)

        with self.assertRaisesRegex(db.QueueError, REVIEW_REQUIRED):
            self.queue.edit_task("legacy", {"goal": "corrected goal"})
        self.assertNotEqual(self.queue.task("legacy").goal, "corrected goal")

        edited = self.queue.edit_task("legacy", rereviewed(self.queue.task("legacy"), {"goal": "corrected goal"}))
        self.assertEqual(edited.goal, "corrected goal")
        self.assertEqual(self.queue.review_problems("legacy"), [])

    def test_grants_edit_requires_a_review_that_uses_them(self) -> None:
        self.add("granted", source_ref=ISSUE_3833_URL, done_when=DONE_3833,
                 readiness_review=review_3833(findings=[]))
        with self.assertRaisesRegex(db.QueueError, REVIEW_REQUIRED):
            self.queue.edit_task("granted", {"grants": [GRANT_3833]})
        self.assertEqual(self.queue.task("granted").grants, ())

        edited = self.queue.edit_task("granted", {"grants": [GRANT_3833], "readiness_review": review_3833()})
        self.assertEqual(edited.to_dict()["grants"], [GRANT_3833])
        self.assertEqual(self.queue.review_problems("granted"), [])

    def test_cli_edit_passes_review_through(self) -> None:
        self.add("cli-edit")
        changes = rereviewed(self.queue.task("cli-edit"), {"goal": "cli corrected goal"})
        with (
            mock.patch.object(cli, "_queue", return_value=(runtime(self.queue.path), self.queue)),
            mock.patch.object(checks, "subprocess_runner", self.runner()),
            captured_json() as payloads,
        ):
            refused = cli.main(["edit", "--database", str(self.queue.path), "cli-edit",
                                "--changes", json.dumps({"goal": "cli corrected goal"})])
            accepted = cli.main(["edit", "--database", str(self.queue.path), "cli-edit",
                                 "--changes", json.dumps(changes)])
        self.assertEqual(refused, 2, payloads)
        self.assertIn(REVIEW_REQUIRED, payloads[0]["error"])
        self.assertEqual(accepted, 0, payloads)
        self.assertEqual(self.queue.task("cli-edit").goal, "cli corrected goal")


class RoutingControlTests(ReviewCase):
    """F4: routing and display controls sit outside the reviewed contract."""

    def test_routing_setters_never_stale_the_review(self) -> None:
        self.add("routed")
        before_review = stored_review(self.queue, "routed")
        before_ready = self.ready("routed")

        self.queue.set_model("routed", "claude-fable-5")
        self.queue.set_mcp("routed", "linear-cn")
        self.queue.set_providers("routed", ["alpha"])
        self.queue.set_priority("routed", 0)

        self.assertIsNotNone(self.queue.task("routed").model)
        self.assertEqual(self.queue.review_problems("routed"), [])
        self.assertEqual(stored_review(self.queue, "routed"), before_review)
        after_ready = self.ready("routed")
        self.assertEqual(
            (after_ready["state"], after_ready["ready"], after_ready["hold_reason"]),
            (before_ready["state"], before_ready["ready"], before_ready["hold_reason"]),
        )

    def test_every_reviewed_field_edit_requires_a_review(self) -> None:
        self.add("parent")
        self.add("reviewed-fields", depends_on=["parent"])
        other = self.mkdir("other-checkout")
        changes = {
            "cwd": str(other),
            "goal": "a different goal",
            "context": "new context",
            "constraints": "new constraints",
            "precondition": "new precondition",
            "done_when": "a different proof",
            "source_ref": "https://example.test/other-plan",
            "start_ref": "main",
            "depends_on": [],
            "merged_depends_on": ["parent"],
            "checks": [K8S_SERVICE],
            "grants": [GRANT_3833],
        }
        before = self.queue.task("reviewed-fields").to_dict()
        for field, value in changes.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(db.QueueError, REVIEW_REQUIRED):
                    self.queue.edit_task("reviewed-fields", {field: value})
                self.assertEqual(self.queue.task("reviewed-fields").to_dict(), before)


class PreviewScriptTests(unittest.TestCase):
    """F1: the documented preview script still creates its example tasks."""

    SCRIPT = package_tests.REPO_ROOT / "scripts" / "preview-async-work.py"

    def test_examples_are_created_with_valid_reviews(self) -> None:
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            config_path = home / ".config" / "bonus-drain" / "config.json"
            config_path.parent.mkdir(parents=True)
            package_tests.BonusDrainPackageContractTests.installed_config(self, home, config_path)
            source_database = Path(json.loads(config_path.read_text())["database"])
            db.QueueDB(source_database).initialize()
            destination = root / "preview"
            environment = {
                **os.environ,
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "XDG_CACHE_HOME": str(home / ".cache"),
            }
            environment.pop("BONUS_DRAIN_CONFIG", None)

            completed = subprocess.run(
                [sys.executable, str(self.SCRIPT), "--source-config", str(config_path),
                 "--destination", str(destination), "--host", "preview.example.ts.net:8443", "--examples"],
                check=False, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["database"], str(destination / "queue.db"))
            preview = db.QueueDB(destination / "queue.db")
            ids = [
                "preview-01-plan", "preview-02-build", "preview-03-review", "preview-04-research",
            ]
            for task_id in ids:
                with self.subTest(task_id=task_id):
                    self.assertIsNotNone(preview.task(task_id))
                    self.assertIsNotNone(preview.task(task_id).readiness_review)
                    self.assertEqual(preview.review_problems(task_id), [])
            # The source queue is only read.
            self.assertEqual(db.QueueDB(source_database).tasks(), [])


# --- 7: contract hash stability ---------------------------------------------------------
class ContractHashTests(ReviewCase):
    @staticmethod
    def pre_review_hash(item: db.Task) -> str:
        """The contract identity before this change: the task dict without the review fields."""

        value = item.to_dict()
        for field in ("priority", "size", "active", "readiness_review", "grants"):
            value.pop(field, None)
        if value["start_ref"] is None:
            value.pop("start_ref")
        for field in ("checks", "merged_depends_on"):
            if not value[field]:
                value.pop(field)
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def test_review_does_not_change_the_contract_hash(self) -> None:
        added = self.add("hashed")
        original = db._contract_hash(added)
        self.assertEqual(original, self.pre_review_hash(added))

        set_review_json(self.queue, "hashed", None)
        self.assertEqual(db._contract_hash(self.queue.task("hashed")), original)

        richer = minimal_review(done_when=added.done_when)
        richer["adrs"] = ["docs/adr/0160.md"]
        self.queue.edit_task("hashed", {"readiness_review": richer})
        self.assertEqual(db._contract_hash(self.queue.task("hashed")), original)

    def test_grants_change_the_contract_hash(self) -> None:
        self.add("granted", source_ref=ISSUE_3833_URL, done_when=DONE_3833,
                 readiness_review=review_3833(findings=[]))
        before = db._contract_hash(self.queue.task("granted"))
        edited = self.queue.edit_task("granted", {"grants": [GRANT_3833], "readiness_review": review_3833()})
        self.assertNotEqual(db._contract_hash(edited), before)


# --- 8 + R3: grants and the default grant in the worker prompt --------------------------
PR_POLICY = (
    "This configured repository permits a branch push and pull request. "
    "Merge only when the task contract explicitly grants merge authority into a named "
    "epic/* branch, and only into that branch after all PR checks pass. "
    "Otherwise, do not merge."
)
ARTIFACT_POLICY = "Produce a branch and committed artifact only; do not push, publish, merge, or delete unrelated files."
GRANT_OVERRIDE = (
    "Queue-time grants listed in this prompt are explicit exceptions to this default: within each "
    "grant's stated scope, perform the granted action (including a merge, push, release, or "
    "infrastructure change it names) and do not stop or record authority_required for it."
)
GRANTS_HEADER = (
    "Authority Brian granted at queue time. These are explicit grants: act within them and do not "
    "stop, skip, or record authority_required for anything they cover:"
)
MERGE_GRANT = {
    "id": "merge-main", "kind": "merge",
    "scope": "Merge the PR for this task into main once all required checks pass",
}
PUBLISH_GRANT = {
    "id": "publish-notes", "kind": "release",
    "scope": "Push the branch to origin and publish the v0.13.0 release notes",
}
CONTRACT_HEADER = "Execute this authorized asynchronous task within its stated contract."


class PromptGrantTests(ReviewCase):
    def prompt(self, task_id: str, config=None) -> str:
        return dispatcher.render_prompt(
            config or runtime(self.queue.path), self.queue.task(task_id), KEY, "alpha", "alpha-account",
        )

    def pr_config(self):
        return replace(runtime(self.queue.path), pr_exceptions=(
            {"path": str(self.root), "allow_push": True, "allow_pr": True},
        ))

    def granted(self, task_id: str, grant: dict[str, Any]):
        review = minimal_review(done_when=f"proof for {task_id} is retained")
        review["findings"] = [finding("authority", {"grant": grant["id"]})]
        return self.add(task_id, grants=[grant], readiness_review=review)

    def policy_line(self, prompt: str) -> str:
        lines = prompt.split("\n")
        return lines[lines.index(CONTRACT_HEADER) + 1]

    def assertGrantRendered(self, prompt: str, policy: str, grant: dict[str, Any]) -> None:
        line = f"- {grant['scope']} ({grant['kind']}, grant {grant['id']})"
        self.assertIn(policy, prompt)
        self.assertIn(GRANT_OVERRIDE, prompt)
        self.assertIn(GRANTS_HEADER + "\n" + line, prompt)
        self.assertLess(prompt.index(policy), prompt.index(GRANT_OVERRIDE))
        self.assertLess(prompt.index(GRANT_OVERRIDE), prompt.index(GRANTS_HEADER))
        self.assertLess(prompt.index(line), prompt.index(dispatcher.PRECONDITION_EXECUTION_RULE))

    def test_merge_grant_in_pr_repository(self) -> None:
        self.granted("merge-task", MERGE_GRANT)
        self.assertGrantRendered(self.prompt("merge-task", self.pr_config()), PR_POLICY, MERGE_GRANT)

    def test_publication_grant_in_artifact_only_repository(self) -> None:
        self.granted("publish-task", PUBLISH_GRANT)
        self.assertGrantRendered(self.prompt("publish-task"), ARTIFACT_POLICY, PUBLISH_GRANT)

    def test_no_grants_keeps_policy_text_unchanged(self) -> None:
        self.add("plain")
        for label, config, policy in (
            ("pr repository", self.pr_config(), PR_POLICY),
            ("artifact only", runtime(self.queue.path), ARTIFACT_POLICY),
        ):
            with self.subTest(label):
                prompt = self.prompt("plain", config)
                self.assertEqual(self.policy_line(prompt), policy)
                self.assertNotIn(GRANT_OVERRIDE, prompt)
                self.assertNotIn("Authority Brian granted at queue time", prompt)

    def test_default_worker_grant_in_oneoff_and_recurring_prompts(self) -> None:
        self.assertIn("pre-existing lint, format, or type errors", dispatcher.DEFAULT_WORKER_GRANT)
        self.add("oneoff")
        self.add("weekly", kind="recurring", cadence="weekly")
        for task_id in ("oneoff", "weekly"):
            with self.subTest(task_id):
                prompt = self.prompt(task_id)
                self.assertIn(
                    dispatcher.PRECONDITION_EXECUTION_RULE + "\n" + dispatcher.DEFAULT_WORKER_GRANT, prompt,
                )


# --- 9 + R4: queue_time_knowable on blocker outcomes ------------------------------------
def blocker(code: str, knowable: Any = None, *, include: bool = True) -> dict[str, Any]:
    reason: dict[str, Any] = {"code": code, "detail": f"fixture {code}", "signature": f"{code}:fixture"}
    if include:
        reason["queue_time_knowable"] = knowable
    return {"reason": reason}


class OutcomeMeasurementTests(ReviewCase):
    CASES = (("failed", "authority_required"), ("skipped", "verification_needed"),
             ("failed", "verification_needed"), ("skipped", "authority_required"))

    def test_blocker_outcomes_require_the_field(self) -> None:
        for status, code in self.CASES:
            with self.subTest(status=status, code=code):
                with self.assertRaisesRegex(
                    db.QueueError,
                    "reason.queue_time_knowable is required for authority_required and verification_needed outcomes",
                ):
                    db.validate_outcome(status, blocker(code, include=False))

    def test_both_boolean_values_are_kept(self) -> None:
        for status, code in self.CASES:
            for knowable in (True, False):
                with self.subTest(status=status, code=code, knowable=knowable):
                    value = db.validate_outcome(status, blocker(code, knowable))
                    self.assertIs(value["reason"]["queue_time_knowable"], knowable)
                    self.assertEqual(value["reason"]["code"], code)

    def test_non_boolean_is_rejected(self) -> None:
        for bad in ("true", 1, 0, []):
            with self.subTest(bad=bad), self.assertRaisesRegex(db.QueueError, "queue_time_knowable"):
                db.validate_outcome("failed", blocker("authority_required", bad))

    def test_field_is_rejected_on_other_codes(self) -> None:
        for status, code in (("failed", "retryable"), ("skipped", "permanent"), ("failed", "unknown_launch")):
            with self.subTest(code=code), self.assertRaisesRegex(
                db.QueueError,
                "queue_time_knowable is only valid on authority_required or verification_needed reasons",
            ):
                db.validate_outcome(status, blocker(code, True))
        # Liveness: the same codes without the field are still accepted.
        self.assertEqual(
            db.validate_outcome("failed", blocker("retryable", include=False))["reason"]["code"], "retryable",
        )

    def test_explicit_null_is_accepted_as_unreported(self) -> None:
        for status, code in self.CASES:
            with self.subTest(status=status, code=code):
                value = db.validate_outcome(status, blocker(code, None))
                self.assertIn("queue_time_knowable", value["reason"])
                self.assertIsNone(value["reason"]["queue_time_knowable"])
                with self.assertRaisesRegex(db.QueueError, "reason.queue_time_knowable is required"):
                    db.validate_outcome(status, blocker(code, include=False))

    def test_historical_outcomes_without_the_field_still_validate(self) -> None:
        for status, code in self.CASES:
            with self.subTest(status=status, code=code):
                value = db.validate_outcome(status, blocker(code, include=False), require_structured_reason=False)
                self.assertNotIn("queue_time_knowable", value["reason"])

    def test_recorded_attempt_keeps_the_field(self) -> None:
        self.add("blocked")
        attempt = self.block("blocked", blocker("authority_required", True))
        stored = json.loads(rows(
            self.queue, "SELECT outcome_json FROM task_attempts WHERE id=?", (attempt.id,),
        )[0]["outcome_json"])
        self.assertEqual(stored["reason"]["code"], "authority_required")
        self.assertIs(stored["reason"]["queue_time_knowable"], True)

    def test_outcome_schema_and_contract_name_the_field(self) -> None:
        self.add("schema")
        attempt = self.claim("schema")
        prompt = dispatcher.render_prompt(
            runtime(self.queue.path), self.queue.task("schema"), KEY, "alpha", "alpha-account",
            attempt=attempt, outcome_path=self.root / "schema-outcome.json",
        )
        line = next(item for item in prompt.splitlines() if item.startswith("OUTCOME_SCHEMA="))
        schema = json.loads(line.removeprefix("OUTCOME_SCHEMA="))
        self.assertIn("queue_time_knowable", schema["reason"])
        self.assertIn("set reason.queue_time_knowable to true if the blocker already existed", prompt)


class RecordUnreportedTests(ReviewCase):
    """A worker that omits queue_time_knowable keeps its authority blocker, recorded as unreported."""

    RESUME = [{"type": "issue_open", "repo": "curie-eng/curie", "number": 3815}]

    def stored_outcome(self, attempt) -> dict[str, Any]:
        return json.loads(rows(
            self.queue, "SELECT outcome_json FROM task_attempts WHERE id=?", (attempt.id,),
        )[0]["outcome_json"])

    def test_missing_field_keeps_authority_outcome_as_unreported(self) -> None:
        self.add("omitted")
        outcome = authority("Missing GitHub test actor", resume_when=self.RESUME,
                            signature="authority_required:test-actor")
        outcome["reason"].pop("queue_time_knowable")
        attempt = self.block("omitted", outcome)

        stored = self.stored_outcome(attempt)
        self.assertEqual(stored["reason"]["code"], "authority_required")
        self.assertEqual(stored["reason"]["detail"], "Missing GitHub test actor")
        self.assertEqual(stored["reason"]["signature"], "authority_required:test-actor")
        self.assertIn("queue_time_knowable", stored["reason"])
        self.assertIsNone(stored["reason"]["queue_time_knowable"])
        self.assertEqual(stored["resume_when"], self.RESUME)
        self.assertEqual(self.queue.attempts(task_id="omitted")[0].reason_code, "authority_required")

        report = {item["task_id"]: item for item in self.queue.held_authority_report()}
        self.assertIn("omitted", report)
        self.assertEqual(report["omitted"]["reason_code"], "authority_required")
        self.assertIsNone(report["omitted"]["queue_time_knowable"])
        self.assertEqual(self.queue.blocker_measurement()["authority_required"],
                         {"knowable": 0, "not_knowable": 0, "unreported": 1})

    def test_otherwise_invalid_outcome_is_still_salvaged(self) -> None:
        self.add("garbled")
        attempt = self.block("garbled", {"reason": {
            "code": "made_up_code", "detail": "worker invented a code", "signature": "made_up_code:x",
        }})

        stored = self.stored_outcome(attempt)
        self.assertEqual(stored["reason"]["code"], "verification_needed")
        self.assertIs(stored["reason"]["queue_time_knowable"], False)
        self.assertEqual(self.queue.blocker_measurement()["verification_needed"],
                         {"knowable": 0, "not_knowable": 1, "unreported": 0})


# --- 10 + R5: held-report carries the measurement ---------------------------------------
class HeldReportMeasurementTests(HermeticEnvironment, ReviewCase):
    def setUp(self) -> None:
        super().setUp()
        self.hermetic(self.mkdir("home"))
        self.add("knowable")
        knowable = authority("Missing GitHub test actor")
        knowable["reason"]["queue_time_knowable"] = True
        self.knowable = self.block("knowable", knowable)
        self.add("emergent")
        self.emergent = self.block("emergent", {"reason": {
            "code": "verification_needed", "detail": "Integration suite needs a release image that was never built",
            "signature": "verification_needed:release-image", "queue_time_knowable": False,
        }})
        self.add("historic")
        self.historic = self.block("historic", authority("Production deploy key is not provisioned"))
        # An outcome recorded before the field existed: strip it as the historical row would lack it.
        with sqlite3.connect(self.queue.path) as connection:
            raw = connection.execute(
                "SELECT outcome_json FROM task_attempts WHERE id=?", (self.historic.id,),
            ).fetchone()[0]
            outcome = json.loads(raw)
            outcome["reason"].pop("queue_time_knowable", None)
            connection.execute(
                "UPDATE task_attempts SET outcome_json=? WHERE id=?", (json.dumps(outcome), self.historic.id),
            )

    def run_report(self, *extra: str) -> tuple[int, list[object], str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), captured_json() as payloads:
            code = cli.main(["held-report", "--database", str(self.queue.path), *extra])
        return code, payloads, stdout.getvalue()

    def test_report_items_carry_reason_code_and_knowable(self) -> None:
        report = {item["task_id"]: item for item in self.queue.held_authority_report()}

        self.assertEqual(set(report), {"knowable", "emergent", "historic"})
        self.assertEqual(
            (report["knowable"]["reason_code"], report["knowable"]["queue_time_knowable"]),
            ("authority_required", True),
        )
        self.assertIsNone(report["historic"]["queue_time_knowable"])
        emergent = report["emergent"]
        self.assertEqual(emergent["source"], "verification_attempt")
        self.assertEqual(emergent["reason_code"], "verification_needed")
        self.assertIs(emergent["queue_time_knowable"], False)
        self.assertEqual(emergent["detail"], "Integration suite needs a release image that was never built")
        self.assertIsNone(emergent["resume_when"])
        self.assertEqual(emergent["source_attempt_id"], self.emergent.id)

    def test_measurement_counts_per_code(self) -> None:
        self.assertEqual(self.queue.blocker_measurement(), {
            "authority_required": {"knowable": 1, "not_knowable": 0, "unreported": 1},
            "verification_needed": {"knowable": 0, "not_knowable": 1, "unreported": 0},
        })

    def test_measurement_always_lists_both_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            empty = db.QueueDB(Path(directory) / "queue.db")
            empty.initialize()
            zero = {"knowable": 0, "not_knowable": 0, "unreported": 0}
            self.assertEqual(empty.blocker_measurement(), {
                "authority_required": zero, "verification_needed": zero,
            })

    def test_cli_json_carries_items_and_measurement(self) -> None:
        before = table_counts(self.queue.path)
        code, payloads, _out = self.run_report("--json")

        self.assertEqual(code, 0, payloads)
        self.assertEqual(table_counts(self.queue.path), before)
        payload = payloads[0]
        self.assertEqual(set(payload), {"held", "measurement"})
        held = {item["task_id"]: item for item in payload["held"]}
        self.assertIs(held["emergent"]["queue_time_knowable"], False)
        self.assertEqual(held["emergent"]["reason_code"], "verification_needed")
        self.assertEqual(payload["measurement"], self.queue.blocker_measurement())

    def test_cli_human_output_prints_column_and_summary(self) -> None:
        code, payloads, out = self.run_report()

        self.assertEqual(code, 0, payloads)
        lines = out.rstrip("\n").split("\n")
        by_task = {line.split("\t")[0]: line.split("\t") for line in lines if "\t" in line}
        self.assertIn("yes", by_task["knowable"])
        self.assertIn("no", by_task["emergent"])
        self.assertNotIn("yes", by_task["historic"])
        self.assertNotIn("no", by_task["historic"])
        # Totals per code; how unreported rows enter <total> is left to the implementation.
        self.assertRegex(
            lines[-1],
            r"^queue-time knowable: authority_required 1/\d+, verification_needed 0/\d+ \(1 unreported\)$",
        )


# --- 11 + R1: the read-only backfill ----------------------------------------------------
class BackfillTests(HermeticEnvironment, ReviewCase):
    def setUp(self) -> None:
        super().setUp()
        self.hermetic(self.mkdir("home"))
        self.cfg = runtime(self.queue.path)
        now_patch = mock.patch.dict(os.environ, {"BONUS_DRAIN_NOW": str(NOW + 100)})
        now_patch.start()
        self.addCleanup(now_patch.stop)

        self.add("migrated")
        set_review_json(self.queue, "migrated", None)
        self.add("needs-svc", checks=[K8S_SERVICE])
        self.add("clean")
        self.add("weekly", kind="recurring", cadence="weekly")
        self.add("stale")
        stale = stored_review(self.queue, "stale")
        stale["review_digest"] = "0" * 64
        set_review_json(self.queue, "stale", stale)
        self.add("invalid")
        broken = stored_review(self.queue, "invalid")
        broken["findings"] = [finding("authority", {"grant": "never-granted"})]
        set_review_json(self.queue, "invalid", broken)
        # Excluded: a launched task, verified-done work, and a one-off with a failed attempt.
        self.add("launched")
        self.claim("launched")
        self.add("done")
        self.complete("done")
        self.add("failed-once")
        self.fail_task("failed-once")

        self.argv = kubectl("k8", "service", "nope-xyz", "default")
        # A stored result from an earlier scout refresh, so "unchanged" is not trivially empty.
        self.refresh(self.runner({self.argv: exited(1, KUBECTL_NOT_FOUND_STDERR)}),
                     task_ids=("needs-svc",), due_only=False, max_calls=None, budget_seconds=None)

    def backfill(self, *extra: str):
        fake = self.runner({self.argv: exited(1, KUBECTL_NOT_FOUND_STDERR)})
        stdout = io.StringIO()
        with (
            mock.patch.object(cli, "_queue", return_value=(self.cfg, self.queue)),
            mock.patch.object(checks, "subprocess_runner", fake),
            contextlib.redirect_stdout(stdout),
            captured_json() as payloads,
        ):
            code = cli.main(["readiness-backfill", "--database", str(self.queue.path), *extra])
        return code, payloads, stdout.getvalue(), fake

    def snapshot(self) -> tuple[object, ...]:
        return (
            rows(self.queue, "SELECT * FROM check_results ORDER BY task_id,check_id"),
            rows(self.queue, "SELECT * FROM tasks ORDER BY id"),
            table_counts(self.queue.path),
        )

    def test_backfill_json_reports_each_candidate_read_only(self) -> None:
        before = self.snapshot()
        self.assertTrue(before[0], "fixture should hold a stored check result")

        code, payloads, _out, fake = self.backfill("--json")

        self.assertEqual(code, 0, payloads)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(fake.calls, [self.argv])
        payload = payloads[0]
        tasks = {item["task_id"]: item for item in payload["tasks"]}
        self.assertEqual(set(tasks), {"migrated", "needs-svc", "clean", "weekly", "stale", "invalid"})
        for item in tasks.values():
            self.assertTrue({"task_id", "title", "kind", "review", "review_problems", "checks",
                             "ready_for_launch"} <= set(item), item)

        self.assertEqual(tasks["migrated"]["review"], "missing")
        self.assertFalse(tasks["migrated"]["ready_for_launch"])
        self.assertEqual(tasks["stale"]["review"], "stale")
        self.assertIn(STALE, tasks["stale"]["review_problems"])
        self.assertFalse(tasks["stale"]["ready_for_launch"])
        self.assertEqual(tasks["invalid"]["review"], "invalid")
        self.assertTrue(any("never-granted" in problem for problem in tasks["invalid"]["review_problems"]))

        svc = tasks["needs-svc"]
        self.assertEqual(svc["review"], "valid")
        self.assertEqual([entry["status"] for entry in svc["checks"]], ["fail"])
        self.assertIn("service/nope-xyz not found in k8/default", svc["checks"][0]["detail"])
        self.assertFalse(svc["ready_for_launch"])

        for task_id in ("clean", "weekly"):
            self.assertEqual(tasks[task_id]["review"], "valid")
            self.assertEqual(tasks[task_id]["review_problems"], [])
            self.assertTrue(tasks[task_id]["ready_for_launch"], tasks[task_id])
        self.assertEqual(tasks["weekly"]["kind"], "recurring")

        summary = payload["summary"]
        self.assertEqual(summary["candidates"], 6)
        self.assertEqual(summary["missing_review"], 1)
        self.assertEqual(summary["failing_checks"], 1)
        # Whether a stale digest also counts as invalid is left to the implementation.
        self.assertIn(summary["invalid_review"], (1, 2))

    def test_stale_digest_through_public_problems(self) -> None:
        self.assertIn(STALE, self.queue.review_problems("stale"))
        self.assertEqual(self.queue.review_problems("clean"), [])

    def test_backfill_human_output_lists_problems(self) -> None:
        before = self.snapshot()
        code, payloads, out, _fake = self.backfill()

        self.assertEqual(code, 0, payloads)
        self.assertEqual(self.snapshot(), before)
        for task_id in ("migrated", "needs-svc", "clean", "stale", "invalid"):
            self.assertIn(task_id, out)
        self.assertNotIn("launched", out)
        self.assertNotIn("failed-once", out)
        self.assertIn("readiness review missing", out)


# --- 12: migration of a pre-review database ---------------------------------------------
def drop_v4(path: Path) -> None:
    """Reduce a freshly initialized queue to the v3 shape the previous release wrote."""

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        for column in ("grants_json", "readiness_review_json"):
            if column in columns:
                connection.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
        connection.execute("DELETE FROM schema_migrations WHERE version=4")


def schema_of(path: Path) -> tuple[set[int], set[str]]:
    with sqlite3.connect(path) as connection:
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    return versions, columns


class MigrationTests(ReviewCase):
    def test_initialize_adds_columns_and_version_4(self) -> None:
        self.add("legacy")
        drop_v4(self.queue.path)
        versions, columns = schema_of(self.queue.path)
        self.assertNotIn(4, versions)
        self.assertIn(3, versions)

        reopened = db.QueueDB(self.queue.path)
        reopened.initialize()

        versions, columns = schema_of(self.queue.path)
        self.assertIn(4, versions)
        self.assertTrue({"grants_json", "readiness_review_json"} <= columns, columns)
        legacy = reopened.task("legacy")
        self.assertEqual(legacy.grants, ())
        self.assertIsNone(legacy.readiness_review)
        self.assertEqual(legacy.to_dict()["grants"], [])
        self.assertIsNone(legacy.to_dict()["readiness_review"])
        self.assertEqual(reopened.review_problems("legacy"), ["readiness review missing"])


class InstallMigrationTests(unittest.TestCase):
    """Install migrates a pre-review queue before the new release becomes current."""

    def setUp(self) -> None:
        self.db, self.lifecycle = package_tests._runtime_modules()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.config_path = self.home / ".config" / "bonus-drain" / "config.json"
        self.database = self.home / ".local" / "state" / "bonus-drain" / "queue.db"
        environment = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("BONUS_DRAIN_CONFIG", None)

    def test_install_migrates_v3_queue_before_publishing(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        package_tests.BonusDrainPackageContractTests.installed_config(self, self.home, self.config_path)
        queue = self.db.QueueDB(self.database)
        queue.initialize()
        queue.add_task(reviewed({
            "id": "legacy", "title": "legacy", "kind": "oneoff", "cwd": str(self.root),
            "goal": "keep working after upgrade", "size": "small",
        }))
        drop_v4(self.database)
        self.assertNotIn(4, schema_of(self.database)[0])

        self.lifecycle.install(SKILL_ROOT, self.home, version="0.3.13+review-migrate-test")

        versions, columns = schema_of(self.database)
        self.assertIn(4, versions)
        self.assertTrue({"grants_json", "readiness_review_json"} <= columns, columns)
        self.assertEqual(
            os.readlink(self.home / ".local" / "lib" / "bonus-drain" / "current"), "0.3.13+review-migrate-test",
        )
        legacy = self.db.QueueDB(self.database).task("legacy")
        self.assertIsNone(legacy.readiness_review)


if __name__ == "__main__":
    unittest.main()
