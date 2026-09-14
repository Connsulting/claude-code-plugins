"""Calendar-window contracts for weekly Bonus Drain work."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import db  # noqa: E402


def epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def timestamp(value: str) -> str:
    return datetime.fromtimestamp(epoch(value), timezone.utc).isoformat()


class WeeklyRecurrenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.queue = db.QueueDB(
            Path(self.temporary.name) / "queue.db",
            recurrence_timezone="America/New_York",
        )
        self.queue.initialize()
        self.queue.add_task({
            "id": "weekly-job",
            "title": "Weekly job",
            "kind": "recurring",
            "cadence": "weekly",
            "priority": 2,
            "cwd": "/tmp",
            "goal": "run weekly",
            "active": True,
        })

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def eligible(self, value: str, *, automatic: bool) -> bool:
        return bool(self.queue.eligible_tasks(
            epoch(value), automatic=automatic, now_epoch=epoch(value),
        ))

    def test_automatic_weekly_work_runs_only_on_eastern_sunday(self) -> None:
        self.assertFalse(self.eligible("2026-09-12T23:59:59Z", automatic=True))
        self.assertFalse(self.eligible("2026-09-13T03:59:59Z", automatic=True))
        self.assertTrue(self.eligible("2026-09-13T04:00:00Z", automatic=True))
        self.assertTrue(self.eligible("2026-09-14T03:59:59Z", automatic=True))
        self.assertFalse(self.eligible("2026-09-14T04:00:00Z", automatic=True))

    def test_manual_run_outside_sunday_consumes_the_saturday_starting_week(self) -> None:
        self.assertTrue(self.eligible("2026-09-12T16:00:00Z", automatic=False))
        self.queue.record(
            "weekly-job",
            "alpha/manual/2026-W37",
            status="done",
            timestamp=timestamp("2026-09-12T16:00:00Z"),
        )

        self.assertFalse(self.eligible("2026-09-13T16:00:00Z", automatic=True))
        self.assertFalse(self.eligible("2026-09-13T16:00:00Z", automatic=False))
        self.assertFalse(self.eligible("2026-09-14T16:00:00Z", automatic=False))
        self.assertFalse(self.eligible("2026-09-14T16:00:00Z", automatic=True))
        self.assertFalse(self.eligible("2026-09-19T03:59:59Z", automatic=False))
        self.assertTrue(self.eligible("2026-09-19T04:00:00Z", automatic=False))
        self.assertFalse(self.eligible("2026-09-19T04:00:00Z", automatic=True))
        self.assertTrue(self.eligible("2026-09-20T16:00:00Z", automatic=True))

    def test_sunday_run_stays_spent_on_monday_and_friday(self) -> None:
        self.queue.record(
            "weekly-job", "alpha/manual/2026-W37", status="done",
            timestamp=timestamp("2026-09-13T16:00:00Z"),
        )
        for value in ("2026-09-14T16:00:00Z", "2026-09-18T16:00:00Z"):
            with self.subTest(value=value):
                self.assertFalse(self.eligible(value, automatic=False))
                self.assertFalse(self.eligible(value, automatic=True))

    def test_missed_sunday_does_not_carry_into_monday(self) -> None:
        self.assertTrue(self.eligible("2026-09-13T16:00:00Z", automatic=True))
        self.assertFalse(self.eligible("2026-09-14T16:00:00Z", automatic=True))
        self.assertTrue(self.eligible("2026-09-20T16:00:00Z", automatic=True))

    def test_atomic_automatic_claim_rechecks_the_sunday_window(self) -> None:
        with mock.patch.object(db.time, "time", return_value=epoch("2026-09-14T16:00:00Z")):
            self.assertFalse(self.queue.claim(
                "weekly-job",
                "alpha/weekly/2000000000",
                "alpha",
                None,
                automatic=True,
            ))
        with mock.patch.object(db.time, "time", return_value=epoch("2026-09-20T16:00:00Z")):
            self.assertTrue(self.queue.claim(
                "weekly-job",
                "alpha/weekly/2000604800",
                "alpha",
                None,
                automatic=True,
            ))

    def test_spring_dst_week_uses_local_midnight_boundaries(self) -> None:
        self.assertFalse(self.eligible("2026-03-08T04:59:59Z", automatic=True))
        self.assertTrue(self.eligible("2026-03-08T05:00:00Z", automatic=True))
        # The DST transition makes this Sunday window 23 elapsed hours.
        self.assertTrue(self.eligible("2026-03-09T03:59:59Z", automatic=True))
        self.assertFalse(self.eligible("2026-03-09T04:00:00Z", automatic=True))

    def test_failed_or_skipped_attempt_consumes_the_week(self) -> None:
        for status in ("failed", "skipped"):
            with self.subTest(status=status):
                queue = db.QueueDB(
                    Path(self.temporary.name) / f"{status}.db",
                    recurrence_timezone="America/New_York",
                )
                queue.initialize()
                queue.add_task({
                    "id": "weekly-job",
                    "title": "Weekly job",
                    "kind": "recurring",
                    "cadence": "weekly",
                    "priority": 2,
                    "cwd": "/tmp",
                    "goal": "run weekly",
                    "active": True,
                })
                queue.record(
                    "weekly-job",
                    f"alpha/manual/{status}",
                    status=status,
                    timestamp=timestamp("2026-09-13T12:00:00Z"),
                )
                self.assertEqual(queue.eligible_tasks(
                    epoch("2026-09-13T18:00:00Z"),
                    automatic=True,
                    now_epoch=epoch("2026-09-13T18:00:00Z"),
                ), [])


if __name__ == "__main__":
    unittest.main()
