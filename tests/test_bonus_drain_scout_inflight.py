"""Regression contract for per-provider Bonus Drain in-flight caps."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.readiness_fixture import proxy_fixture, reviewed

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import config as config_module
from bonus_drain import db, scout, usage

NOW = 2_000_000_000
HOUR = 3_600


def _task(task_id: str, *providers: str) -> dict[str, object]:
    return {
        "id": task_id,
        "title": task_id,
        "kind": "oneoff",
        "priority": 1,
        "cwd": "/tmp",
        "goal": f"run {task_id}",
        "active": True,
        "allowed_providers": list(providers) if providers else None,
    }


def _limit(provider_id: str, ceiling: float = 95) -> config_module.LimitConfig:
    return config_module.LimitConfig(
        f"{provider_id}-weekly", provider_id, 604800, ceiling, 259200, 6,
        max_percent_per_window=0.5, estimated_percent_per_job=1.0,
        pacing_window_seconds=HOUR,
    )


def _two_provider_config(queue: db.QueueDB, cache: Path) -> config_module.RuntimeConfig:
    router = config_module.AdapterConfig("router", "t3-thread", ("/bin/true",))
    return proxy_fixture(config_module.RuntimeConfig(schema_version=1, source_path=None, database=queue.path, record_command=("/bin/true",), secret_refs=(), adapters=(router,), providers=(
            config_module.ProviderConfig("alpha", config_module.DispatchBinding("router", "alpha"), frozenset()),
            config_module.ProviderConfig("beta", config_module.DispatchBinding("router", "beta"), frozenset()),
        ), limits=(_limit("alpha"), _limit("beta")), viewer={}, pr_exceptions=(), usage_max_age_seconds=3600, cache_dir=cache, proxy_secret_ref=None), (("alpha", "alpha-account"), ("beta", "beta-account"),))


def _open_snapshots() -> dict[tuple[str, str], usage.UsageSnapshot]:
    reset = NOW + 40 * HOUR
    return {
        ("alpha", "alpha-account"): usage.UsageSnapshot(
            "alpha", "alpha-account", NOW,
            {"seven_day": {"used_percent": 70, "resets_at": reset}},
        ),
        ("beta", "beta-account"): usage.UsageSnapshot(
            "beta", "beta-account", NOW,
            {"seven_day": {"used_percent": 70, "resets_at": reset}},
        ),
    }


def _multi_account_config(
    queue: db.QueueDB,
    cache: Path,
    active_path: Path,
    *,
    max_jobs: int | None = None,
) -> config_module.RuntimeConfig:
    base = _two_provider_config(queue, cache)
    return proxy_fixture(replace(base, limits=(_limit("alpha"), _limit("beta")), max_jobs=max_jobs),
                         (("alpha", "alpha-personal"), ("alpha", "alpha-business"), ("beta", "beta-account")))


def _multi_snapshots(
    personal_used: float,
    business_used: float,
    *,
    personal_reset: int | None = None,
    business_reset: int | None = None,
    beta_used: float = 95,
) -> dict[tuple[str, str], usage.UsageSnapshot]:
    personal_reset = personal_reset or NOW + 40 * HOUR
    business_reset = business_reset or NOW + 40 * HOUR
    return {
        ("alpha", "alpha-personal"): usage.UsageSnapshot(
            "alpha", "alpha-personal", NOW,
            {"seven_day": {
                "used_percent": personal_used, "resets_at": personal_reset,
            }},
        ),
        ("alpha", "alpha-business"): usage.UsageSnapshot(
            "alpha", "alpha-business", NOW,
            {"seven_day": {
                "used_percent": business_used, "resets_at": business_reset,
            }},
        ),
        ("beta", "beta-account"): usage.UsageSnapshot(
            "beta", "beta-account", NOW,
            {"seven_day": {
                "used_percent": beta_used, "resets_at": NOW + 40 * HOUR,
            }},
        ),
    }


class ScoutPoolSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "queue.db")
        self.queue.initialize()
        self.config = _multi_account_config(self.queue, self.root / "cache", self.root / "unused")
        self.queue.add_task(reviewed(_task("alpha-next", "alpha")))

    def tick(self, personal: float, business: float, **kwargs):
        with mock.patch.object(scout, "read_all", return_value=_multi_snapshots(personal, business, **kwargs)):
            return scout.plan_tick(self.config, self.queue, now_epoch=NOW, provider_holds=())

    def test_pool_selects_earliest_reset_with_headroom(self):
        tick = self.tick(70, 70, personal_reset=NOW + 36 * HOUR, business_reset=NOW + 40 * HOUR)
        self.assertEqual([b.account_id for b in tick.plan.batches], ["alpha-personal"])
        self.assertEqual(tick.plan.batches[0].resets_at, NOW + 36 * HOUR)

    def test_closed_sibling_does_not_close_pool(self):
        tick = self.tick(80, 70)
        self.assertEqual([b.account_id for b in tick.plan.batches], ["alpha-business"])

    def test_all_accounts_at_floor_close_pool(self):
        self.assertEqual(self.tick(80, 80).plan.batches, ())

    def test_running_sibling_consumes_capacity_without_blocking_pool(self):
        self.queue.add_task(reviewed(_task("running", "alpha")))
        self.queue.record("running", "alpha-business/weekly/123", status="dispatched", provider_id="alpha", account_id="alpha-business", cycle=123)
        tick = self.tick(70, 70, personal_reset=NOW + 36 * HOUR)
        self.assertEqual([b.account_id for b in tick.plan.batches], ["alpha-personal"])


class ScoutInflightCapTests(unittest.TestCase):
    def test_running_job_tightens_the_same_provider_cap_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = db.QueueDB(root / "queue.db")
            queue.initialize()
            queue.add_task(reviewed(_task("already-running", "alpha")))
            queue.add_task(reviewed(_task("alpha-one", "alpha")))
            queue.add_task(reviewed(_task("alpha-two", "alpha")))
            queue.add_task(reviewed(_task("beta-one", "beta")))
            queue.record(
                "already-running", "alpha-account/alpha-plan-weekly/2000014400",
                status="dispatched", provider_id="alpha", account_id="alpha-account",
                cycle=NOW + 40 * HOUR,
            )
            config = _two_provider_config(queue, root / "cache")
            snapshots = _open_snapshots()
            dispatched: list[str] = []

            def fake_dispatch(config, queue, **kwargs):
                dispatched.append(kwargs["task_id"])
                return mock.Mock(to_dict=lambda: {"task_id": kwargs["task_id"]})

            with (
                mock.patch.object(scout, 'read_all', return_value=snapshots),
                mock.patch.object(scout, 'dispatch', side_effect=fake_dispatch),
            ):
                report = scout.run_once(config, queue, now_epoch=NOW)

            self.assertEqual(report.errors, ())
            self.assertIn("beta-one", dispatched)
            self.assertNotIn("already-running", dispatched)
            alpha_launched = [task_id for task_id in dispatched if task_id.startswith("alpha")]
            self.assertCountEqual(alpha_launched, ["alpha-one", "alpha-two"])

    def test_surplus_of_four_with_two_inflight_launches_two_more(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = db.QueueDB(root / "queue.db")
            queue.initialize()
            queue.add_task(reviewed(_task("run-a", "alpha")))
            queue.add_task(reviewed(_task("run-b", "alpha")))
            queue.add_task(reviewed(_task("next-1", "alpha")))
            queue.add_task(reviewed(_task("next-2", "alpha")))
            queue.add_task(reviewed(_task("next-3", "alpha")))
            for task_id in ("run-a", "run-b"):
                queue.record(
                    task_id, "alpha-account/alpha-plan-weekly/2000014400",
                    status="dispatched", provider_id="alpha", account_id="alpha-account",
                    cycle=NOW + 40 * HOUR,
                )
            config = _two_provider_config(queue, root / "cache")
            snapshots = {
                ("alpha", "alpha-account"): usage.UsageSnapshot(
                    "alpha", "alpha-account", NOW,
                    {"seven_day": {"used_percent": 71, "resets_at": NOW + 40 * HOUR}},
                ),
                ("beta", "beta-account"): usage.UsageSnapshot(
                    "beta", "beta-account", NOW,
                    {"seven_day": {"used_percent": 95, "resets_at": NOW + 40 * HOUR}},
                ),
            }
            # remaining 24, floor 20, surplus 4. two inflight → launch 2.
            dispatched: list[str] = []

            def fake_dispatch(config, queue, **kwargs):
                dispatched.append(kwargs["task_id"])
                return mock.Mock(to_dict=lambda: {"task_id": kwargs["task_id"]})

            with (
                mock.patch.object(scout, 'read_all', return_value=snapshots),
                mock.patch.object(scout, 'dispatch', side_effect=fake_dispatch),
            ):
                report = scout.run_once(config, queue, now_epoch=NOW)

            self.assertEqual(report.errors, ())
            self.assertEqual(len(dispatched), 2)
            self.assertTrue(all(task_id.startswith("next-") for task_id in dispatched))


class ScoutMatchingAndGlobalCapTests(unittest.TestCase):
    def test_portable_work_fills_a_smaller_codex_surplus_instead_of_all_claude_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = db.QueueDB(root / "queue.db")
            queue.initialize()
            for index in range(6):
                queue.add_task(reviewed(_task(f"portable-{index}")))
            config = _two_provider_config(queue, root / "cache")
            snapshots = {
                ("alpha", "alpha-account"): usage.UsageSnapshot(
                    "alpha", "alpha-account", NOW,
                    {"seven_day": {"used_percent": 69, "resets_at": NOW + 40 * HOUR}},
                ),
                ("beta", "beta-account"): usage.UsageSnapshot(
                    "beta", "beta-account", NOW,
                    {"seven_day": {"used_percent": 73, "resets_at": NOW + 40 * HOUR}},
                ),
            }
            # remaining 40h, floor 20. alpha 95-69-20=6; beta 95-73-20=2.
            with mock.patch.object(scout, "read_all", return_value=snapshots):
                tick = scout.plan_tick(config, queue, now_epoch=NOW)

            self.assertEqual(len(tick.allocations[("alpha", "alpha-account")]), 4)
            self.assertEqual(len(tick.allocations[("beta", "beta-account")]), 2)

    def test_global_cap_leaves_room_across_providers_after_inflight(self) -> None:
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = db.QueueDB(root / "queue.db")
            queue.initialize()
            for index in range(6):
                queue.add_task(reviewed(_task(f"alpha-{index}", "alpha")))
                queue.add_task(reviewed(_task(f"beta-{index}", "beta")))
            config = replace(_two_provider_config(queue, root / "cache"), max_jobs=8)
            snapshots = {
                ("alpha", "alpha-account"): usage.UsageSnapshot(
                    "alpha", "alpha-account", NOW,
                    {"seven_day": {"used_percent": 69, "resets_at": NOW + 40 * HOUR}},
                ),
                ("beta", "beta-account"): usage.UsageSnapshot(
                    "beta", "beta-account", NOW,
                    {"seven_day": {"used_percent": 69, "resets_at": NOW + 40 * HOUR}},
                ),
            }
            dispatched: list[str] = []

            def fake_dispatch(config, queue, **kwargs):
                dispatched.append(kwargs["task_id"])
                return mock.Mock(to_dict=lambda: {"task_id": kwargs["task_id"]})

            with (
                mock.patch.object(scout, 'read_all', return_value=snapshots),
                mock.patch.object(scout, 'dispatch', side_effect=fake_dispatch),
            ):
                report = scout.run_once(config, queue, now_epoch=NOW)

            self.assertEqual(report.errors, ())
            self.assertEqual(len(dispatched), 8)
            self.assertEqual(sum(1 for task_id in dispatched if task_id.startswith("alpha")), 6)
            self.assertEqual(sum(1 for task_id in dispatched if task_id.startswith("beta")), 2)

    def test_omitted_global_cap_lets_each_provider_fill_its_own_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = db.QueueDB(root / "queue.db")
            queue.initialize()
            for index in range(6):
                queue.add_task(reviewed(_task(f"alpha-{index}", "alpha")))
                queue.add_task(reviewed(_task(f"beta-{index}", "beta")))
            config = _two_provider_config(queue, root / "cache")
            snapshots = {
                ("alpha", "alpha-account"): usage.UsageSnapshot(
                    "alpha", "alpha-account", NOW,
                    {"seven_day": {"used_percent": 69, "resets_at": NOW + 40 * HOUR}},
                ),
                ("beta", "beta-account"): usage.UsageSnapshot(
                    "beta", "beta-account", NOW,
                    {"seven_day": {"used_percent": 69, "resets_at": NOW + 40 * HOUR}},
                ),
            }
            dispatched: list[str] = []

            def fake_dispatch(config, queue, **kwargs):
                dispatched.append(kwargs["task_id"])
                return mock.Mock(to_dict=lambda: {"task_id": kwargs["task_id"]})

            with (
                mock.patch.object(scout, 'read_all', return_value=snapshots),
                mock.patch.object(scout, 'dispatch', side_effect=fake_dispatch),
            ):
                report = scout.run_once(config, queue, now_epoch=NOW)

            self.assertEqual(report.errors, ())
            self.assertEqual(len(dispatched), 12)
            self.assertEqual(sum(1 for task_id in dispatched if task_id.startswith("alpha")), 6)
            self.assertEqual(sum(1 for task_id in dispatched if task_id.startswith("beta")), 6)
