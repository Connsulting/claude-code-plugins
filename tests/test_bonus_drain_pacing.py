"""Regression contracts for the Bonus Drain declining-reserve planner."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain.config import LimitConfig  # noqa: E402
from bonus_drain.planner import _limit_admission  # noqa: E402


NOW = 2_000_000_000


class DecliningReservePacingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limit = LimitConfig(
            "weekly", "plan", 604_800, 98, 108_000, 6,
            max_percent_per_window=5, estimated_percent_per_job=0.75,
            pacing_window_seconds=18_000,
        )
        self.reset = NOW + 5 * 18_000

    def test_reaches_but_never_crosses_the_current_reserve_target(self) -> None:
        # 25 hours left means a 25% reserve. At 27% remaining, the 98% ceiling
        # leaves only 25 points of headroom, so no estimated task may launch.
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 73, "resets_at": self.reset}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertEqual(reason, "at reserve target for limit weekly")

    def test_large_headroom_can_still_fill_the_configured_batch(self) -> None:
        # With 40% remaining, 13 points sit above the current 25% reserve;
        # the configured six-way concurrency cap, not the reserve, binds.
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 60, "resets_at": self.reset}, NOW,
        )
        self.assertEqual(allowed, 6)
        self.assertIsNone(reason)

    def test_small_gap_above_target_limits_the_batch_to_safe_estimates(self) -> None:
        # Two points above target can pay for only two 0.75-point jobs.
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 71, "resets_at": self.reset}, NOW,
        )
        self.assertEqual(allowed, 2)
        self.assertIsNone(reason)

    def test_nonzero_reserve_without_an_estimate_fails_closed(self) -> None:
        unestimated = LimitConfig(
            "weekly", "plan", 604_800, 98, 108_000, 6,
            max_percent_per_window=5, pacing_window_seconds=18_000,
        )
        allowed, _reset, reason = _limit_admission(
            unestimated, {"used_percent": 60, "resets_at": self.reset}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertEqual(reason, "cannot enforce reserve without a job estimate for limit weekly")
