"""awaiting_human is a terminal status that parks work for Brian without automatic recovery."""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import cli, db, dispatcher, factory_terminal  # noqa: E402

from tests.test_bonus_dependency_recovery import (  # noqa: E402
    KEY, NOW, RecoveryCase, captured_json, iso, rows, runtime, task, verified,
)
from tests.test_bonus_drain_jobs_viewer import _load_server  # noqa: E402

DETAIL = "Approve the CI workflow diff in PR 42 before it is committed"
PR_URL = "https://github.com/example/repo/pull/42"


def awaiting(detail: str = DETAIL, code: str = "authority_required") -> dict[str, object]:
    return {"reason": {"code": code, "detail": detail, "signature": f"{code}:approval"}}


class AwaitingHumanCase(RecoveryCase):
    def park(self, task_id: str, *, outcome: dict[str, object] | None = None, now: int = NOW):
        attempt = self.claim(task_id, now=now)
        self.queue.record(
            task_id, KEY, attempt_id=attempt.id, status="dispatched",
            provider_id="alpha", account_id="alpha-account", router_job_id=f"job-{task_id}",
            timestamp=iso(now), now_epoch=now,
        )
        self.terminal(task_id, attempt, "awaiting_human", outcome or awaiting(), now=now + 1)
        return attempt


class RecordTests(AwaitingHumanCase):
    def test_record_accepts_awaiting_human_and_runs_json_shows_it(self) -> None:
        self.add("parked")
        attempt = self.park("parked")

        self.assertEqual(self.queue.attempts(task_id="parked")[0].state, "awaiting_human")
        self.assertEqual(self.queue.claims(), [])
        with captured_json() as payloads:
            code = cli.main([
                "runs", "--database", str(self.queue.path), "--task", "parked", "--json",
            ])
        self.assertEqual(code, 0)
        statuses = [run["status"] for run in payloads[0]["runs"]]
        self.assertIn("awaiting_human", statuses)
        stored = rows(self.queue, "SELECT status FROM runs WHERE task='parked' ORDER BY rowid_pk")
        self.assertEqual(stored[-1]["status"], "awaiting_human")
        self.assertEqual(self.queue.attempts(task_id="parked")[0].id, attempt.id)

    def test_identical_repeat_is_idempotent_and_a_different_terminal_is_rejected(self) -> None:
        self.add("parked")
        attempt = self.park("parked")
        before = len(rows(self.queue, "SELECT * FROM runs WHERE task='parked'"))

        replay = self.terminal("parked", attempt, "awaiting_human", awaiting(), now=NOW + 1)
        self.assertEqual(replay.attempt_id, attempt.id)
        self.assertEqual(len(rows(self.queue, "SELECT * FROM runs WHERE task='parked'")), before)

        with self.assertRaisesRegex(db.QueueError, "conflict|already recorded"):
            self.terminal("parked", attempt, "failed", awaiting(code="retryable"), now=NOW + 2)
        self.assertEqual(self.queue.attempts(task_id="parked")[0].state, "awaiting_human")


class ValidationTests(AwaitingHumanCase):
    def _reject(self, outcome: dict[str, object] | None, pattern: str) -> None:
        self.add("bad")
        attempt = self.claim("bad")
        with self.assertRaisesRegex(db.QueueError, pattern):
            self.queue.record(
                "bad", KEY, attempt_id=attempt.id, status="awaiting_human", outcome=outcome,
                provider_id="alpha", account_id="alpha-account", timestamp=iso(NOW),
                now_epoch=NOW, summary="needs Brian",
            )
        # The claim survives a rejected terminal.
        self.assertEqual(self.queue.claim_for("bad").attempt_id, attempt.id)
        self.assertNotIn(
            "awaiting_human", [row["status"] for row in rows(self.queue, "SELECT status FROM runs")],
        )

    def test_missing_outcome_is_rejected(self) -> None:
        self._reject(None, "requires|outcome|reason")

    def test_empty_detail_is_rejected(self) -> None:
        self._reject(awaiting(detail="   "), "detail")

    def test_missing_detail_is_rejected(self) -> None:
        self._reject({"reason": {"code": "authority_required"}}, "detail")

    def test_verified_completion_is_rejected(self) -> None:
        outcome = awaiting()
        outcome["completion"] = {"verified": True, "mechanism": "artifact", "evidence": [PR_URL]}
        self._reject(outcome, "verified")

    def test_done_when_verified_code_is_rejected(self) -> None:
        self._reject(awaiting(code="done_when_verified"), "done_when_verified|reason|code")

    def test_validate_outcome_accepts_awaiting_human_directly(self) -> None:
        value = db.validate_outcome("awaiting_human", awaiting())
        self.assertEqual(value["reason"]["detail"], DETAIL)
        self.assertIn("awaiting_human", db.TERMINAL_STATUSES)
        self.assertIn("awaiting_human", db.VALID_STATUSES)
        self.assertIn("awaiting_human", db.ATTEMPT_STATES)


class ReadinessTests(AwaitingHumanCase):
    def test_readiness_parks_for_brian_without_requeue(self) -> None:
        self.add("parked")
        self.park("parked")

        status = self.queue.readiness("parked", now_epoch=NOW + 10)
        self.assertEqual(status["state"], "awaiting_human")
        self.assertFalse(status["ready"])
        self.assertIn("Awaiting Brian", status["reason"])
        self.assertIn(DETAIL, status["reason"])
        # Parked work is never requeued automatically, but Brian may continue it himself.
        self.assertTrue(status["requeue"]["allowed"])

    def test_no_automatic_recovery_and_dependent_keeps_waiting(self) -> None:
        self.add("parent")
        self.add("child", depends_on=["parent"])
        self.park("parent")

        decisions = self.queue.reconcile_recoveries(now_epoch=NOW + 10, dry_run=False)
        self.assertEqual([d for d in decisions if d.task_id == "parent"], [])
        self.assertIsNone(self.queue.recovery_for("parent"))
        self.assertEqual(self.queue.readiness("parent", now_epoch=NOW + 10)["state"], "awaiting_human")

        child = self.queue.readiness("child", now_epoch=NOW + 10)
        self.assertFalse(child["ready"])
        self.assertEqual(child["state"], "waiting")
        parent_dep = [item for item in child["dependencies"] if item["id"] == "parent"]
        self.assertEqual(len(parent_dep), 1)
        self.assertFalse(parent_dep[0]["satisfied"])


class OperatorRecoveryTests(AwaitingHumanCase):
    def test_operator_requeue_continues_awaiting_human_despite_authority_code(self) -> None:
        self.add("parked")
        source = self.park("parked")

        decision = self.queue.requeue("parked", attempt_id=source.id, now_epoch=NOW + 10)
        self.assertIn(decision.state, {"scheduled", "backoff"})
        recovery = self.queue.recovery_for("parked")
        self.assertIsNotNone(recovery)
        self.assertEqual(recovery.origin, "operator")
        status = self.queue.readiness("parked", now_epoch=NOW + 10_000)
        self.assertTrue(status["ready"], status)

    def test_recover_complete_from_awaiting_human_satisfies_dependents(self) -> None:
        self.add("parent")
        self.add("child", depends_on=["parent"])
        source = self.park("parent")

        completed = self.queue.recover_complete(
            "parent", expected_attempt_id=source.id,
            outcome=verified(evidence="fixture://brian-approved"),
            summary="Brian approved and the work was committed", now_epoch=NOW + 10,
        )
        self.assertEqual(completed.status, "done")
        self.assertNotEqual(completed.attempt_id, source.id)
        child = self.queue.readiness("child", now_epoch=NOW + 20)
        self.assertTrue(all(item["satisfied"] for item in child["dependencies"]), child)

    def test_automatic_recovery_still_skips_awaiting_human(self) -> None:
        self.add("parent")
        self.add("child", depends_on=["parent"])
        self.park("parent")
        for offset in (10, 5_000, 50_000):
            decisions = self.queue.reconcile_recoveries(now_epoch=NOW + offset, dry_run=False)
            self.assertEqual([d for d in decisions if d.task_id == "parent"], [])
        self.assertIsNone(self.queue.recovery_for("parent"))


class InflightTests(AwaitingHumanCase):
    def test_queue_inflight_excludes_awaiting_human(self) -> None:
        self.add("parked")
        self.add("running")
        self.park("parked")
        live = self.claim("running")
        self.queue.record(
            "running", KEY, attempt_id=live.id, status="dispatched",
            provider_id="alpha", account_id="alpha-account", router_job_id="job-running",
            timestamp=iso(NOW), now_epoch=NOW,
        )
        self.assertEqual([run.task for run in self.queue.inflight()], ["running"])

    def test_viewer_get_inflight_excludes_awaiting_human(self) -> None:
        self.add("parked")
        self.add("running")
        self.park("parked")
        live = self.claim("running")
        self.queue.record(
            "running", KEY, attempt_id=live.id, status="dispatched",
            provider_id="alpha", account_id="alpha-account", router_job_id="job-running",
            timestamp=iso(NOW), now_epoch=NOW,
        )
        viewer = _load_server()
        with mock.patch.object(viewer, "DB_PATH", self.queue.path):
            inflight = viewer.get_inflight()
        # get_inflight swallows errors into [], so the live row proves the query ran.
        self.assertEqual([row["task"] for row in inflight], ["running"])


class ViewerTests(AwaitingHumanCase):
    def setUp(self) -> None:
        super().setUp()
        self.viewer = _load_server()

    def test_status_color_is_distinct_from_failed(self) -> None:
        colors = self.viewer.STATUS_COLORS
        self.assertIn("awaiting_human", colors)
        self.assertNotEqual(colors["awaiting_human"], colors["failed"])

    def test_remaining_snapshot_shows_parked_work_from_production_eligibility(self) -> None:
        self.add("parked")
        self.park("parked")
        cfg = runtime(self.queue.path)
        with (
            mock.patch.object(cli, "_queue", return_value=(cfg, self.queue)),
            captured_json() as payloads,
        ):
            code = cli.main(["queue", "0", "--json", "--now", str(NOW + 10)])
        self.assertEqual(code, 0)
        payload = payloads[0]
        self.assertNotIn("parked", payload["eligible_task_ids"])
        result = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        with mock.patch.object(self.viewer.subprocess, "run", return_value=result):
            try:
                remaining = self.viewer._remaining_snapshot(0)
            except RuntimeError as exc:
                self.fail(f"viewer rejected awaiting_human readiness: {exc}")
        parked = [item for item in remaining if item["id"] == "parked"]
        self.assertEqual([item["readiness"]["state"] for item in parked], ["awaiting_human"])
        self.assertEqual(parked[0]["readiness"]["reason"], f"Awaiting Brian: {DETAIL}")


class MigrationTests(RecoveryCase):
    def _old_schema(self) -> str:
        schema = (SKILL_ROOT / "schema.sql").read_text(encoding="utf-8")
        old = schema.replace("'failed','awaiting_human'", "'failed'")
        self.assertIn("CHECK (status IN ('dispatched','done','skipped','failed'))", old)
        self.assertIn(
            "CHECK (state IN ('claimed','dispatched','done','skipped','failed','ambiguous','aborted'))", old,
        )
        return old

    def test_old_check_constraints_are_relaxed_idempotently_preserving_rows(self) -> None:
        path = self.root / "old.db"
        with sqlite3.connect(path) as connection:
            connection.executescript(self._old_schema())
            connection.execute(
                "INSERT INTO runs(task,kind,cycle,eligibility_key,status,ts) "
                "VALUES('legacy','oneoff',0,NULL,'failed',?)", (iso(NOW),),
            )
        insert = (
            "INSERT INTO runs(task,kind,cycle,eligibility_key,status,ts) "
            "VALUES('new','oneoff',0,NULL,'awaiting_human',?)"
        )
        with sqlite3.connect(path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(insert, (iso(NOW),))

        queue = db.QueueDB(path)
        queue.initialize()
        with sqlite3.connect(path) as connection:
            for table in ("runs", "task_attempts"):
                sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
                ).fetchone()[0]
                self.assertIn("'awaiting_human'", sql, table)
            connection.execute(insert, (iso(NOW),))

        queue.initialize()
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            statuses = [
                row[0] for row in connection.execute("SELECT status FROM runs ORDER BY rowid_pk")
            ]
            self.assertEqual(statuses, ["failed", "awaiting_human"])
            for table in ("runs", "task_attempts"):
                sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
                ).fetchone()[0]
                self.assertEqual(sql.count("'awaiting_human'"), 1, table)


class PromptTests(RecoveryCase):
    def _prompt(self, task_id: str) -> str:
        config = runtime(self.queue.path)
        return dispatcher.render_prompt(
            config, self.queue.task(task_id), "manual/awaiting-human", "alpha", "alpha-account",
        )

    def _assert_contract(self, prompt: str) -> None:
        self.assertIn("done|skipped|failed|awaiting_human", prompt)
        self.assertIn("Opening or updating a pull request is not done", prompt)
        self.assertIn("all PR checks pass for the current head", prompt)
        self.assertIn("confirmed merged into the exact authorized epic branch", prompt)
        self.assertIn("must never be recorded as done", prompt)
        self.assertNotIn("even while that PR awaits", prompt)
        self.assertIn("completion.mechanism=artifact", prompt)
        self.assertIn("Record awaiting_human only when", prompt)
        self.assertIn("name exactly what Brian must do", prompt)
        self.assertIn(
            "If the work itself cannot be completed, record failed with the blocker before exiting",
            prompt,
        )
        self.assertNotIn("If safe progress requires new input or authority", prompt)
        self.assertIn("Failed, skipped, or awaiting_human results require the structured reason", prompt)

    def test_oneoff_prompt_carries_awaiting_human_contract(self) -> None:
        self.add("oneoff")
        self._assert_contract(self._prompt("oneoff"))

    def test_recurring_prompt_carries_awaiting_human_contract(self) -> None:
        self.add("weekly", kind="recurring", cadence="weekly")
        self._assert_contract(self._prompt("weekly"))

    def test_configured_pr_permission_preserves_explicit_epic_merge_authority(self) -> None:
        item = self.add("epic", constraints="Merge only into epic/example. Done means merged.")
        config = replace(runtime(self.queue.path), pr_exceptions=(
            {"path": str(self.root), "allow_push": True, "allow_pr": True},
        ))
        prompt = dispatcher.render_prompt(
            config, item, "manual/epic", "alpha", "alpha-account",
        )
        self.assertIn("explicitly grants merge authority into a named epic/* branch", prompt)
        self.assertIn("Otherwise, do not merge", prompt)
        self.assertNotIn("never merge it", prompt)
        self._assert_contract(prompt)


class FactoryTerminalTests(unittest.TestCase):
    ROW = {"factory_version": "v1", "repo": "r", "tier": "quick", "status": "dispatched",
           "completed_at": None, "outcome": None, "pr_url": None}

    def test_awaiting_human_maps_to_factory_awaiting_human(self) -> None:
        self.assertEqual(factory_terminal.LEDGER_TO_RUN_STATUS.get("awaiting_human"), "awaiting_human")
        fills = factory_terminal.candidate_fills(
            self.ROW, "awaiting_human", "2026-09-14T15:00:00Z", "Needs Brian to approve.", "/tmp",
            lambda ref, cwd: None,
        )
        self.assertEqual(fills["status"], "awaiting_human")

    def test_done_with_pr_maps_to_complete_with_pr_url(self) -> None:
        fills = factory_terminal.candidate_fills(
            self.ROW, "done", "2026-09-14T15:00:00Z", f"Opened {PR_URL} awaiting review.", "/tmp",
            lambda ref, cwd: ref if ref.startswith("https://") else None,
        )
        self.assertEqual(fills["status"], "complete")
        self.assertEqual(fills["pr_url"], PR_URL)


if __name__ == "__main__":
    unittest.main()
