"""Scout recovery uses terminal worker evidence and preserves queue ownership otherwise."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/bonus-drain/skills/bonus-drain"))

from bonus_drain import db, reconcile, scout
from bonus_drain.adapters import AdapterError
from bonus_drain.dispatcher import DispatchError
from tests.test_bonus_drain_review_repairs import (
    ELIGIBILITY_KEY, NOW, RESET, runtime, snapshots, task,
)
from tests.test_bonus_drain_scout_inflight import _open_snapshots, _two_provider_config


class ScoutReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "queue.db")
        self.config = runtime(self.queue.path)
        self.queue.add_task(task("abandoned"))
        self.queue.claim("abandoned", ELIGIBILITY_KEY, "alpha", "alpha-account")
        self.queue.acquire_activation(
            "abandoned", ELIGIBILITY_KEY, "alpha", "alpha-account", lambda: None,
        )
        self.queue.record(
            "abandoned", ELIGIBILITY_KEY, status="dispatched", cycle=RESET,
            provider_id="alpha", account_id="alpha-account", router_job_id="job-1",
        )

    def response(self, state: str = "completed", **extra):
        return {"rows": [{
            "provider": "alpha", "job_id": "job-1", "state": state, **extra,
        }]}

    def run_scout(self, response, *, dry_run=False, release=None, config=None, usage_snapshots=None):
        with mock.patch.object(reconcile, "execute_adapter", return_value=response):
            with mock.patch.object(
                scout, "read_all",
                return_value=snapshots() if usage_snapshots is None else usage_snapshots,
            ):
                with mock.patch.object(scout, "dispatch") as dispatch:
                    report = scout.run_once(
                        config or self.config, self.queue, now_epoch=NOW, dry_run=dry_run,
                        activation_call=release,
                    )
        return report, dispatch

    def test_terminal_worker_releases_ownership_and_unblocks_dispatch_once(self):
        self.queue.add_task(task("next-job"))
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
        self.queue.add_task(task("healthy-beta") | {"allowed_providers": ["beta"]})
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
                "abandoned", ELIGIBILITY_KEY, status="done", release_activation=lambda: None,
            )
            return self.response()

        with mock.patch.object(reconcile, "execute_adapter", side_effect=probe):
            report = reconcile.reconcile_inflight(self.config, self.queue)
        self.assertEqual(report[0]["action"], "already_recorded")
        self.assertEqual([r.status for r in self.queue.runs()], ["done", "dispatched"])

    def test_worker_terminal_at_record_boundary_wins(self):
        original = self.queue.record

        def racing_record(*args, **kwargs):
            original("abandoned", ELIGIBILITY_KEY, status="skipped", release_activation=lambda: None)
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
        with mock.patch.object(reconcile, "execute_adapter") as probe:
            with mock.patch.object(scout, "read_all", return_value=snapshots()):
                report = scout.run_once(self.config, self.queue, now_epoch=NOW)
        self.assertEqual(report.blockers[0]["kind"], "reconciliation_required")
        probe.assert_not_called()

    def test_real_status_subprocess_accepts_failure_report_but_not_probe_failure(self):
        router = self.root / "router"
        config = runtime(self.queue.path, router_path=str(router))
        release = mock.Mock()
        def script(exit_code):
            router.write_text(
                "#!/usr/bin/env python3\nimport json,sys\n"
                "assert sys.argv[1:] == ['status', '--limit', '1000', '--json']\n"
                f"print({json.dumps(self.response('failed'))!r})\n"
                f"sys.exit({exit_code})\n"
            )
            router.chmod(0o755)

        script(2)
        report = reconcile.reconcile_inflight(config, self.queue, activation_call=release)
        self.assertEqual(report[0]["action"], "held")
        self.assertEqual(len(self.queue.inflight()), 1)
        release.assert_not_called()
        script(1)
        report = reconcile.reconcile_inflight(config, self.queue, activation_call=release)
        self.assertEqual(report[0]["action"], "failed")
        self.assertEqual(self.queue.inflight(), [])


if __name__ == "__main__":
    unittest.main()
