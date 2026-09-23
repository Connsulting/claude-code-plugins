"""Dependency branch choice, queue ownership, and local dispatch proof."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import config as config_module
from bonus_drain import cli, db, dispatcher, handoff, scout, usage

NOW = 2_000_000_000
BRANCH_FIELDS = {"branch_ref", "parent_ids"}


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def task(task_id: str, cwd: Path, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": task_id,
        "title": task_id,
        "kind": "oneoff",
        "priority": 2,
        "cwd": str(cwd),
        "goal": f"complete {task_id}",
        "done_when": f"proof for {task_id} is retained",
        "created_at": iso(NOW - 60),
        "active": True,
        "size": "small",
    }
    value.update(changes)
    return value


def repository(
    branch: str, *, state: str = "unmerged", target: str = "main",
    remote: str = "https://github.com/example/repository.git",
) -> dict[str, object]:
    return {
        "remote": remote,
        "target_ref": f"refs/heads/{target}",
        "branch_ref": f"refs/heads/{branch}",
        "integration_state": state,
        "target_base_oid": "a" * 40,
        "head_oid": "b" * 40,
    }


def verified_outcome(repo: dict[str, object] | None = None) -> dict[str, object]:
    outcome: dict[str, object] = {
        "reason": {
            "code": "done_when_verified",
            "detail": "the done when was checked",
            "signature": "done_when_verified:fixture",
        },
        "completion": {
            "verified": True,
            "mechanism": "command",
            "evidence": ["fixture://proof"],
        },
    }
    if repo is not None:
        outcome["repository"] = repo
    return outcome


def runtime(database: Path) -> config_module.RuntimeConfig:
    return config_module.RuntimeConfig(
        schema_version=1,
        source_path=None,
        database=database,
        record_command=("/bin/true", "record"),
        secret_refs=(),
        adapters=(config_module.AdapterConfig(
            "router", "agent-router", ("/bin/true",),
            timeout_seconds=10, max_output_bytes=65_536,
        ),),
        providers=(config_module.ProviderConfig(
            "alpha", config_module.DispatchBinding("router", "alpha"),
            frozenset(), "single",
        ),),
        plans=(config_module.PlanConfig("alpha-plan", "alpha"),),
        accounts=(config_module.AccountConfig(
            "alpha-account", "alpha", "alpha-plan",
        ),),
        limits=(config_module.LimitConfig(
            "alpha-weekly", "alpha-plan", 604_800, 95, 20_000, 6,
        ),),
        viewer={},
        pr_exceptions=(),
        usage_max_age_seconds=3_600,
        cache_dir=database.parent / "cache",
    )


class SimpleBranchResolutionContract(unittest.TestCase):
    def resolve(
        self, outcomes: list[tuple[str, dict[str, object]]],
        start_ref: str | None = None,
    ) -> dict[str, object] | None:
        with mock.patch.object(
            subprocess, "run", side_effect=AssertionError("Git called"),
        ):
            return handoff.resolve_dependency_base(outcomes, start_ref=start_ref)

    def test_explicit_start_ref_is_a_pure_branch_choice(self) -> None:
        outcomes = [("parent", {"repository": repository("task/parent")})]
        self.assertEqual(
            self.resolve(outcomes, "refs/heads/next"),
            {"branch_ref": "refs/heads/next", "parent_ids": ["parent"]},
        )
        self.assertEqual(
            self.resolve(outcomes, "refs/heads/epic/next"),
            {"branch_ref": "refs/heads/epic/next", "parent_ids": ["parent"]},
        )

    def test_explicit_start_without_repository_evidence_and_no_start_without_evidence(self) -> None:
        self.assertIsNone(self.resolve([]))
        self.assertIsNone(self.resolve([("parent", verified_outcome())]))
        self.assertEqual(
            self.resolve([("parent", verified_outcome())], "refs/heads/next"),
            {"branch_ref": "refs/heads/next", "parent_ids": []},
        )

    def test_one_unmerged_branch_wins_and_merged_parents_select_common_target(self) -> None:
        outcomes = [
            ("merged", {"repository": repository("task/merged", state="merged")}),
            ("open", {"repository": repository("task/open")}),
        ]
        self.assertEqual(
            self.resolve(outcomes),
            {"branch_ref": "refs/heads/task/open", "parent_ids": ["merged", "open"]},
        )
        outcomes[1][1]["repository"] = repository("task/open", state="merged")
        self.assertEqual(
            self.resolve(outcomes),
            {"branch_ref": "refs/heads/main", "parent_ids": ["merged", "open"]},
        )

    def test_distinct_unmerged_branches_require_explicit_start_even_if_commits_match(self) -> None:
        outcomes = [
            ("a", {"repository": repository("task/a")}),
            ("b", {"repository": repository("task/b")}),
        ]
        with self.assertRaises(handoff.DependencyHandoffError) as raised:
            self.resolve(outcomes)
        self.assertEqual(raised.exception.reason_code, "integration_required")
        self.assertIn("start_ref", raised.exception.detail)
        self.assertEqual(
            self.resolve(outcomes, "refs/heads/epic/combined"),
            {"branch_ref": "refs/heads/epic/combined", "parent_ids": ["a", "b"]},
        )

    def test_automatic_choice_requires_common_repository_and_target(self) -> None:
        equivalent = [
            ("ssh", {"repository": repository(
                "task/a", state="merged", remote="git@github.com:example/repository.git",
            )}),
            ("https", {"repository": repository("task/b", state="merged")}),
        ]
        self.assertEqual(self.resolve(equivalent), {
            "branch_ref": "refs/heads/main", "parent_ids": ["ssh", "https"],
        })
        for changed in (
            repository("task/b", remote="https://github.com/example/other.git"),
            repository("task/b", target="next"),
        ):
            with self.subTest(changed=changed):
                outcomes = [
                    ("a", {"repository": repository("task/a", state="merged")}),
                    ("b", {"repository": changed | {"integration_state": "merged"}}),
                ]
                with self.assertRaises(handoff.DependencyHandoffError):
                    self.resolve(outcomes)

    def test_explicit_choice_still_rejects_malformed_parent_metadata(self) -> None:
        for field, value in (
            ("remote", ""),
            ("target_ref", "main"),
            ("branch_ref", "refs/tags/v1"),
            ("integration_state", "unknown"),
        ):
            with self.subTest(field=field):
                bad = repository("task/parent")
                bad[field] = value
                with self.assertRaises(handoff.DependencyHandoffError):
                    self.resolve([("parent", {"repository": bad})], "refs/heads/next")

    def test_obsolete_sha_and_receipt_are_not_git_proof_gates(self) -> None:
        evidence = repository("task/parent")
        evidence.update({
            "head_oid": "obsolete",
            "target_base_oid": "obsolete",
            "merge_receipt": {"kind": "squash", "result_oid": "obsolete"},
        })
        self.assertEqual(
            self.resolve([("parent", {"repository": evidence})]),
            {"branch_ref": "refs/heads/task/parent", "parent_ids": ["parent"]},
        )


class QueueBranchChoiceContract(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "queue.db")
        self.queue.initialize()

    def add(self, task_id: str, **changes: object) -> db.Task:
        return self.queue.add_task(task(task_id, self.root, **changes))

    def complete(self, task_id: str, repo: dict[str, object] | None = None) -> None:
        attempt = self.queue.claim(
            task_id, f"manual/{task_id}", "alpha", "alpha-account", now_epoch=NOW,
        )
        self.assertIsNotNone(attempt)
        self.queue.record(
            task_id, f"manual/{task_id}", attempt_id=attempt.id,
            status="done", outcome=verified_outcome(repo),
            provider_id="alpha", account_id="alpha-account",
            timestamp=iso(NOW), now_epoch=NOW, summary="verified",
        )

    def prompt_branch(self, prompt: str) -> dict[str, object]:
        match = re.search(r"(?m)^DEPENDENCY_BASE(?:=|\s+)(\{[^\n]+\})$", prompt)
        self.assertIsNotNone(match, prompt)
        value = json.loads(match.group(1))
        self.assertEqual(set(value), BRANCH_FIELDS)
        return value

    def test_incomplete_parent_waits_and_done_parent_needs_no_git_checkout(self) -> None:
        self.add("parent")
        self.add("child", depends_on=["parent"])
        self.assertFalse(self.queue.readiness("child", now_epoch=NOW)["ready"])
        self.assertIsNone(self.queue.claim(
            "child", "manual/child", "alpha", "alpha-account", now_epoch=NOW,
        ))
        self.complete("parent", repository("task/parent"))
        with mock.patch.object(
            subprocess, "run", side_effect=AssertionError("Git called"),
        ):
            ready = self.queue.readiness("child", now_epoch=NOW)
            self.assertTrue(ready["ready"], ready)
            self.assertEqual(
                self.queue.dependency_base("child"),
                {"branch_ref": "refs/heads/task/parent", "parent_ids": ["parent"]},
            )

    def test_many_parent_explicit_branch_is_shared_by_view_scout_and_dispatch(self) -> None:
        for name in ("a", "b"):
            self.add(name)
            self.complete(name, repository(f"task/{name}"))
        self.add("child", depends_on=["a", "b"], start_ref="epic/next")
        selected = {"branch_ref": "refs/heads/epic/next", "parent_ids": ["a", "b"]}
        self.assertEqual(self.queue.task("child").start_ref, "refs/heads/epic/next")
        self.assertEqual(self.queue.readiness("child", now_epoch=NOW)["dependency_base"], selected)
        self.assertEqual(self.queue.snapshot(now_epoch=NOW)["readiness"]["child"]["dependency_base"], selected)
        snapshots = {
            ("alpha", "alpha-account"): usage.UsageSnapshot(
                "alpha", "alpha-account", NOW,
                {"alpha-weekly": {"used_percent": 70, "resets_at": NOW + 1_000}},
            ),
        }
        with mock.patch.object(scout, "read_all", return_value=snapshots):
            tick = scout.plan_tick(
                runtime(self.queue.path), self.queue, now_epoch=NOW, provider_holds=(),
            )
        self.assertIn("child", [
            item.id for tasks in tick.allocations.values() for item in tasks
        ])
        router = mock.Mock(return_value={
            "provider": "alpha",
            "dispatch": {"job_id": "local-child", "launched": True},
        })
        result = dispatcher.dispatch(
            runtime(self.queue.path), self.queue,
            task_id="child", eligibility_key="manual/child",
            requested_provider="alpha", router_call=router,
        )
        self.assertEqual(self.prompt_branch(result.prompt), selected)
        self.assertIn("refs/heads/epic/next", result.prompt)
        self.assertIn("current tip", result.prompt)
        self.assertIn("verification_needed", result.prompt)
        self.assertIn("Do not substitute another branch", result.prompt)
        self.assertIn("grants no merge authority", result.prompt)
        router.assert_called_once()

    def test_ambiguous_child_is_held_before_router_and_scout_allocates_other_work(self) -> None:
        for name in ("a", "b"):
            self.add(name)
            self.complete(name, repository(f"task/{name}"))
        self.add("child", depends_on=["a", "b"], priority=1)
        self.add("independent", priority=3)
        readiness = self.queue.readiness("child", now_epoch=NOW)
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["hold_reason"], "integration_required")
        self.assertIn("start_ref", readiness["reason"])
        router = mock.Mock()
        with self.assertRaises(dispatcher.DispatchError):
            dispatcher.dispatch(
                runtime(self.queue.path), self.queue,
                task_id="child", eligibility_key="manual/child",
                requested_provider="alpha", router_call=router,
            )
        router.assert_not_called()
        snapshots = {
            ("alpha", "alpha-account"): usage.UsageSnapshot(
                "alpha", "alpha-account", NOW,
                {"alpha-weekly": {"used_percent": 70, "resets_at": NOW + 1_000}},
            ),
        }
        with mock.patch.object(scout, "read_all", return_value=snapshots):
            tick = scout.plan_tick(
                runtime(self.queue.path), self.queue, now_epoch=NOW, provider_holds=(),
            )
        self.assertEqual(
            [item.id for tasks in tick.allocations.values() for item in tasks],
            ["independent"],
        )
        self.assertEqual(tick.dependency_holds[0]["hold_reason"], "integration_required")

    def test_read_only_local_queue_reports_the_same_held_decision(self) -> None:
        for name in ("a", "b"):
            self.add(name)
            self.complete(name, repository(f"task/{name}"))
        self.add("child", depends_on=["a", "b"])
        strict = self.queue.readiness("child", now_epoch=NOW)
        local = db.LocalQueueReader(self.queue.path)
        self.assertEqual(local.readiness("child", now_epoch=NOW), strict)
        with self.assertRaises(sqlite3.OperationalError):
            with local._connect() as connection:
                connection.execute("UPDATE tasks SET title='changed' WHERE id='child'")
        self.assertEqual(self.queue.task("child").title, "child")
        with mock.patch.object(cli, "_json") as output:
            self.assertEqual(cli.main([
                "queue", "--database", str(self.queue.path), "--local", "--now", str(NOW), "--json",
            ]), 0)
        self.assertEqual(output.call_args.args[0]["readiness"]["child"], strict)

    def test_failed_middle_task_recovers_then_hands_branch_to_next_task(self) -> None:
        self.add("a")
        self.add("b", depends_on=["a"])
        self.add("c", depends_on=["b"])
        self.complete("a", repository("task/a"))
        routed: list[str] = []

        def router_call(*_args: object, **_kwargs: object) -> dict[str, object]:
            routed.append("launched")
            return {"provider": "alpha", "dispatch": {
                "job_id": f"local-job-{len(routed)}", "launched": True,
            }}

        first = dispatcher.dispatch(
            runtime(self.queue.path), self.queue,
            task_id="b", eligibility_key="manual/b",
            requested_provider="alpha", router_call=router_call,
        )
        self.assertEqual(self.prompt_branch(first.prompt)["branch_ref"], "refs/heads/task/a")
        first_claim = self.queue.claim_for("b")
        self.assertIsNotNone(first_claim)
        self.queue.record(
            "b", first.eligibility_key, attempt_id=first_claim.attempt_id,
            status="failed", outcome={"reason": {
                "code": "retryable", "detail": "tests failed", "signature": "tests:failed",
            }}, provider_id="alpha", account_id="alpha-account",
            timestamp=iso(NOW), now_epoch=NOW, summary="tests failed",
        )
        self.assertFalse(self.queue.readiness("c", now_epoch=NOW)["ready"])

        snapshots = {("alpha", "alpha-account"): usage.UsageSnapshot(
            "alpha", "alpha-account", NOW + 300,
            {"alpha-weekly": {"used_percent": 20, "resets_at": NOW + 1_000}},
        )}
        with mock.patch.object(scout, "read_all", return_value=snapshots):
            recovered = scout.run_once(
                runtime(self.queue.path), self.queue, now_epoch=NOW + 300,
                router_call=router_call,
            )
        self.assertEqual([item.task_id for item in recovered.dispatched], ["b"])
        self.assertEqual(
            self.prompt_branch(recovered.dispatched[0].prompt)["branch_ref"],
            "refs/heads/task/a",
        )
        retry_claim = self.queue.claim_for("b")
        self.assertIsNotNone(retry_claim)
        self.assertNotEqual(retry_claim.attempt_id, first_claim.attempt_id)
        retry = recovered.dispatched[0]
        self.queue.record(
            "b", retry.eligibility_key, attempt_id=retry_claim.attempt_id,
            status="done", outcome=verified_outcome(repository("task/b")),
            provider_id="alpha", account_id="alpha-account",
            timestamp=iso(NOW + 301), now_epoch=NOW + 301,
            summary="recovery verified",
        )
        with mock.patch.object(scout, "read_all", return_value=snapshots):
            successor = scout.run_once(
                runtime(self.queue.path), self.queue, now_epoch=NOW + 301,
                router_call=router_call,
            )
        self.assertEqual([item.task_id for item in successor.dispatched], ["c"])
        self.assertEqual(
            self.prompt_branch(successor.dispatched[0].prompt)["branch_ref"],
            "refs/heads/task/b",
        )
        self.assertEqual(
            [row.state for row in self.queue.attempts(task_id="b")],
            ["failed", "done"],
        )

    def test_reverify_preserves_done_outcome_and_uses_latest_structural_claim(self) -> None:
        self.add("parent")
        self.complete("parent", repository("task/old"))
        source = self.queue.runs(task_id="parent")[-1]
        original = source.outcome
        self.add("child", depends_on=["parent"])
        with self.assertRaisesRegex(db.QueueError, "repository identity"):
            self.queue.reverify_handoff(
                "parent", from_done_rowid=source.rowid_pk, after_revision_id=None,
                outcome=verified_outcome(repository("task/other")),
                summary="branch redirected",
            )
        replacement = repository("task/old")
        replacement["head_oid"] = "changed without Git access"
        revision = self.queue.reverify_handoff(
            "parent", from_done_rowid=source.rowid_pk, after_revision_id=None,
            outcome=verified_outcome(replacement), summary="new evidence",
        )
        self.assertEqual(self.queue.runs(task_id="parent")[-1].outcome, original)
        self.assertEqual(
            self.queue.handoff_revision("parent")["after_revision_id"], revision["id"],
        )
        with self.assertRaises(db.QueueError):
            self.queue.reverify_handoff(
                "parent", from_done_rowid=source.rowid_pk, after_revision_id=None,
                outcome=verified_outcome(replacement), summary="stale revision",
            )
        self.assertEqual(
            self.queue.dependency_base("child"),
            {"branch_ref": "refs/heads/task/old", "parent_ids": ["parent"]},
        )

    def test_newer_done_row_cannot_be_overridden_by_old_revision(self) -> None:
        self.add("parent")
        self.complete("parent", repository("task/parent"))
        source = self.queue.runs(task_id="parent")[-1]
        self.queue.reverify_handoff(
            "parent", from_done_rowid=source.rowid_pk, after_revision_id=None,
            outcome=verified_outcome(repository("task/parent")), summary="revision",
        )
        self.add("child", depends_on=["parent"])
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute(
                """INSERT INTO runs(task,kind,cycle,status,ts,attempt_id,outcome_json)
                   SELECT task,kind,cycle,status,ts,attempt_id,outcome_json
                     FROM runs WHERE rowid_pk=?""",
                (source.rowid_pk,),
            )
        with self.assertRaisesRegex(db.QueueError, "older done row"):
            self.queue.dependency_base("child")


if __name__ == "__main__":
    unittest.main()
