"""Scout recovery uses terminal worker evidence and preserves queue ownership otherwise."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/bonus-drain/skills/bonus-drain"))

from bonus_drain import db, reconcile, scout, t3_state
from bonus_drain.adapters import AdapterError

from tests.readiness_fixture import reviewed
from tests.test_bonus_drain_review_repairs import (
    ELIGIBILITY_KEY,
    NOW,
    RESET,
    runtime,
    snapshots,
    task,
)
from tests.test_bonus_drain_scout_inflight import _open_snapshots, _two_provider_config


def verified_outcome():
    return {
        "reason": {
            "code": "done_when_verified", "detail": "fixture proof",
            "signature": "done_when_verified:reconcile-fixture",
        },
        "completion": {
            "verified": True, "mechanism": "command",
            "evidence": ["fixture://reconciliation"],
        },
    }


class T3StateFixture:
    """Query columns and lineage shape observed read-only in T3 state on 2026-10-13."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with sqlite3.connect(path) as connection:
            connection.executescript("""
                CREATE TABLE orchestration_v2_projection_threads (
                    thread_id TEXT PRIMARY KEY, provider_instance_id TEXT,
                    updated_at TEXT, deleted_at TEXT, payload_json TEXT NOT NULL
                );
                CREATE TABLE orchestration_v2_projection_runs (
                    run_id TEXT PRIMARY KEY, thread_id TEXT, ordinal INTEGER,
                    status TEXT, requested_at TEXT, completed_at TEXT, payload_json TEXT
                );
                CREATE TABLE orchestration_v2_projection_provider_sessions (
                    provider_session_id TEXT PRIMARY KEY, provider_instance_id TEXT,
                    updated_at TEXT, payload_json TEXT
                );
                CREATE TABLE orchestration_v2_projection_provider_session_bindings (
                    provider_session_id TEXT, thread_id TEXT
                );
                CREATE TABLE orchestration_v2_projection_turn_items (
                    thread_id TEXT, run_id TEXT, type TEXT, status TEXT,
                    node_id TEXT, updated_at TEXT, ordinal INTEGER, payload_json TEXT
                );
            """)

    @staticmethod
    def stamp(age: int) -> str:
        return datetime.fromtimestamp(NOW - age, timezone.utc).isoformat().replace("+00:00", "Z")

    def thread(
        self, thread_id: str, parent: str | None = None, *,
        relationship: str = "subagent", deleted: bool = False, activity_age: int = 1800,
    ) -> None:
        payload = {} if parent is None else {
            "lineage": {"parentThreadId": parent, "relationshipToParent": relationship},
        }
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO orchestration_v2_projection_threads VALUES(?,?,?,?,?)",
                (thread_id, "alpha", self.stamp(activity_age), self.stamp(0) if deleted else None,
                 json.dumps(payload)),
            )

    def run(
        self, thread_id: str, status: str, *, ordinal: int = 1,
        ended_age: int | None = 1800, held: bool = False,
    ) -> None:
        completed_at = (
            self.stamp(ended_age)
            if status in t3_state.TERMINAL_RUN_STATUSES and ended_age is not None else None
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO orchestration_v2_projection_runs VALUES(?,?,?,?,?,?,?)",
                (f"{thread_id}-run-{ordinal}", thread_id, ordinal, status, self.stamp(3600),
                 completed_at, json.dumps({"startedAt": self.stamp(3600), "queueHeld": held})),
            )


class T3StateReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = T3StateFixture(self.root / "t3.sqlite")
        self.state.thread("parent")
        self.state.run("parent", "cancelled")

    def lookup(self):
        result = t3_state.lookup_thread("parent", self.state.path)
        self.assertIsNotNone(result)
        return result

    def test_recursive_descendants_with_active_or_unsettled_latest_runs_are_live(self):
        self.state.thread("child", "parent")
        self.state.run("child", "completed")
        self.state.thread("grandchild", "child")
        for status in ("preparing", "starting", "running", "queued", "waiting", "unknown"):
            with self.subTest(status=status):
                self.state.run("grandchild", status)
                observed = self.lookup()
                self.assertEqual(observed.run_status, "cancelled")
                self.assertFalse(observed.active_run)
                self.assertTrue(observed.live_descendant)
        self.state.run("grandchild", "interrupted")
        self.assertFalse(self.lookup().live_descendant)

    def test_any_active_descendant_run_blocks_even_when_latest_is_terminal(self):
        self.state.thread("child", "parent")
        self.state.run("child", "running")
        self.state.run("child", "failed", ordinal=2)
        self.assertTrue(self.lookup().live_descendant)

    def test_no_run_or_held_queued_descendant_is_unsettled(self):
        self.state.thread("child", "parent")
        self.assertTrue(self.lookup().live_descendant)
        self.state.run("child", "completed")
        self.state.run("child", "queued", ordinal=2, held=True)
        self.assertTrue(self.lookup().live_descendant)

    def test_parent_active_history_and_held_latest_run_are_reported_separately(self):
        self.state.run("parent", "running")
        self.state.run("parent", "completed", ordinal=2)
        self.assertTrue(self.lookup().active_run)
        self.assertFalse(self.lookup().unsettled_run)
        self.state.run("parent", "cancelled")
        self.state.run("parent", "queued", ordinal=3, held=True)
        observed = self.lookup()
        self.assertFalse(observed.active_run)
        self.assertTrue(observed.unsettled_run)
        self.assertEqual(observed.run_status, "completed")

    def test_unrelated_threads_and_other_lineage_relationships_do_not_hold_parent(self):
        self.state.thread("unrelated")
        self.state.run("unrelated", "running")
        self.state.thread("fork", "parent", relationship="fork")
        self.state.run("fork", "running")
        self.assertFalse(self.lookup().live_descendant)

    def test_deleted_intermediate_does_not_hide_a_live_grandchild(self):
        self.state.thread("child", "parent", deleted=True)
        self.state.run("child", "completed")
        self.state.thread("grandchild", "child")
        self.state.run("grandchild", "running")
        self.assertTrue(self.lookup().live_descendant)

    def test_lineage_cycle_terminates_and_reports_settled_or_live_runs(self):
        self.state.thread("child", "parent")
        self.state.thread("parent", "child")
        self.state.run("child", "running")
        self.assertTrue(self.lookup().live_descendant)
        self.state.run("child", "completed")
        self.assertFalse(self.lookup().live_descendant)

    def test_reader_does_not_write_or_create_state_and_refuses_unreadable_schema(self):
        before = self.state.path.read_bytes()
        observed = self.lookup()
        self.assertEqual(observed.completed_at, self.state.stamp(1800))
        self.assertEqual(self.state.path.read_bytes(), before)
        self.assertIsNone(t3_state.lookup_thread("missing", self.state.path))
        missing = self.root / "missing.sqlite"
        with self.assertRaisesRegex(t3_state.T3StateError, "missing"):
            t3_state.lookup_thread("parent", missing)
        self.assertFalse(missing.exists())
        with sqlite3.connect(self.state.path) as connection:
            connection.execute("DROP TABLE orchestration_v2_projection_threads")
        with self.assertRaisesRegex(t3_state.T3StateError, "query failed"):
            t3_state.lookup_thread("parent", self.state.path)


class ScoutReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "queue.db")
        self.config = runtime(self.queue.path)
        self.queue.add_task(reviewed(task("abandoned")))
        self.attempt = self.queue.claim(
            "abandoned", ELIGIBILITY_KEY, "alpha", "alpha-account",
         provider_max_size="huge")
        self.assertIsNotNone(self.attempt)
        self.queue.record(
            "abandoned", ELIGIBILITY_KEY, attempt_id=self.attempt.id,
            status="dispatched", cycle=RESET,
            provider_id="alpha", account_id="alpha-account", router_job_id="job-1",
        )

    def response(self, state: str = "completed", **extra):
        return {"rows": [{
            "provider": "alpha", "job_id": "job-1", "state": state, **extra,
        }]}

    def run_scout(self, response, *, dry_run=False, config=None, usage_snapshots=None):
        with (
            mock.patch.object(reconcile, 'execute_adapter', return_value=response),
            mock.patch.object(scout, 'read_all', return_value=snapshots() if usage_snapshots is None else usage_snapshots),
            mock.patch.object(scout, 'dispatch') as dispatch,
        ):
            report = scout.run_once(
                config or self.config, self.queue, now_epoch=NOW, dry_run=dry_run,
                            )
        return report, dispatch

    def test_terminal_worker_releases_ownership_and_unblocks_dispatch_once(self):
        self.queue.add_task(reviewed(task("next-job")))
        report, dispatch = self.run_scout(self.response())
        self.assertEqual(report.blockers, ())
        self.assertEqual(report.reconciliation[0]["action"], "failed")
        self.assertEqual(dispatch.call_args.kwargs["task_id"], "next-job")
        self.assertEqual(self.queue.inflight(), [])
        self.assertEqual(self.queue.claims(), [])
        self.assertEqual([r.status for r in self.queue.runs()], ["failed", "dispatched"])
        self.assertFalse(self.queue.eligible_tasks(RESET, task_id="abandoned"))
        with mock.patch.object(reconcile, "execute_adapter") as probe:
            self.assertEqual(reconcile.reconcile_inflight(self.config, self.queue), ())
            probe.assert_not_called()
        self.assertEqual(len(self.queue.runs()), 2)

    def test_live_unknown_absent_wrong_provider_and_duplicate_jobs_stay_blocked(self):
        self.queue.add_task(reviewed(task("healthy-beta") | {"allowed_providers": ["beta"]}))
        two_provider = _two_provider_config(self.queue, self.root / "cache")
        responses = [
            self.response("running"), self.response("unknown", persisted="completed"),
            {"rows": []}, {"rows": [dict(self.response()["rows"][0], provider="other")]},
            {"rows": self.response()["rows"] * 2}, {"rows": "bad"},
        ]
        for response in responses:
            with self.subTest(response=response):
                report, dispatch = self.run_scout(
                    response, config=two_provider, usage_snapshots=_open_snapshots(),
                )
                self.assertEqual(report.reconciliation[0]["action"], "held")
                self.assertEqual(dispatch.call_count, 1)
                self.assertEqual(dispatch.call_args.kwargs["task_id"], "healthy-beta")
                self.assertEqual(dispatch.call_args.kwargs["requested_provider"], "beta")
                self.assertIn(("alpha", "alpha-account"), report.plan.closed)
                self.assertEqual(len(self.queue.claims()), 1)
                self.assertEqual(len(self.queue.runs()), 1)

    def test_dry_run_reports_repair_without_recording_or_releasing(self):
        report, dispatch = self.run_scout(self.response("failed"), dry_run=True)
        self.assertEqual(report.reconciliation[0]["action"], "would_fail")
        self.assertEqual(len(self.queue.runs()), 1)
        dispatch.assert_not_called()

    def test_unavailable_probe_preserves_claim(self):
        with mock.patch.object(reconcile, "execute_adapter", side_effect=AdapterError("timeout")):
            report = reconcile.reconcile_inflight(self.config, self.queue)
        self.assertEqual(report[0]["reason"], "timeout")
        self.assertEqual(len(self.queue.inflight()), 1)

    def test_worker_terminal_during_probe_wins(self):
        def probe(*args, **kwargs):
            self.queue.record(
                "abandoned", ELIGIBILITY_KEY, attempt_id=self.attempt.id,
                status="done", outcome=verified_outcome(),
            )
            return self.response()

        with mock.patch.object(reconcile, "execute_adapter", side_effect=probe):
            report = reconcile.reconcile_inflight(self.config, self.queue)
        self.assertEqual(report[0]["action"], "already_recorded")
        self.assertEqual([r.status for r in self.queue.runs()], ["done", "dispatched"])

    def test_worker_terminal_at_record_boundary_wins(self):
        original = self.queue.record

        def racing_record(*args, **kwargs):
            original(
                "abandoned", ELIGIBILITY_KEY, attempt_id=self.attempt.id,
                status="skipped",
                outcome={"reason": {
                    "code": "verification_needed", "detail": "fixture race",
                    "signature": "verification_needed:fixture-race",
                    "queue_time_knowable": False,
                }},
            )
            return original(*args, **kwargs)

        with mock.patch.object(self.queue, "record", side_effect=racing_record):
            report, _ = self.run_scout(self.response())
        self.assertIn("already recorded as skipped", report.reconciliation[0]["reason"])
        self.assertEqual([r.status for r in self.queue.runs()], ["skipped", "dispatched"])

    def test_ambiguous_claim_stops_before_probing(self):
        self.queue.mark_ambiguous("abandoned", ELIGIBILITY_KEY, detail="unknown launch")
        with (
            mock.patch.object(reconcile, 'execute_adapter') as probe,
            mock.patch.object(scout, 'read_all', return_value=snapshots()),
        ):
            report = scout.run_once(self.config, self.queue, now_epoch=NOW)
        self.assertEqual(report.blockers[0]["kind"], "reconciliation_required")
        probe.assert_not_called()

    def _activity_stamp(self, age_seconds: int) -> str:
        return datetime.fromtimestamp(NOW - age_seconds, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )

    def test_cancelled_t3_run_idle_30_minutes_is_recorded_failed(self):
        report, _dispatch = self.run_scout(self.response(
            "turn_ended", run_status="cancelled",
            last_activity_at=self._activity_stamp(1800), surface="t3",
        ))
        self.assertEqual(report.reconciliation[0]["action"], "failed")
        self.assertEqual([row.status for row in self.queue.runs()], ["failed", "dispatched"])
        self.assertEqual(self.queue.claims(), [])
        stored = self.queue.attempts(task_id="abandoned")
        self.assertEqual(stored[0].reason_signature, "unknown_launch:t3_run_cancelled")

    def test_cancelled_t3_run_idle_5_minutes_stays_held(self):
        report, _dispatch = self.run_scout(self.response(
            "turn_ended", run_status="cancelled",
            last_activity_at=self._activity_stamp(300), surface="t3",
        ))
        self.assertEqual(report.reconciliation[0]["action"], "held")
        self.assertEqual(len(self.queue.claims()), 1)
        self.assertEqual(len(self.queue.runs()), 1)

    def test_turn_ended_completed_t3_run_keeps_current_hold(self):
        report, _dispatch = self.run_scout(self.response(
            "turn_ended", run_status="completed",
            last_activity_at=self._activity_stamp(1800), surface="t3",
        ))
        self.assertEqual(report.reconciliation[0]["action"], "held")
        self.assertEqual(
            report.reconciliation[0]["reason"],
            "worker state is unknown; operator reconciliation required",
        )
        self.assertEqual(len(self.queue.claims()), 1)
        self.assertEqual(len(self.queue.runs()), 1)

    def test_state_read_failure_preserves_the_claim_and_a_terminal_run_fails_it(self):
        with mock.patch.object(
            reconcile, "execute_adapter", side_effect=reconcile.T3StateError("locked"),
        ):
            report = reconcile.reconcile_inflight(self.config, self.queue)
        self.assertEqual(report[0]["action"], "held")
        self.assertIn("locked", report[0]["reason"])
        with mock.patch.object(
            reconcile, "execute_adapter",
            return_value=reconcile.T3Thread(
                run_status="failed", active_run=False, last_error=None, last_activity_at=None,
                completed_at=None, unsettled_run=False, live_descendant=False,
            ),
        ):
            report = reconcile.reconcile_inflight(self.config, self.queue)
        self.assertEqual(report[0]["action"], "failed")
        self.assertEqual(self.queue.inflight(), [])

    def test_sqlite_nested_child_keeps_ownership_until_every_run_settles(self):
        state = T3StateFixture(self.root / "t3.sqlite")
        state.thread("job-1")
        state.run("job-1", "cancelled")
        state.thread("child", "job-1")
        state.run("child", "completed")
        state.thread("grandchild", "child")
        state.run("grandchild", "queued")
        report = reconcile.reconcile_inflight(
            self.config, self.queue, state_path=state.path, now_epoch=NOW,
        )
        self.assertEqual((report[0]["action"], report[0]["reason"]), ("held", "worker is running"))
        self.assertEqual(len(self.queue.claims()), 1)
        self.assertEqual(len(self.queue.runs()), 1)
        state.run("grandchild", "completed")
        report = reconcile.reconcile_inflight(
            self.config, self.queue, state_path=state.path, now_epoch=NOW,
        )
        self.assertEqual(report[0]["action"], "failed")
        self.assertEqual(self.queue.attempts(task_id="abandoned")[0].reason_signature,
                         "unknown_launch:t3_run_cancelled")
        self.assertEqual(self.queue.claims(), [])

    def test_sqlite_completed_parent_with_live_child_keeps_queue_running(self):
        state = T3StateFixture(self.root / "t3.sqlite")
        state.thread("job-1")
        state.run("job-1", "completed")
        state.thread("child", "job-1")
        state.run("child", "running")
        report = reconcile.reconcile_inflight(
            self.config, self.queue, state_path=state.path, now_epoch=NOW,
        )
        self.assertEqual((report[0]["action"], report[0]["reason"]), ("held", "worker is running"))
        self.assertEqual(len(self.queue.claims()), 1)
        self.assertEqual(len(self.queue.inflight()), 1)
        self.assertEqual(len(self.queue.runs()), 1)
        state.run("child", "completed")
        report = reconcile.reconcile_inflight(
            self.config, self.queue, state_path=state.path, now_epoch=NOW,
        )
        self.assertEqual(report[0]["action"], "failed")
        self.assertEqual(self.queue.claims(), [])

    def test_sqlite_parent_completion_grace_ends_at_exactly_900_seconds(self):
        state = T3StateFixture(self.root / "t3.sqlite")
        state.thread("job-1")
        state.run("job-1", "cancelled", ended_age=899)
        report = reconcile.reconcile_inflight(
            self.config, self.queue, state_path=state.path, now_epoch=NOW,
        )
        self.assertEqual((report[0]["action"], report[0]["reason"]), ("held", "worker is running"))
        self.assertEqual(len(self.queue.claims()), 1)
        report = reconcile.reconcile_inflight(
            self.config, self.queue, state_path=state.path, now_epoch=NOW + 1,
        )
        self.assertEqual(report[0]["action"], "failed")
        self.assertEqual(self.queue.claims(), [])

    def test_completion_grace_applies_to_every_terminal_t3_status(self):
        for status in t3_state.TERMINAL_RUN_STATUSES:
            with self.subTest(status=status):
                observed = t3_state.T3Thread(
                    run_status=status, active_run=False, last_error=None, last_activity_at=None,
                    completed_at=self._activity_stamp(899), unsettled_run=False, live_descendant=False,
                )
                held = reconcile._normalize_observation(observed, "job-1", provider_name="alpha", now_epoch=NOW)
                self.assertEqual(held.hold_reason, "worker is running")
                terminal = reconcile._normalize_observation(observed, "job-1", provider_name="alpha", now_epoch=NOW + 1)
                self.assertIsNone(terminal.hold_reason)
                self.assertEqual(terminal.signature, (
                    f"unknown_launch:t3_run_{status}" if status in {"cancelled", "interrupted"}
                    else "unknown_launch:missing_terminal_record"
                ))

    def test_recent_thread_metadata_does_not_extend_old_run_completion_grace(self):
        state = T3StateFixture(self.root / "t3.sqlite")
        state.thread("job-1", activity_age=1)
        state.run("job-1", "failed", ended_age=1800)
        report = reconcile.reconcile_inflight(
            self.config, self.queue, state_path=state.path, now_epoch=NOW,
        )
        self.assertEqual(report[0]["action"], "failed")
        self.assertEqual(self.queue.attempts(task_id="abandoned")[0].reason_signature,
                         "unknown_launch:missing_terminal_record")


if __name__ == "__main__":
    unittest.main()
