"""Regression contracts for Bonus Drain's weekly-ceiling admission."""

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


class WeeklyCeilingAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limit = LimitConfig("weekly", "plan", 604_800, 98, 108_000, 6)
        self.reset = NOW + 5 * 18_000

    def test_headroom_is_not_held_for_five_hour_pacing(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 73, "resets_at": self.reset}, NOW,
        )
        self.assertEqual(allowed, 6)
        self.assertIsNone(reason)

    def test_large_headroom_can_fill_the_configured_batch(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 60, "resets_at": self.reset}, NOW,
        )
        self.assertEqual(allowed, 6)
        self.assertIsNone(reason)

    def test_small_headroom_still_uses_the_configured_batch(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 71, "resets_at": self.reset}, NOW,
        )
        self.assertEqual(allowed, 6)
        self.assertIsNone(reason)

    def test_weekly_ceiling_remains_a_hard_gate(self) -> None:
        allowed, _reset, reason = _limit_admission(
            self.limit, {"used_percent": 98, "resets_at": self.reset}, NOW,
        )
        self.assertEqual(allowed, 0)
        self.assertEqual(reason, "at ceiling for limit weekly")
