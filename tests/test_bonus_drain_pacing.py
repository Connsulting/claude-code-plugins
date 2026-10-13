"""Regression contracts for Bonus Drain's remaining-headroom floor."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.readiness_fixture import reviewed

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain.config import (
    DispatchBinding,
    LimitConfig,
    ProviderConfig,
    RuntimeConfig,
)
from bonus_drain.planner import build_plan, limit_window
from bonus_drain.usage import UsageSnapshot

NOW = 2_000_000_000
HOUR = 3_600


class RemainingHeadroomFloorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limit = LimitConfig(
            "weekly", "plan", 604_800, 98, 259_200, 6,
            max_percent_per_window=0.5, estimated_percent_per_job=1.0,
            pacing_window_seconds=HOUR,
        )

    def admission(self, limit, reading, now):
        config = RuntimeConfig(
            schema_version=1, source_path=None, database=Path("/unused/queue.db"),
            record_command=("/bin/true",), secret_refs=(), adapters=(),
            providers=(ProviderConfig("plan", DispatchBinding("unused", "plan"), "huge"),),
            limits=(limit,), viewer={}, pr_exceptions=(), usage_max_age_seconds=3_600,
            cache_dir=Path("/unused/cache"), proxy_secret_ref=None,
        )
        snapshot = UsageSnapshot("plan", "subscription", now, {limit_window(limit): reading})
        result = build_plan(config, {("plan", "subscription"): snapshot},
                            eligible_count=20, now_epoch=now)
        gate = result.gates[0]
        return gate.batch_size, gate.resets_at, gate.reason

    def test_holds_half_a_point_per_remaining_hour(self) -> None:
        allowed, _reset, reason = self.admission(
            self.limit, {"used_percent": 79, "resets_at": NOW + 40 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertIsNotNone(reason)

    def test_reopens_once_remaining_hours_shrink_onto_the_same_headroom(self) -> None:
        allowed, _reset, reason = self.admission(
            self.limit, {"used_percent": 79, "resets_at": NOW + 38 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertIsNotNone(reason)
        allowed, _reset, reason = self.admission(
            self.limit, {"used_percent": 79, "resets_at": NOW + 36 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 1)
        self.assertIsNone(reason)

    def test_twenty_four_points_with_forty_eight_hours_left_is_at_the_floor(self) -> None:
        allowed, _reset, reason = self.admission(
            self.limit, {"used_percent": 74, "resets_at": NOW + 48 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertIsNotNone(reason)

    def test_one_point_above_the_floor_launches_one_job(self) -> None:
        allowed, _reset, reason = self.admission(
            self.limit, {"used_percent": 73, "resets_at": NOW + 48 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 1)
        self.assertIsNone(reason)

    def test_four_points_above_the_floor_launches_four_jobs(self) -> None:
        allowed, _reset, reason = self.admission(
            self.limit, {"used_percent": 70, "resets_at": NOW + 48 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 4)
        self.assertIsNone(reason)

    def test_surplus_above_six_is_capped_at_the_configured_batch(self) -> None:
        allowed, _reset, reason = self.admission(
            self.limit, {"used_percent": 25, "resets_at": NOW + 72 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 6)
        self.assertIsNone(reason)

    def test_fractional_surplus_below_one_job_does_not_launch(self) -> None:
        allowed, _reset, reason = self.admission(
            self.limit, {"used_percent": 73.6, "resets_at": NOW + 48 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertIsNotNone(reason)

    def test_zero_reserve_still_dumps_to_the_weekly_ceiling(self) -> None:
        unlimited = LimitConfig("weekly", "plan", 604_800, 98, 259_200, 6)
        allowed, _reset, reason = self.admission(
            unlimited, {"used_percent": 74, "resets_at": NOW + 48 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 6)
        self.assertIsNone(reason)

    def test_nonzero_reserve_without_an_estimate_fails_closed(self) -> None:
        unestimated = LimitConfig(
            "weekly", "plan", 604_800, 98, 259_200, 6,
            max_percent_per_window=0.5, pacing_window_seconds=HOUR,
        )
        allowed, _reset, reason = self.admission(
            unestimated, {"used_percent": 25, "resets_at": NOW + 72 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertIsNotNone(reason)

    def test_weekly_ceiling_remains_a_hard_gate(self) -> None:
        allowed, _reset, reason = self.admission(
            self.limit, {"used_percent": 98, "resets_at": NOW + 40 * HOUR}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertIsNotNone(reason)


class SameProviderPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        from bonus_drain import db

        from tests.test_bonus_drain_scout_inflight import _multi_account_config
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = _multi_account_config(db.QueueDB(root / "queue.db"), root / "cache", root / "unused")

    def plan(self, personal: float, business: float, **kwargs):
        from tests.test_bonus_drain_scout_inflight import _multi_snapshots
        return build_plan(self.config, _multi_snapshots(personal, business, **kwargs),
                          eligible_count=20, now_epoch=NOW)

    def test_positive_mean_includes_negative_subscription_and_sums_job_budget(self) -> None:
        result = self.plan(80, 65)
        self.assertEqual([(b.provider_id, b.account_id, b.batch_size) for b in result.batches],
                         [("alpha", "alpha", 5)])
        gate = next(g for g in result.to_dict()["gates"] if g["provider_id"] == "alpha")
        self.assertTrue(gate["open"])
        self.assertEqual(gate["surplus"], 2.5)
        readings = {row["account_id"]: row for row in result.to_dict()["subscriptions"]
                    if row["provider_id"] == "alpha"}
        self.assertEqual(set(readings), {"alpha-personal", "alpha-business"})
        self.assertEqual(readings["alpha-personal"]["pacing"]["alpha-weekly"]["surplus"], -5)
        self.assertEqual(readings["alpha-business"]["pacing"]["alpha-weekly"]["surplus"], 10)
        self.assertEqual(readings["alpha-personal"]["pacing"]["alpha-weekly"]["reserve"], 20)
        self.assertEqual(readings["alpha-personal"]["limits"]["seven_day"]["used_percent"], 80)
        self.assertEqual(result.batches[0].eligibility_key, f"alpha/alpha-weekly/{NOW + 40 * HOUR}")

    def test_zero_and_negative_means_close_whole_pool(self) -> None:
        for personal, business in ((80, 70), (80, 75)):
            with self.subTest(personal=personal, business=business):
                result = self.plan(personal, business)
                self.assertEqual(result.batches, ())
                self.assertIn(("alpha", "alpha"), result.closed)
                gate = next(g for g in result.gates if g.provider_id == "alpha")
                self.assertFalse(gate.open)
                self.assertEqual(sum(row["provider_id"] == "alpha"
                                     for row in result.to_dict()["subscriptions"]), 2)

    def test_earliest_reset_sets_pool_cycle_and_sum_is_capped_at_batch_size(self) -> None:
        result = self.plan(70, 70, personal_reset=NOW + 36 * HOUR,
                           business_reset=NOW + 40 * HOUR)
        self.assertEqual(len(result.batches), 1)
        self.assertEqual(result.batches[0].resets_at, NOW + 36 * HOUR)
        self.assertEqual(result.batches[0].batch_size, 6)
        self.assertEqual(result.batches[0].eligibility_key, f"alpha/alpha-weekly/{NOW + 36 * HOUR}")

    def test_stale_sibling_closes_whole_pool_and_fresh_recovery_reopens_it(self) -> None:
        from tests.test_bonus_drain_scout_inflight import _multi_snapshots
        records = _multi_snapshots(70, 70)
        records[("alpha", "alpha-personal")] = replace(
            records[("alpha", "alpha-personal")], captured_at=NOW - 3_601,
        )
        result = build_plan(self.config, records, eligible_count=20, now_epoch=NOW)
        self.assertEqual(result.batches, ())
        self.assertIn("stale", result.closed[("alpha", "alpha")])
        gate = next(g for g in result.gates if g.provider_id == "alpha")
        self.assertFalse(gate.open)
        self.assertEqual(sum(row["provider_id"] == "alpha"
                             for row in result.to_dict()["subscriptions"]), 2)
        self.assertEqual(self.plan(70, 70).batches[0].account_id, "alpha")

    def test_invalid_or_before_lead_sibling_closes_whole_pool(self) -> None:
        from tests.test_bonus_drain_scout_inflight import _multi_snapshots
        for reading in ({"used_percent": None, "resets_at": NOW + 40 * HOUR},
                        {"used_percent": 10, "resets_at": NOW + 80 * HOUR}):
            with self.subTest(reading=reading):
                records = _multi_snapshots(70, 70)
                records[("alpha", "alpha-personal")] = replace(
                    records[("alpha", "alpha-personal")], limits={"seven_day": reading},
                )
                result = build_plan(self.config, records, eligible_count=20, now_epoch=NOW)
                self.assertEqual(result.batches, ())
                self.assertIn(("alpha", "alpha"), result.closed)

    def test_multiple_limits_keep_tightest_pool_budget(self) -> None:
        from bonus_drain.config import LimitConfig

        from tests.test_bonus_drain_scout_inflight import _multi_snapshots
        short = LimitConfig("alpha-short", "alpha", 18_000, 80, 20_000, 6,
                            estimated_percent_per_job=1)
        cfg = replace(self.config, limits=(*self.config.limits, short))
        records = _multi_snapshots(70, 70)
        for key in (("alpha", "alpha-personal"), ("alpha", "alpha-business")):
            records[key] = replace(records[key], limits={**records[key].limits,
                "five_hour": {"used_percent": 79, "resets_at": NOW + 1_000}})
        result = build_plan(cfg, records, eligible_count=20, now_epoch=NOW)
        self.assertEqual(result.batches[0].batch_size, 2)
        self.assertEqual(result.batches[0].surplus, 1)
        self.assertEqual(result.batches[0].eligibility_key, f"alpha/alpha-short/{NOW + 1_000}")

    def test_unmetered_pool_uses_configured_cap_and_hour_cycle(self) -> None:
        from tests.test_bonus_drain_scout_inflight import _multi_snapshots
        records = {key: replace(row, unmetered=True, limits={})
                   for key, row in _multi_snapshots(70, 70).items() if key[0] == "alpha"}
        cfg = replace(self.config, unmetered_max_inflight=3)
        result = build_plan(cfg, records, eligible_count=20, now_epoch=NOW)
        self.assertEqual(result.batches[0].batch_size, 3)
        self.assertTrue(result.batches[0].unmetered)
        self.assertEqual(result.batches[0].account_id, "alpha")
        self.assertEqual(result.batches[0].eligibility_key, f"alpha/unmetered/{NOW // HOUR * HOUR}")
        self.assertIsNone(result.batches[0].to_dict()["resets_at"])

    def test_mixed_metering_and_no_inventory_fail_closed(self) -> None:
        from tests.test_bonus_drain_scout_inflight import _multi_snapshots
        records = _multi_snapshots(70, 70)
        records[("alpha", "alpha-business")] = replace(records[("alpha", "alpha-business")],
                                                        unmetered=True, limits={})
        self.assertEqual(build_plan(self.config, records, eligible_count=20, now_epoch=NOW).batches, ())
        absent = build_plan(self.config, {}, eligible_count=20, now_epoch=NOW)
        self.assertEqual(absent.batches, ())
        self.assertEqual(absent.closed[("alpha", "alpha")], "no proxy subscriptions")


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
