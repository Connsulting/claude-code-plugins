"""Scout recovery uses terminal worker evidence and preserves queue ownership otherwise."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/bonus-drain/skills/bonus-drain"))

from bonus_drain import db, reconcile, scout
from bonus_drain.adapters import AdapterError
from bonus_drain.dispatcher import DispatchError

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
        )
        self.assertIsNotNone(self.attempt)
        self.queue.acquire_activation(
            "abandoned", ELIGIBILITY_KEY, "alpha", "alpha-account", lambda: None,
        )
        self.queue.record(
            "abandoned", ELIGIBILITY_KEY, attempt_id=self.attempt.id,
            status="dispatched", cycle=RESET,
            provider_id="alpha", account_id="alpha-account", router_job_id="job-1",
        )

    def response(self, state: str = "completed", **extra):
        return {"rows": [{
            "provider": "alpha", "job_id": "job-1", "state": state, **extra,
        }]}

    def run_scout(self, response, *, dry_run=False, release=None, config=None, usage_snapshots=None):
        with (
            mock.patch.object(reconcile, 'execute_adapter', return_value=response),
            mock.patch.object(scout, 'read_all', return_value=snapshots() if usage_snapshots is None else usage_snapshots),
            mock.patch.object(scout, 'dispatch') as dispatch,
        ):
            report = scout.run_once(
                config or self.config, self.queue, now_epoch=NOW, dry_run=dry_run,
                activation_call=release,
            )
        return report, dispatch

    def test_terminal_worker_releases_ownership_and_unblocks_dispatch_once(self):
        self.queue.add_task(reviewed(task("next-job")))
        release = mock.Mock()
        report, dispatch = self.run_scout(self.response(), release=release)
        self.assertEqual(report.blockers, ())
        self.assertEqual(report.reconciliation[0]["action"], "failed")
        self.assertEqual(dispatch.call_args.kwargs["task_id"], "next-job")
        release.assert_called_once_with("release", "alpha-account")
        self.assertEqual(self.queue.inflight(), [])
        self.assertEqual(self.queue.claims(), [])
        self.assertEqual(self.queue.activation_leases(), [])
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
                self.assertEqual(len(self.queue.activation_leases()), 1)
                self.assertEqual(len(self.queue.runs()), 1)

    def test_dry_run_reports_repair_without_recording_or_releasing(self):
        release = mock.Mock()
        report, dispatch = self.run_scout(self.response("failed"), dry_run=True, release=release)
        self.assertEqual(report.reconciliation[0]["action"], "would_fail")
        self.assertEqual(len(self.queue.runs()), 1)
        self.assertEqual(len(self.queue.activation_leases()), 1)
        release.assert_not_called()
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
                release_activation=lambda: None,
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
                release_activation=lambda: None,
            )
            return original(*args, **kwargs)

        with mock.patch.object(self.queue, "record", side_effect=racing_record):
            report, _ = self.run_scout(self.response())
        self.assertIn("already recorded as skipped", report.reconciliation[0]["reason"])
        self.assertEqual([r.status for r in self.queue.runs()], ["skipped", "dispatched"])

    def test_failed_activation_release_stays_blocked(self):
        release = mock.Mock(side_effect=DispatchError("release failed"))
        report, dispatch = self.run_scout(self.response(), release=release)
        self.assertEqual(report.reconciliation[0]["action"], "held")
        self.assertEqual(len(self.queue.inflight()), 1)
        self.assertEqual(self.queue.activation_leases()[0].state, "releasing")
        self.assertFalse(db.doctor(self.queue).ok)
        dispatch.assert_not_called()

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
        release = mock.Mock()
        report, _dispatch = self.run_scout(self.response(
            "turn_ended", run_status="cancelled",
            last_activity_at=self._activity_stamp(1800), surface="t3",
        ), release=release)
        self.assertEqual(report.reconciliation[0]["action"], "failed")
        self.assertEqual([row.status for row in self.queue.runs()], ["failed", "dispatched"])
        self.assertEqual(self.queue.claims(), [])
        self.assertEqual(self.queue.activation_leases(), [])
        release.assert_called_once_with("release", "alpha-account")
        stored = self.queue.attempts(task_id="abandoned")
        self.assertEqual(stored[0].reason_signature, "unknown_launch:t3_run_cancelled")

    def test_cancelled_t3_run_idle_5_minutes_stays_held(self):
        report, _dispatch = self.run_scout(self.response(
            "turn_ended", run_status="cancelled",
            last_activity_at=self._activity_stamp(300), surface="t3",
        ))
        self.assertEqual(report.reconciliation[0]["action"], "held")
        self.assertEqual(len(self.queue.claims()), 1)
        self.assertEqual(len(self.queue.activation_leases()), 1)
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
        self.assertEqual(len(self.queue.activation_leases()), 1)
        self.assertEqual(len(self.queue.runs()), 1)

    def test_state_read_failure_preserves_the_claim_and_a_terminal_run_fails_it(self):
        release = mock.Mock()
        with mock.patch.object(
            reconcile, "execute_adapter", side_effect=reconcile.T3StateError("locked"),
        ):
            report = reconcile.reconcile_inflight(self.config, self.queue, activation_call=release)
        self.assertEqual(report[0]["action"], "held")
        self.assertIn("locked", report[0]["reason"])
        release.assert_not_called()
        with mock.patch.object(
            reconcile, "execute_adapter",
            return_value=reconcile.T3Thread("failed", False, None, None),
        ):
            report = reconcile.reconcile_inflight(self.config, self.queue, activation_call=release)
        self.assertEqual(report[0]["action"], "failed")
        self.assertEqual(self.queue.inflight(), [])


if __name__ == "__main__":
    unittest.main()
