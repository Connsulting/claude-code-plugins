"""Scout health transitions through SQLite and a local HTTP receiver."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "plugins" / "bonus-drain" / "skills" / "bonus-drain"),
)

from bonus_drain import cli, config, db, notifications, scout, usage
from bonus_drain.planner import PlanResult

from tests.readiness_fixture import isolated_cli, reviewed
from tests.test_bonus_drain_review_repairs import ELIGIBILITY_KEY, NOW, runtime, task
from tests.test_bonus_drain_scout_host_gate import SKILL_ROOT, _raw_config


class ScoutNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = db.QueueDB(self.root / "queue.db")
        self.runtime_dir = self.root / "run"
        self.runtime_dir.mkdir()
        runtime_env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime_dir)})
        runtime_env.start()
        self.addCleanup(runtime_env.stop)
        self.bodies = []
        self.response_status = 200
        owner = self

        class Capture(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.bodies.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
                self.send_response(owner.response_status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}/test-topic"
        self.cfg = replace(runtime(self.queue.path), scout_ntfy_url=self.url)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def tick(self, now=NOW, status=1, **kwargs):
        notifications.scout_health(
            self.cfg, self.queue, now_epoch=now, exit_status=status,
            blockers=({"kind": "reconciliation_required", "tasks": ["frozen-task"],
                       "message": "secret must stay local"},) if status else (),
            **kwargs,
        )

    def test_first_stuck_and_daily_reminder_are_deduplicated_durably(self):
        self.tick()
        self.queue = db.QueueDB(self.queue.path)
        self.tick(NOW + 1)
        self.tick(NOW + 86399)
        self.assertEqual(len(self.bodies), 1)
        self.tick(NOW + 86400)
        self.assertEqual(len(self.bodies), 2)
        self.assertIn("reconciliation_required", self.bodies[0])
        self.assertIn("frozen-task", self.bodies[0])
        self.assertIn("bonus-drain doctor --json", self.bodies[0])
        self.assertNotIn("secret", self.bodies[0])

    def test_normal_zero_dispatch_and_unset_topic_do_not_alert(self):
        self.tick(status=0)
        notifications.scout_health(
            replace(self.cfg, scout_ntfy_url=None), self.queue,
            now_epoch=NOW, exit_status=1,
        )
        self.assertEqual(self.bodies, [])

    def test_recovery_sends_once_and_new_failure_alerts_immediately(self):
        self.tick()
        self.tick(NOW + 1, status=0)
        self.tick(NOW + 2, status=0)
        self.assertEqual(len(self.bodies), 2)
        self.assertIn("recovered", self.bodies[1])
        self.assertIn("frozen-task", self.bodies[1])
        self.tick(NOW + 3)
        self.assertEqual(len(self.bodies), 3)

    def test_dry_run_and_skipped_ticks_do_not_send_or_recover(self):
        self.tick(dry_run=True)
        self.tick(skipped=True)
        self.assertEqual(self.bodies, [])
        self.tick()
        self.tick(status=0, dry_run=True)
        self.tick(status=0, skipped=True)
        self.tick(NOW + 1)
        self.assertEqual(len(self.bodies), 1)

    def test_reconciliation_blocker_alerts_even_with_zero_exit(self):
        notifications.scout_health(
            self.cfg, self.queue, now_epoch=NOW, exit_status=0,
            blockers=({"kind": "reconciliation_required", "tasks": ["frozen-task"]},),
        )
        self.assertEqual(len(self.bodies), 1)

    def test_usage_outage_threshold_deduplication_and_recovery_through_scout(self):
        snapshot = {"provider_id": "alpha", "account_id": "alpha-account",
                    "captured_at": NOW, "fresh": False,
                    "error": "proxy usage unavailable secret", "limits": {}}

        def tick(now, unhealthy=True, **kwargs):
            reading = dict(snapshot, captured_at=now, fresh=not unhealthy,
                           error=snapshot["error"] if unhealthy else None)
            report = scout.ScoutReport(
                now, False, PlanResult((), {}, (), now, (reading,)), (), (), (),
            )
            with mock.patch.object(scout, "_run_locked", return_value=report):
                result = scout.run_once(self.cfg, self.queue, now_epoch=now, **kwargs)
            self.assertEqual(result, report)

        tick(NOW)
        tick(NOW + 3599)
        self.assertEqual(self.bodies, [])
        self.queue = db.QueueDB(self.queue.path)
        tick(NOW + 3600)
        tick(NOW + 90000)
        self.assertEqual(len(self.bodies), 1)
        self.assertIn("usage_unavailable", self.bodies[0])
        self.assertNotIn("secret", self.bodies[0])
        tick(NOW + 90001, unhealthy=False)
        tick(NOW + 90002, unhealthy=False)
        self.assertEqual(len(self.bodies), 2)
        self.assertIn("recovered", self.bodies[1])
        tick(NOW + 90003)
        tick(NOW + 93603)
        self.assertEqual(len(self.bodies), 3)

    def test_usage_outage_transient_and_reserve_closures_do_not_alert(self):
        def tick(now, fresh):
            reading = {"provider_id": "alpha", "account_id": "alpha-account",
                       "captured_at": now, "fresh": fresh,
                       "error": None if fresh else "proxy usage unavailable"}
            report = scout.ScoutReport(
                now, False, PlanResult((), {("alpha", "alpha"): "at reserve target"},
                                       (), now, (reading,)), (), (), (),
            )
            with mock.patch.object(scout, "_run_locked", return_value=report):
                scout.run_once(self.cfg, self.queue, now_epoch=now)

        tick(NOW, False)
        tick(NOW + 3599, True)
        tick(NOW + 7200, False)
        self.assertEqual(self.bodies, [])

    def test_http_failure_preserves_cli_exit_and_dispatched_output(self):
        self.response_status = 503
        dispatched = mock.Mock()
        dispatched.to_dict.return_value = {"task_id": "dispatched-task", "job_id": "job-1"}
        for errors, expected in (((), 0), (({"kind": "failed", "task_id": "other"},), 1)):
            report = scout.ScoutReport(
                NOW, False, PlanResult((), {}, (), NOW), (dispatched,), (), errors,
            )
            with mock.patch.object(cli, "_queue", return_value=(self.cfg, self.queue)), \
                 mock.patch.object(scout, "_run_locked", return_value=report), \
                 mock.patch.object(cli, "_json") as output:
                result = isolated_cli(["scout", "--json", "--now", str(NOW)])
            self.assertEqual(result, expected)
            self.assertEqual(output.call_args.args[0], report.to_dict())
        self.assertEqual(len(self.bodies), 1)
        self.tick(NOW + 1)
        self.assertEqual(len(self.bodies), 1)

    def test_storage_failure_and_scout_exception_keep_original_exit(self):
        with mock.patch.object(self.queue, "reserve_scout_notice", side_effect=OSError("unwritable")):
            self.tick()
        with mock.patch.object(cli, "_queue", return_value=(self.cfg, self.queue)), \
             mock.patch.object(scout, "_run_locked", side_effect=RuntimeError("secret exception")), \
             mock.patch.object(cli, "_json"):
            self.assertEqual(isolated_cli(["scout", "--json"]), 1)
        self.assertEqual(len(self.bodies), 1)
        self.assertIn("scout_failure", self.bodies[0])
        self.assertNotIn("secret", self.bodies[0])

    def test_unwritable_queue_db_sends_one_stuck_notice(self):
        # On 2026-10-13 a full root disk made every reservation fail, so no stuck notice went out.
        full = sqlite3.OperationalError("database or disk is full")
        with mock.patch.object(self.queue, "reserve_scout_notice", side_effect=full), \
             mock.patch.object(notifications, "_post_ntfy") as post:
            self.tick()
            self.tick(NOW + 600)
            self.tick(NOW + 1200, status=0)

        post.assert_called_once()
        message = post.call_args.args[1]
        self.assertTrue(message.startswith(
            "Bonus Drain scout stuck: queue db unwritable (database or disk is full)."
        ), message)
        self.assertIn("Kinds: storage_unwritable.", message)

    def test_writable_queue_db_clears_the_storage_marker_and_recovers(self):
        full = sqlite3.OperationalError("database or disk is full")
        with mock.patch.object(self.queue, "reserve_scout_notice", side_effect=full):
            self.tick()
        self.assertEqual(len(self.bodies), 1)
        self.assertTrue(list(self.runtime_dir.iterdir()))

        self.tick(NOW + 600, status=0)

        self.assertEqual(list(self.runtime_dir.iterdir()), [])
        self.assertEqual(len(self.bodies), 2)
        self.assertIn("recovered", self.bodies[1])
        self.assertIn("storage_unwritable", self.bodies[1])
        with mock.patch.object(self.queue, "reserve_scout_notice", side_effect=full):
            self.tick(NOW + 1200)
        self.assertEqual(len(self.bodies), 3)

    def test_tick_lock_orders_health_changes_until_notification_finishes(self):
        self.tick()
        entered = threading.Event()
        release = threading.Event()
        healthy = scout.ScoutReport(NOW + 1, False, PlanResult((), {}, (), NOW + 1), (), (), ())
        results = []

        def delayed_send(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test notification timed out")
            return mock.MagicMock()

        with mock.patch.object(scout, "_run_locked", return_value=healthy) as run, \
             mock.patch.object(notifications, "urlopen", side_effect=delayed_send):
            first = threading.Thread(target=lambda: results.append(scout.run_once(self.cfg, self.queue, now_epoch=NOW + 1)))
            first.start()
            try:
                self.assertTrue(entered.wait(5))
                second = scout.run_once(self.cfg, self.queue, now_epoch=NOW + 2)
                self.assertEqual(second.skipped["reason"], "tick_in_progress")
                run.assert_called_once()
            finally:
                release.set()
                first.join(5)
            self.assertFalse(first.is_alive())
        self.assertEqual(results, [healthy])

    def test_config_accepts_optional_topic_and_rejects_credentials(self):
        raw = _raw_config(self.root, scout_ntfy_url=self.url)
        self.assertEqual(config.validate_config(raw).scout_ntfy_url, self.url)
        for url in ("https://user:password@ntfy.sh/topic", "file:///tmp/topic", "https://ntfy.sh/", "https://ntfy.sh/topic?secret=value"):
            with self.subTest(url=url), self.assertRaises(config.ConfigError):
                config.validate_config(dict(raw, scout_ntfy_url=url))

    def test_real_scout_cli_usage_outage_then_recovery(self):
        raw = _raw_config(self.root, scout_ntfy_url=self.url,
                          host_load_gate={"enabled": False})
        path = self.root / "config.json"
        path.write_text(json.dumps(raw))
        cfg = config.load_config(path)
        usage.write_cache(cfg.usage_cache_dir / "auth-files.json", {
            "captured_at": NOW,
            "accounts": [{"id": "alpha-account", "provider_id": "alpha", "unmetered": False}],
        })

        def tick(now, fresh=False):
            snapshot = usage.UsageSnapshot(
                "alpha", "alpha-account", now,
                {"seven_day": {"used_percent": 20, "resets_at": now + 1000}},
                fresh=fresh, error=None if fresh else "proxy usage unavailable",
            )
            usage.write_cache(usage.cache_path(cfg.usage_cache_dir, "alpha", "alpha-account"),
                              snapshot.cache_dict())
            result = subprocess.run(
                [str(SKILL_ROOT / "bin" / "bonus-drain"), "scout", "--json",
                 "--config", str(path), "--now", str(now)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["dispatched"], [])

        tick(NOW)
        tick(NOW + 3599)
        self.assertEqual(self.bodies, [])
        tick(NOW + 3600)
        tick(NOW + 90000)
        self.assertEqual(len(self.bodies), 1)
        self.assertIn("usage_unavailable", self.bodies[0])
        tick(NOW + 90001, fresh=True)
        tick(NOW + 90002, fresh=True)
        self.assertEqual(len(self.bodies), 2)
        self.assertIn("recovered", self.bodies[1])

    def test_real_scout_cli_alert_then_recovery_after_reconciliation(self):
        raw = _raw_config(self.root, scout_ntfy_url=self.url, host_load_gate={"enabled": False})
        path = self.root / "config.json"
        path.write_text(json.dumps(raw))
        queue = db.QueueDB(Path(raw["database"]))
        queue.add_task(reviewed(task("frozen-task")))
        attempt = queue.claim("frozen-task", ELIGIBILITY_KEY, "alpha", "alpha-account", provider_max_size="huge", now_epoch=NOW)
        queue.mark_ambiguous("frozen-task", ELIGIBILITY_KEY, detail="unknown launch secret")

        def run_tick(now):
            result = subprocess.run(
                [str(SKILL_ROOT / "bin" / "bonus-drain"), "scout", "--json", "--config", str(path), "--now", str(now)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            return result.returncode, json.loads(result.stdout)

        for now in (NOW, NOW + 1):
            code, report = run_tick(now)
            self.assertEqual(code, 1)
            self.assertEqual(report["dispatched"], [])
            self.assertEqual(report["blockers"][0]["kind"], "reconciliation_required")
        self.assertEqual(self.bodies, [
            "Bonus Drain scout stuck. Kinds: reconciliation_required. Tasks: frozen-task. Inspect: bonus-drain doctor --json."
        ])
        queue.record(
            "frozen-task", ELIGIBILITY_KEY, attempt_id=attempt.id, status="failed",
            summary="Local reconciliation proved no launch", now_epoch=NOW + 2,
            outcome={"reason": {"code": "verification_needed", "detail": "Local test reconciliation", "signature": "test:reconciliation", "queue_time_knowable": False}},
        )
        for now in (NOW + 3, NOW + 4):
            code, report = run_tick(now)
            self.assertEqual(code, 0)
            self.assertEqual(report["dispatched"], [])
        self.assertEqual(len(self.bodies), 2)
        self.assertEqual(self.bodies[1],
            "Bonus Drain scout recovered. Previous kinds: reconciliation_required. Tasks: frozen-task. Inspect: bonus-drain doctor --json.")


if __name__ == "__main__":
    unittest.main()
