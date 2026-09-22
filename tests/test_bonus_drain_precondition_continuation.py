"""Checkout setup is not a skip, and same-thread continuation is not a second launch."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tests.test_bonus_dependency_recovery import (
    KEY, NOW, RecoveryCase, captured_json, iso, reason, rows, runtime, verified,
)
from bonus_drain import cli, db, dispatcher, reconcile


def authority(detail: str = "the controlling decision is still draft") -> dict[str, object]:
    return {
        "reason": {
            "code": "authority_required",
            "detail": detail,
            "signature": "authority_required:draft-decision",
        }
    }


class PreconditionAuthoringTests(unittest.TestCase):
    def test_queueing_skill_rejects_setup_as_a_precondition(self) -> None:
        skill = (
            Path(__file__).resolve().parents[1]
            / "plugins/bonus-drain/skills/bonus-drain/SKILL.md"
        ).read_text(encoding="utf-8")
        guide = (
            Path(__file__).resolve().parents[1]
            / "plugins/bonus-drain/skills/bonus-drain/ASYNC_WORK.md"
        ).read_text(encoding="utf-8")
        self.assertIn("leave the precondition empty", skill)
        self.assertIn("Do not invent a checkout check", skill)
        self.assertIn("If it is false, the worker records\nskipped and stops", skill)
        self.assertIn("Leave it empty rather than requiring a clean checkout", guide)


class PreconditionPromptTests(RecoveryCase):
    def _prompt(self, task_id: str, *, kind: str = "oneoff", precondition: str | None = None) -> str:
        changes: dict[str, object] = {"kind": kind, "use_implement": True}
        if precondition is not None:
            changes["precondition"] = precondition
        if kind == "recurring":
            changes["cadence"] = "weekly"
        queued = self.add(task_id, **changes)
        attempt = self.claim(task_id)
        return dispatcher.render_prompt(
            runtime(self.queue.path), queued, KEY, "alpha", "alpha-account",
            attempt=attempt, outcome_path=self.root / f"{task_id}-outcome.json",
        )

    def test_clean_checkout_precondition_is_setup_not_an_immediate_skip(self) -> None:
        prompt = self._prompt(
            "checkout",
            precondition="origin/next checked out clean; plugin-format and CLI tests run locally.",
        )

        self.assertIn("origin/next checked out clean", prompt)
        self.assertIn("not an unmet precondition", prompt)
        self.assertIn("Create your own clean worktree from the named remote base", prompt)
        self.assertIn("occupied default ports", prompt)
        self.assertIn("shared baseline lock", prompt)
        self.assertIn("missing local dependency", prompt)
        self.assertIn("another owner already editing the same paths", prompt)
        self.assertIn("frozen contract", prompt)
        self.assertIn("unavailable provider", prompt)
        self.assertIn("missing authority", prompt)
        self.assertIn("validation gate", prompt)
        self.assertNotIn("If it is unmet, record skipped immediately", prompt)

    def test_recurring_prompt_uses_the_same_setup_rule_without_continuation(self) -> None:
        prompt = self._prompt("weekly", kind="recurring")

        self.assertIn(dispatcher.PRECONDITION_EXECUTION_RULE, prompt)
        self.assertNotIn("continue-progress", prompt)
        self.assertNotIn("recover-complete", prompt)


class ContinuationTests(RecoveryCase):
    def skip_with_job(self, task_id: str, outcome: dict[str, object] | None = None):
        attempt = self.claim(task_id)
        self.queue.record(
            task_id, KEY, attempt_id=attempt.id, status="dispatched",
            provider_id="alpha", account_id="alpha-account",
            router_job_id=f"job-{task_id}",
            timestamp=iso(NOW), now_epoch=NOW,
        )
        self.terminal(task_id, attempt, "skipped", outcome or reason(), now=NOW + 1)
        return attempt

    def source_row(self, attempt_id: str) -> dict[str, object]:
        found = rows(
            self.queue,
            "SELECT id,state,outcome_json,terminal_at FROM task_attempts WHERE id=?",
            (attempt_id,),
        )
        self.assertEqual(len(found), 1)
        return found[0]

    def test_continuation_shows_the_same_job_running_and_dispatch_does_not_launch(self) -> None:
        self.add("resume")
        self.add("child", depends_on=["resume"])
        source = self.skip_with_job("resume")
        before = self.source_row(source.id)
        self.queue.reconcile_recoveries(now_epoch=NOW + 2, dry_run=False)
        recovery = self.queue.recovery_for("resume")
        self.assertIsNotNone(recovery)
        self.assertIn(recovery.state, {"scheduled", "backoff"})

        with mock.patch("bonus_drain.adapters.execute_adapter") as adapter:
            opened = self.queue.open_same_thread_continuation(
                "resume", expected_attempt_id=source.id, now_epoch=NOW + 3,
            )
        adapter.assert_not_called()

        self.assertEqual(opened["router_job_id"], "job-resume")
        self.assertFalse(opened["idempotent"])
        self.assertEqual(self.source_row(source.id), before)
        status = self.queue.readiness("resume", now_epoch=NOW + 3)
        self.assertEqual(status["state"], "running")
        self.assertEqual(
            [run.router_job_id for run in self.queue.inflight()],
            ["job-resume"],
        )
        self.assertEqual(self.queue.recovery_for("resume").state, "consumed")
        self.assertEqual(
            self.queue.recovery_for("resume").consumed_by_attempt_id,
            opened["attempt_id"],
        )
        self.assertNotIn(
            "resume",
            [
                task.id for task in self.queue.eligible_tasks(
                    0, provider_id="alpha", now_epoch=NOW + 100_000,
                )
            ],
        )
        router = mock.Mock()
        with self.assertRaises(dispatcher.AlreadyClaimed):
            dispatcher.dispatch(
                runtime(self.queue.path), self.queue,
                task_id="resume", eligibility_key=KEY,
                requested_provider="alpha", router_call=router,
                now_epoch=NOW + 100_000,
            )
        router.assert_not_called()
        self.assertIsNone(self.queue.claim(
            "resume", KEY, "alpha", "alpha-account", now_epoch=NOW + 100_000,
        ))

        again = self.queue.open_same_thread_continuation(
            "resume", expected_attempt_id=source.id, now_epoch=NOW + 4,
        )
        self.assertEqual(again["attempt_id"], opened["attempt_id"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.queue.attempts(task_id="resume")), 2)

        self.queue.record(
            "resume", KEY, attempt_id=opened["attempt_id"], status="done",
            outcome=verified(evidence="fixture://continued"),
            provider_id="alpha", account_id="alpha-account",
            timestamp=iso(NOW + 5), now_epoch=NOW + 5,
            summary="continued work verified the checkout in its own worktree",
        )
        self.assertEqual(self.queue.readiness("resume", now_epoch=NOW + 5)["state"], "done")
        self.assertIsNone(self.queue.recovery_for("resume"))
        self.assertEqual(self.source_row(source.id)["state"], "skipped")
        self.assertEqual(self.queue.claims(), [])
        child = self.queue.readiness("child", now_epoch=NOW + 5)
        self.assertTrue(all(item["satisfied"] for item in child["dependencies"]), child)

    def test_cli_continuation_is_idempotent_and_preserves_the_skipped_attempt(self) -> None:
        self.add("cli-resume")
        source = self.skip_with_job("cli-resume")
        before = self.source_row(source.id)

        with captured_json() as payloads:
            code = cli.main([
                "continue-progress", "--database", str(self.queue.path),
                "--task", "cli-resume", "--from-attempt", source.id, "--json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(payloads[0]["router_job_id"], "job-cli-resume")
        self.assertFalse(payloads[0]["idempotent"])
        opened_attempt = payloads[0]["attempt_id"]

        with captured_json() as payloads:
            code = cli.main([
                "continue-progress", "--database", str(self.queue.path),
                "--task", "cli-resume", "--from-attempt", source.id, "--json",
            ])
        self.assertEqual(code, 0)
        self.assertTrue(payloads[0]["idempotent"])
        self.assertEqual(payloads[0]["attempt_id"], opened_attempt)
        self.assertEqual(self.source_row(source.id), before)
        self.assertEqual(len(self.queue.attempts(task_id="cli-resume")), 2)

    def test_missing_router_job_does_not_invent_a_running_claim(self) -> None:
        self.add("unlaunched")
        attempt = self.claim("unlaunched")
        self.terminal("unlaunched", attempt, "skipped", reason(), now=NOW + 1)

        with self.assertRaisesRegex(db.QueueError, "original router job"):
            self.queue.open_same_thread_continuation(
                "unlaunched", expected_attempt_id=attempt.id, now_epoch=NOW + 2,
            )
        self.assertEqual(len(self.queue.attempts(task_id="unlaunched")), 1)
        self.assertEqual(self.queue.claims(), [])
        self.assertEqual(
            self.queue.readiness("unlaunched", now_epoch=NOW + 2)["state"], "skipped",
        )

    def test_held_authority_recovery_refuses_continuation(self) -> None:
        self.add("blocked")
        self.add("child", depends_on=["blocked"])
        source = self.skip_with_job("blocked", authority())
        decisions = self.queue.reconcile_recoveries(now_epoch=NOW + 2, dry_run=False)
        held = [item for item in decisions if item.task_id == "blocked"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].state, "held")
        before = self.source_row(source.id)

        with self.assertRaisesRegex(db.QueueError, "held"):
            self.queue.open_same_thread_continuation(
                "blocked", expected_attempt_id=source.id, now_epoch=NOW + 3,
            )
        self.assertEqual(self.source_row(source.id), before)
        self.assertEqual(len(self.queue.attempts(task_id="blocked")), 1)
        self.assertEqual(self.queue.claims(), [])
        self.assertEqual(self.queue.recovery_for("blocked").state, "held")

    def test_authority_skip_is_refused_before_scout_writes_a_hold(self) -> None:
        for code in ("authority_required", "permanent", "unknown_launch"):
            task_id = f"hold-{code}"
            self.add(task_id)
            source = self.skip_with_job(task_id, {
                "reason": {
                    "code": code,
                    "detail": f"fixture {code} blocks continuation",
                    "signature": f"{code}:fixture",
                },
            })
            before = self.source_row(source.id)
            with self.assertRaisesRegex(db.QueueError, code):
                self.queue.open_same_thread_continuation(
                    task_id, expected_attempt_id=source.id, now_epoch=NOW + 3,
                )
            self.assertEqual(self.source_row(source.id), before)
            self.assertEqual(len(self.queue.attempts(task_id=task_id)), 1)
            self.assertIsNone(self.queue.recovery_for(task_id))
        self.assertEqual(self.queue.claims(), [])

    def test_terminal_continuation_drops_the_consumed_recovery_projection(self) -> None:
        self.add("parked")
        self.add("child", depends_on=["parked"])
        source = self.skip_with_job("parked")
        self.queue.reconcile_recoveries(now_epoch=NOW + 2, dry_run=False)
        opened = self.queue.open_same_thread_continuation(
            "parked", expected_attempt_id=source.id, now_epoch=NOW + 3,
        )
        self.assertEqual(self.queue.recovery_for("parked").state, "consumed")
        self.queue.record(
            "parked", KEY, attempt_id=opened["attempt_id"], status="awaiting_human",
            outcome=authority("need an explicit approval before continuing"),
            provider_id="alpha", account_id="alpha-account",
            timestamp=iso(NOW + 4), now_epoch=NOW + 4,
            summary="continuation parked for approval",
        )
        self.assertIsNone(self.queue.recovery_for("parked"))
        child = self.queue.readiness("child", now_epoch=NOW + 4)
        parent = next(item for item in child["dependencies"] if item["id"] == "parked")
        self.assertEqual(parent["status"], "awaiting_human")
        self.assertNotEqual(parent["status"], "recovering")
        resumed = self.queue.open_same_thread_continuation(
            "parked", expected_attempt_id=opened["attempt_id"], now_epoch=NOW + 5,
        )
        self.assertNotEqual(resumed["attempt_id"], opened["attempt_id"])
        self.assertFalse(resumed["idempotent"])
        self.assertEqual(self.queue.readiness("parked", now_epoch=NOW + 5)["state"], "running")
        self.assertEqual(self.source_row(source.id)["state"], "skipped")

    def test_reconcile_holds_a_live_continuation_and_keeps_the_original_skip(self) -> None:
        self.add("live")
        source = self.skip_with_job("live")
        before = self.source_row(source.id)
        opened = self.queue.open_same_thread_continuation(
            "live", expected_attempt_id=source.id, now_epoch=NOW + 3,
        )
        with mock.patch(
            "bonus_drain.reconcile.execute_adapter",
            return_value={"rows": [{
                "provider": "alpha", "job_id": "job-live", "state": "running",
            }]},
        ):
            reports = reconcile.reconcile_inflight(
                runtime(self.queue.path), self.queue, now_epoch=NOW + 4,
            )
        self.assertEqual(reports[0]["action"], "held")
        self.assertIn("running", reports[0]["reason"])
        self.assertEqual(self.queue.claim_for("live").attempt_id, opened["attempt_id"])
        self.assertEqual(self.source_row(source.id), before)

        with mock.patch(
            "bonus_drain.reconcile.execute_adapter",
            return_value={"rows": [{
                "provider": "alpha", "job_id": "job-live", "state": "completed",
            }]},
        ):
            reports = reconcile.reconcile_inflight(
                runtime(self.queue.path), self.queue, now_epoch=NOW + 5,
            )
        self.assertEqual(reports[0]["action"], "held")
        self.assertIn("continuation liveness is not proven", reports[0]["reason"])
        states = {
            attempt.id: attempt.state for attempt in self.queue.attempts(task_id="live")
        }
        self.assertEqual(states[source.id], "skipped")
        self.assertEqual(states[opened["attempt_id"]], "dispatched")
        self.assertEqual(self.queue.claim_for("live").attempt_id, opened["attempt_id"])


if __name__ == "__main__":
    unittest.main()
