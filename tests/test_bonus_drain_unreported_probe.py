"""A provider that omits its weekly percent gets one probe run per period, never more.

Grok's billing API leaves ``creditUsagePercent`` unset at the start of every weekly period
and first reports it as an integer step of 1.0. The normalizer can opt in to reporting that
state as the first step with ``unreported_since``. These contracts prove the planner admits
at most one job on such a reading, and the scout closes the account once Bonus Drain has
dispatched anything on it since the period started, so unknown capacity is never drained.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.readiness_fixture import reviewed
from tests.test_bonus_drain_scout_inflight import (
    HOUR,
    NOW,
    _limit,
    _open_snapshots,
    _task,
    _two_provider_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import config as config_module
from bonus_drain import db, scout, usage
from bonus_drain.planner import _limit_admission

PERIOD_START = NOW - 47 * HOUR
RESET = PERIOD_START + 604_800
UNREPORTED = {"used_percent": 1.0, "resets_at": RESET, "unreported_since": PERIOD_START}


def _grok_shaped_limit() -> config_module.LimitConfig:
    """The live Grok limit: the whole week is drainable, three jobs per batch."""

    return config_module.LimitConfig(
        "alpha-plan-weekly", "alpha-plan", 604_800, 95, 604_800, 3,
        max_percent_per_window=0.5, estimated_percent_per_job=1.0,
        pacing_window_seconds=HOUR,
    )


class UnreportedReadingCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account = config_module.AccountConfig("alpha-account", "alpha", "alpha-plan")

    def raw(self, reading: dict) -> dict:
        return {
            "provider_id": "alpha", "account_id": "alpha-account", "captured_at": NOW,
            "limits": {"alpha-plan-weekly": reading},
        }

    def test_unreported_since_survives_normalization(self) -> None:
        snapshot = usage.normalize_snapshot(self.raw(dict(UNREPORTED)), self.account, now_epoch=NOW)
        self.assertEqual(snapshot.limits["alpha-plan-weekly"]["unreported_since"], PERIOD_START)

    def test_unreported_since_after_the_reset_is_rejected(self) -> None:
        with self.assertRaises(usage.AdapterFailure):
            usage.normalize_snapshot(
                self.raw({**UNREPORTED, "unreported_since": RESET}), self.account, now_epoch=NOW,
            )

    def test_unreported_since_must_be_an_epoch(self) -> None:
        with self.assertRaises(usage.AdapterFailure):
            usage.normalize_snapshot(
                self.raw({**UNREPORTED, "unreported_since": True}), self.account, now_epoch=NOW,
            )


class UnreportedReadingResetMergeTests(unittest.TestCase):
    def test_a_separate_reset_adapter_keeps_the_unreported_flag(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            queue = db.QueueDB(root / "queue.db")
            base = _two_provider_config(queue, root / "cache")
            usage_adapter = config_module.AdapterConfig("alpha-usage", "usage", ("/bin/true",))
            reset_adapter = config_module.AdapterConfig("alpha-reset", "reset", ("/bin/true",))
            account = config_module.AccountConfig(
                "alpha-account", "alpha", "alpha-plan",
                usage_adapter_id="alpha-usage", reset_adapter_id="alpha-reset",
            )
            config = replace(
                base,
                adapters=(*base.adapters, usage_adapter, reset_adapter),
                accounts=(account,),
                limits=(_grok_shaped_limit(),),
            )

            def adapter_call(adapter, variables, *, config):
                if adapter.id == "alpha-reset":
                    return {"limits": {"alpha-plan-weekly": {"resets_at": RESET}}}
                return {
                    "provider_id": "alpha", "account_id": "alpha-account", "captured_at": NOW,
                    "limits": {"alpha-plan-weekly": {
                        "used_percent": 1.0, "unreported_since": PERIOD_START,
                    }},
                }

            snapshot = usage.refresh_all(
                config, root / "cache", adapter_call=adapter_call, now_epoch=NOW,
            )[("alpha", "alpha-account")]
        self.assertTrue(snapshot.fresh, snapshot.error)
        self.assertEqual(snapshot.limits["alpha-plan-weekly"], UNREPORTED)


class UnreportedReadingAdmissionTests(unittest.TestCase):
    def test_an_unreported_reading_admits_exactly_one_job(self) -> None:
        limit = _grok_shaped_limit()
        reported, _reset, reason = _limit_admission(
            limit, {"used_percent": 1.0, "resets_at": RESET}, NOW,
        )
        self.assertIsNone(reason)
        self.assertGreater(reported, 1, "fixture must have room for more than one job")
        allowed, _reset, reason = _limit_admission(limit, dict(UNREPORTED), NOW)
        self.assertIsNone(reason)
        self.assertEqual(allowed, 1)


class UnreportedProbeScoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "queue.db")
        self.queue.initialize()
        base = _two_provider_config(self.queue, self.root / "cache")
        self.config = replace(base, limits=(_grok_shaped_limit(), _limit("beta-plan")))
        self.queue.add_task(reviewed(_task("alpha-next", "alpha")))
        self.queue.add_task(reviewed(_task("alpha-later", "alpha")))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def tick(self) -> scout.TickPlan:
        snapshots = _open_snapshots()
        snapshots[("alpha", "alpha-account")] = usage.UsageSnapshot(
            "alpha", "alpha-account", NOW, {"alpha-plan-weekly": dict(UNREPORTED)},
        )
        with mock.patch.object(scout, "read_all", return_value=snapshots):
            return scout.plan_tick(self.config, self.queue, now_epoch=NOW, provider_holds=())

    def alpha_batches(self, tick: scout.TickPlan) -> list:
        return [batch for batch in tick.plan.batches if batch.account_id == "alpha-account"]

    def test_first_tick_of_the_period_opens_one_probe_slot(self) -> None:
        batches = self.alpha_batches(self.tick())
        self.assertEqual([batch.batch_size for batch in batches], [1])

    def test_a_run_since_the_period_started_closes_the_account(self) -> None:
        self.queue.add_task(reviewed(_task("beta-next", "beta")))
        self.queue.record(
            "alpha-next", f"alpha-account/alpha-plan-weekly/{RESET}",
            status="dispatched", provider_id="alpha", account_id="alpha-account", cycle=RESET,
            now_epoch=PERIOD_START + HOUR,
        )
        tick = self.tick()
        self.assertEqual(self.alpha_batches(tick), [])
        self.assertEqual(
            tick.plan.closed[("alpha", "alpha-account")],
            "weekly usage still unreported after this period's probe run",
        )
        self.assertEqual(
            [batch.account_id for batch in tick.plan.batches], ["beta-account"],
            "the sibling provider keeps its own gate",
        )

    def test_a_run_from_the_previous_period_does_not_spend_the_probe(self) -> None:
        self.queue.add_task(reviewed(_task("alpha-third", "alpha")))
        self.queue.record(
            "alpha-next", f"alpha-account/alpha-plan-weekly/{PERIOD_START}",
            status="skipped", provider_id="alpha", account_id="alpha-account",
            cycle=PERIOD_START, now_epoch=PERIOD_START - HOUR,
        )
        batches = self.alpha_batches(self.tick())
        self.assertEqual([batch.batch_size for batch in batches], [1])

    def test_a_run_on_a_sibling_provider_does_not_spend_the_probe(self) -> None:
        self.queue.add_task(reviewed(_task("beta-done", "beta")))
        self.queue.record(
            "beta-done", f"beta-account/beta-plan-weekly/{NOW + 40 * HOUR}",
            status="skipped", provider_id="beta", account_id="beta-account",
            cycle=NOW + 40 * HOUR, now_epoch=PERIOD_START + HOUR,
        )
        batches = self.alpha_batches(self.tick())
        self.assertEqual([batch.batch_size for batch in batches], [1])


if __name__ == "__main__":
    unittest.main()
