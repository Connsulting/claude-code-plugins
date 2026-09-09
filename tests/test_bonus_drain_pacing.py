"""Regression contracts for Bonus Drain's remaining-headroom floor."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path



REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain.config import LimitConfig  # noqa: E402
from bonus_drain.planner import _limit_admission, build_plan  # noqa: E402


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


def _two_account_config() -> SimpleNamespace:
    from bonus_drain import config as config_module

    router = config_module.AdapterConfig("router", "agent-router", ("/bin/true",))
    return config_module.RuntimeConfig(
        schema_version=1,
        source_path=None,
        database=Path("/tmp/unused.db"),
        record_command=("/bin/true",),
        secret_refs=(),
        adapters=(router,),
        providers=(
            config_module.ProviderConfig(
                "claude", config_module.DispatchBinding("router", "claude"),
                frozenset(), "multi",
            ),
        ),
        plans=(
            config_module.PlanConfig("claude-personal-plan", "claude"),
            config_module.PlanConfig("claude-business-plan", "claude"),
        ),
        accounts=(
            config_module.AccountConfig("claude-personal", "claude", "claude-personal-plan"),
            config_module.AccountConfig("claude-business", "claude", "claude-business-plan"),
        ),
        limits=(
            config_module.LimitConfig(
                "claude-personal-weekly", "claude-personal-plan", 604_800, 95, 259_200, 6,
                max_percent_per_window=0.5, estimated_percent_per_job=1.0,
                pacing_window_seconds=HOUR,
            ),
            config_module.LimitConfig(
                "claude-business-weekly", "claude-business-plan", 604_800, 99, 259_200, 6,
                max_percent_per_window=0.5, estimated_percent_per_job=1.0,
                pacing_window_seconds=HOUR,
            ),
        ),
        viewer={},
        pr_exceptions=(),
        usage_max_age_seconds=3600,
        cache_dir=Path("/tmp"),
    )


class SameProviderAccountSelectionTests(unittest.TestCase):
    def test_higher_surplus_account_wins_the_provider_tick(self) -> None:
        from bonus_drain.usage import UsageSnapshot

        config = _two_account_config()
        snapshots = {
            ("claude", "claude-personal"): UsageSnapshot(
                "claude", "claude-personal", NOW,
                {"claude-personal-weekly": {"used_percent": 80, "resets_at": NOW + 40 * HOUR}},
            ),
            ("claude", "claude-business"): UsageSnapshot(
                "claude", "claude-business", NOW,
                {"claude-business-weekly": {"used_percent": 70, "resets_at": NOW + 40 * HOUR}},
            ),
        }
        # personal remaining 15, floor 20 → closed. business remaining 29, floor 20 → 9 surplus.
        plan = build_plan(
            config, snapshots,
            eligible_count={("claude", "claude-personal"): 4, ("claude", "claude-business"): 4},
            now_epoch=NOW,
        )
        self.assertEqual([batch.account_id for batch in plan.batches], ["claude-business"])
        self.assertEqual(plan.batches[0].batch_size, 4)
        self.assertEqual(plan.closed[("claude", "claude-personal")], "at reserve target for limit claude-personal-weekly")

    def test_both_above_floor_keeps_the_larger_surplus(self) -> None:
        from bonus_drain.usage import UsageSnapshot

        config = _two_account_config()
        snapshots = {
            ("claude", "claude-personal"): UsageSnapshot(
                "claude", "claude-personal", NOW,
                {"claude-personal-weekly": {"used_percent": 70, "resets_at": NOW + 40 * HOUR}},
            ),
            ("claude", "claude-business"): UsageSnapshot(
                "claude", "claude-business", NOW,
                {"claude-business-weekly": {"used_percent": 60, "resets_at": NOW + 40 * HOUR}},
            ),
        }
        # personal remaining 25-20=5; business remaining 39-20=19.
        plan = build_plan(
            config, snapshots,
            eligible_count={("claude", "claude-personal"): 6, ("claude", "claude-business"): 6},
            now_epoch=NOW,
        )
        self.assertEqual([batch.account_id for batch in plan.batches], ["claude-business"])
        self.assertEqual(plan.batches[0].batch_size, 6)
        self.assertEqual(plan.closed[("claude", "claude-personal")], "queued behind claude-business")
