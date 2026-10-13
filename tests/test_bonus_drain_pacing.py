"""Regression contracts for Bonus Drain's remaining-headroom floor."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from tests.readiness_fixture import reviewed

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain.config import LimitConfig
from bonus_drain.planner import _limit_admission, build_plan

NOW = 2_000_000_000
HOUR = 3_600


class RemainingHeadroomFloorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limit = LimitConfig(
            "weekly", "plan", 604_800, 98, 259_200, 6,
            max_percent_per_window=0.5, estimated_percent_per_job=1.0,
            pacing_window_seconds=HOUR,
        )

    def test_holds_half_a_point_per_remaining_hour(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 79, "resets_at": NOW + 40 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertEqual(reason, "at reserve target for limit weekly")

    def test_reopens_once_remaining_hours_shrink_onto_the_same_headroom(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 79, "resets_at": NOW + 38 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertEqual(reason, "at reserve target for limit weekly")
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 79, "resets_at": NOW + 36 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 1)
        self.assertIsNone(reason)

    def test_twenty_four_points_with_forty_eight_hours_left_is_at_the_floor(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 74, "resets_at": NOW + 48 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertEqual(reason, "at reserve target for limit weekly")

    def test_one_point_above_the_floor_launches_one_job(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 73, "resets_at": NOW + 48 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 1)
        self.assertIsNone(reason)

    def test_four_points_above_the_floor_launches_four_jobs(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 70, "resets_at": NOW + 48 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 4)
        self.assertIsNone(reason)

    def test_surplus_above_six_is_capped_at_the_configured_batch(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 25, "resets_at": NOW + 72 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 6)
        self.assertIsNone(reason)

    def test_fractional_surplus_below_one_job_does_not_launch(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 73.6, "resets_at": NOW + 48 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertEqual(reason, "remaining budget is below one job estimate for limit weekly")

    def test_zero_reserve_still_dumps_to_the_weekly_ceiling(self) -> None:
        unlimited = LimitConfig("weekly", "plan", 604_800, 98, 259_200, 6)
        allowed, _reset, reason = _limit_admission(
            unlimited, {"used_percent": 74, "resets_at": NOW + 48 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 6)
        self.assertIsNone(reason)

    def test_nonzero_reserve_without_an_estimate_fails_closed(self) -> None:
        unestimated = LimitConfig(
            "weekly", "plan", 604_800, 98, 259_200, 6,
            max_percent_per_window=0.5, pacing_window_seconds=HOUR,
        )
        allowed, _reset, reason = _limit_admission(
            unestimated, {"used_percent": 25, "resets_at": NOW + 72 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertEqual(reason, "cannot enforce reserve without a job estimate for limit weekly")

    def test_weekly_ceiling_remains_a_hard_gate(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 98, "resets_at": NOW + 40 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertEqual(reason, "at ceiling for limit weekly")


class SameProviderPoolTests(unittest.TestCase):
    def test_shared_threshold_contains_failure_to_one_account(self):
        import tempfile

        from bonus_drain import db, planner

        from tests.test_bonus_drain_scout_inflight import (
            _multi_account_config,
            _multi_snapshots,
        )
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            config = _multi_account_config(db.QueueDB(root / "queue.db"), root / "cache", root / "unused")
            result = build_plan(config, _multi_snapshots(80, 70), eligible_count=4, now_epoch=NOW)
            self.assertEqual([batch.account_id for batch in result.batches], ["alpha-business"])
            self.assertEqual(result.closed[("alpha", "alpha-personal")], "at reserve target for limit alpha-weekly")
            final = planner.finalize_plan(config, result, eligible_count=4)
            self.assertEqual(final.batches[0].batch_size, 4)


class RecurringCycleEligibilityTests(unittest.TestCase):
    def test_weekly_task_cannot_rerun_on_the_same_reset_cycle(self) -> None:
        import tempfile

        from bonus_drain import db as bonus_db

        reset = NOW + 40 * HOUR
        with tempfile.TemporaryDirectory() as temporary:
            queue = bonus_db.QueueDB(Path(temporary) / "queue.db")
            queue.initialize()
            queue.add_task(reviewed({
                "id": "weekly-job",
                "title": "Weekly job",
                "kind": "recurring",
                "cadence": "weekly",
                "priority": 2,
                "cwd": "/tmp",
                "goal": "run weekly",
                "active": True,
            }))
            queue.record(
                "weekly-job", f"alpha/weekly/{reset}",
                status="done", provider_id="alpha", account_id="alpha-account",
                cycle=reset, ts="2020-01-01T00:00:00Z",
            )
            self.assertEqual(queue.eligible_tasks(reset), [])
            later = queue.eligible_tasks(reset + 7 * 24 * HOUR)
            self.assertEqual([task.id for task in later], ["weekly-job"])
