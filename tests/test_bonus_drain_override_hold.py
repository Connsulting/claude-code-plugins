"""Operator override of retained recovery holds through public requeue."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import unittest
from unittest import mock

from tests.test_bonus_dependency_recovery import (
    NOW, KEY, RecoveryCase, as_dict, captured_json, iso, reason, rows, verified,
)
from bonus_drain import cli, db, goals
from tests.readiness_fixture import rereviewed


class OverrideHoldCase(RecoveryCase):
    def requeue_cli(self, task_id: str, *extra: str, now: int = NOW + 10) -> tuple[int, list[object]]:
        with captured_json() as payloads:
            code = cli.main([
                "requeue", "--database", str(self.queue.path), task_id,
                "--json", "--now", str(now), *extra,
            ])
        return code, payloads

    def override(self, task_id: str, text: str = "Brian granted access", now: int = NOW + 10):
        return self.requeue_cli(task_id, "--override-hold", "--reason", text, now=now)

    def held_by(self, task_id: str, code: str) -> object:
        self.add(task_id)
        attempt = self.claim(task_id)
        self.terminal(task_id, attempt, "failed", reason(code, f"{code}:fixture"))
        return attempt

    def held_no_progress(self, task_id: str) -> object:
        self.add(task_id)
        self.add(f"{task_id}-child", depends_on=[task_id])
        attempt = self.claim(task_id)
        self.terminal(task_id, attempt, "failed", reason(signature="network:stable"))
        self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)
        retry = self.claim(task_id, automatic=True, now=NOW + 300)
        self.terminal(task_id, retry, "failed", reason(signature="network:stable"), now=NOW + 300)
        return retry

    def recovery(self, task_id: str) -> dict[str, object]:
        return as_dict(self.queue.recovery_for(task_id))


class OverrideSucceeds(OverrideHoldCase):
    def assert_overridden(self, task_id: str, code: str, now: int) -> None:
        exit_code, payloads = self.override(task_id, "approved by Brian", now=now)
        self.assertEqual(exit_code, 0, payloads)
        after = self.recovery(task_id)
        self.assertEqual(
            (after["origin"], after["state"], after["not_before"], after["reason_code"]),
            ("operator", "scheduled", iso(now), code),
        )
        self.assertIn("operator hold override", after["detail"])
        self.assertIn("approved by Brian", after["detail"])

    def test_each_allowed_hold_reason_is_overridden_after_a_cached_held_row(self) -> None:
        for code in ("authority_required", "permanent"):
            with self.subTest(code=code):
                task_id = f"held-{code}"
                self.held_by(task_id, code)
                default_code, _ = self.requeue_cli(task_id)
                self.assertEqual(default_code, 1)
                self.assertEqual(self.recovery(task_id)["state"], "held")
                self.assert_overridden(task_id, code, NOW + 20)

        self.held_no_progress("held-progress")
        default_code, _ = self.requeue_cli("held-progress", now=NOW + 301)
        self.assertEqual(default_code, 1)
        self.assertEqual(
            (self.recovery("held-progress")["state"], self.recovery("held-progress")["reason_code"]),
            ("held", "no_progress"),
        )
        self.assert_overridden("held-progress", "no_progress", NOW + 400)

    def test_repeat_override_reschedules_and_edit_is_permitted(self) -> None:
        self.held_by("repeat", "authority_required")
        self.assertEqual(self.override("repeat", "first")[0], 0)
        exit_code, _ = self.override("repeat", "second look", now=NOW + 50)
        self.assertEqual(exit_code, 0)
        self.assertIn("second look", self.recovery("repeat")["detail"])
        self.assertEqual(self.recovery("repeat")["not_before"], iso(NOW + 50))

        edited = self.queue.edit_task(
            "repeat", rereviewed(self.queue.task("repeat"), {"goal": "corrected contract after override"}),
        )
        self.assertEqual(edited.goal, "corrected contract after override")

    def test_edit_still_refused_while_held_without_override(self) -> None:
        self.held_by("stuck", "permanent")
        self.requeue_cli("stuck")
        with self.assertRaisesRegex(db.QueueError, "held"):
            self.queue.edit_task("stuck", {"goal": "changed"})


class OverrideRefuses(OverrideHoldCase):
    def assert_refused(self, task_id: str, pattern: str) -> None:
        before = rows(self.queue, "SELECT * FROM task_recovery WHERE task_id=?", (task_id,))
        exit_code, payloads = self.override(task_id)
        self.assertEqual(exit_code, 1, payloads)
        self.assertFalse(payloads[0]["ok"])
        self.assertRegex(payloads[0]["message"], pattern)
        self.assertEqual(
            rows(self.queue, "SELECT * FROM task_recovery WHERE task_id=?", (task_id,)), before,
        )

    def test_unknown_launch_is_refused(self) -> None:
        self.held_by("unknown", "unknown_launch")
        self.requeue_cli("unknown")
        self.assert_refused("unknown", "unknown_launch")

    def test_dispatch_claim_is_refused(self) -> None:
        self.held_by("claimed", "authority_required")
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute(
                "INSERT INTO dispatch_claims(task_id,eligibility_key,provider_id,state,claimed_at)"
                " VALUES('claimed','other-key','alpha','ambiguous',?)",
                (iso(NOW),),
            )
        self.assert_refused("claimed", "launch ownership")

    def test_goal_managed_task_is_refused(self) -> None:
        self.held_by("goal-task", "authority_required")
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute(
                "INSERT INTO goals(id,contract_json,state,summary,created_at)"
                " VALUES('g1','{}','queued','fixture',?)",
                (iso(NOW),),
            )
            connection.execute(
                "INSERT INTO goal_members(goal_id,task_id,role,managed)"
                " VALUES('g1','goal-task','implementation',1)",
            )
        self.assert_refused("goal-task", "goal-owned")

    def test_recovery_admission_rejection_is_refused(self) -> None:
        self.held_by("admission", "authority_required")
        with mock.patch.object(goals, "recovery_admission", return_value=(False, "goal_paused")):
            self.assert_refused("admission", "goal_paused")

    def test_verified_done_is_refused(self) -> None:
        self.add("finished")
        attempt = self.claim("finished")
        self.terminal("finished", attempt, "done", verified())
        self.assert_refused("finished", "done")

    def test_non_oneoff_is_refused(self) -> None:
        self.add("weekly", kind="recurring", cadence="weekly")
        self.assert_refused("weekly", "one-off")

    def test_changed_contract_is_refused(self) -> None:
        self.held_by("changed", "authority_required")
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute("UPDATE tasks SET goal='silently changed' WHERE id='changed'")
        self.assert_refused("changed", "contract changed")

    def test_changed_contract_on_a_legacy_source_is_refused(self) -> None:
        self.add("legacy")
        self.queue.record(
            "legacy", None, cycle=0, status="failed", summary="old worker",
            outcome=reason("authority_required", "authority_required:legacy"),
        )
        self.requeue_cli("legacy")
        self.assertEqual(self.recovery("legacy")["state"], "held")
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute("UPDATE tasks SET goal='silently changed' WHERE id='legacy'")
        self.assert_refused("legacy", "contract changed")

    def test_not_held_recovery_is_refused(self) -> None:
        self.add("retryable")
        self.fail_task("retryable")
        self.assert_refused("retryable", "not held")

    def test_missing_or_blank_reason_is_rejected(self) -> None:
        self.held_by("no-reason", "authority_required")
        for extra in (("--override-hold",), ("--override-hold", "--reason", "   ")):
            with self.subTest(extra=extra):
                exit_code, _ = self.requeue_cli("no-reason", *extra)
                self.assertNotEqual(exit_code, 0)
                self.assertEqual(rows(self.queue, "SELECT * FROM task_recovery"), [])
        with self.assertRaisesRegex(db.QueueError, "non-empty reason"):
            self.queue.requeue("no-reason", override_reason=" ", now_epoch=NOW)


class DefaultAndAutomaticUnchanged(OverrideHoldCase):
    def test_default_requeue_keeps_the_cached_hold(self) -> None:
        self.held_by("plain", "authority_required")
        first, _ = self.requeue_cli("plain")
        held = self.recovery("plain")
        second, payloads = self.requeue_cli("plain", now=NOW + 99)
        self.assertEqual((first, second), (1, 1))
        self.assertEqual(self.recovery("plain"), held)
        self.assertNotIn("override", str(payloads))

    def test_default_retryable_requeue_still_schedules(self) -> None:
        self.add("ok")
        self.fail_task("ok")
        self.assertEqual(self.requeue_cli("ok")[0], 0)
        self.assertEqual(self.recovery("ok")["state"], "scheduled")

    def test_automatic_recovery_cannot_use_the_override(self) -> None:
        self.held_by("auto", "authority_required")
        self.add("auto-child", depends_on=["auto"])
        with self.queue._transaction() as connection:
            with self.assertRaisesRegex(db.QueueError, "only operator"):
                self.queue._request_recovery_in_connection(
                    connection, "auto", expected_attempt_id=None,
                    expected_legacy_run_rowid=None, mode=None, source="automatic",
                    now_epoch=NOW, override_reason="sneaky",
                )
        self.queue.reconcile_recoveries(now_epoch=NOW + 1, dry_run=False)
        self.assertEqual(self.recovery("auto")["state"], "held")


if __name__ == "__main__":
    unittest.main()
