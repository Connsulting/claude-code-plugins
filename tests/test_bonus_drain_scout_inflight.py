"""Regression contract for per-provider Bonus Drain in-flight caps."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import config as config_module, db, scout, usage  # noqa: E402


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


def _limit(plan_id: str, ceiling: float = 95) -> config_module.LimitConfig:
    return config_module.LimitConfig(
        f"{plan_id}-weekly", plan_id, 604800, ceiling, 259200, 6,
        max_percent_per_window=0.5, estimated_percent_per_job=1.0,
        pacing_window_seconds=HOUR,
    )


def _two_provider_config(queue: db.QueueDB, cache: Path) -> config_module.RuntimeConfig:
    router = config_module.AdapterConfig("router", "agent-router", ("/bin/true",))
    return config_module.RuntimeConfig(
        schema_version=1,
        source_path=None,
        database=queue.path,
        record_command=("/bin/true",),
        secret_refs=(),
        adapters=(router,),
        providers=(
            config_module.ProviderConfig(
                "alpha", config_module.DispatchBinding("router", "alpha"),
                frozenset(), "single",
            ),
            config_module.ProviderConfig(
                "beta", config_module.DispatchBinding("router", "beta"),
                frozenset(), "single",
            ),
        ),
        plans=(
            config_module.PlanConfig("alpha-plan", "alpha"),
            config_module.PlanConfig("beta-plan", "beta"),
        ),
        accounts=(
            config_module.AccountConfig("alpha-account", "alpha", "alpha-plan"),
            config_module.AccountConfig("beta-account", "beta", "beta-plan"),
        ),
        limits=(_limit("alpha-plan"), _limit("beta-plan")),
        viewer={},
        pr_exceptions=(),
        usage_max_age_seconds=3600,
        cache_dir=cache,
    )


def _open_snapshots() -> dict[tuple[str, str], usage.UsageSnapshot]:
    reset = NOW + 40 * HOUR
    return {
        ("alpha", "alpha-account"): usage.UsageSnapshot(
            "alpha", "alpha-account", NOW,
            {"alpha-plan-weekly": {"used_percent": 70, "resets_at": reset}},
        ),
        ("beta", "beta-account"): usage.UsageSnapshot(
            "beta", "beta-account", NOW,
            {"beta-plan-weekly": {"used_percent": 70, "resets_at": reset}},
        ),
    }


class ScoutInflightCapTests(unittest.TestCase):
    def test_running_job_tightens_the_same_provider_cap_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = db.QueueDB(root / "queue.db")
            queue.initialize()
            queue.add_task(_task("already-running", "alpha"))
            queue.add_task(_task("alpha-one", "alpha"))
            queue.add_task(_task("alpha-two", "alpha"))
            queue.add_task(_task("beta-one", "beta"))
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

            with mock.patch.object(scout, "read_all", return_value=snapshots):
                with mock.patch.object(scout, "dispatch", side_effect=fake_dispatch):
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
            queue.add_task(_task("run-a", "alpha"))
            queue.add_task(_task("run-b", "alpha"))
            queue.add_task(_task("next-1", "alpha"))
            queue.add_task(_task("next-2", "alpha"))
            queue.add_task(_task("next-3", "alpha"))
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
                    {"alpha-plan-weekly": {"used_percent": 71, "resets_at": NOW + 40 * HOUR}},
                ),
                ("beta", "beta-account"): usage.UsageSnapshot(
                    "beta", "beta-account", NOW,
                    {"beta-plan-weekly": {"used_percent": 95, "resets_at": NOW + 40 * HOUR}},
                ),
            }
            # remaining 24, floor 20, surplus 4. two inflight → launch 2.
            dispatched: list[str] = []

            def fake_dispatch(config, queue, **kwargs):
                dispatched.append(kwargs["task_id"])
                return mock.Mock(to_dict=lambda: {"task_id": kwargs["task_id"]})

            with mock.patch.object(scout, "read_all", return_value=snapshots):
                with mock.patch.object(scout, "dispatch", side_effect=fake_dispatch):
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
                queue.add_task(_task(f"portable-{index}"))
            config = _two_provider_config(queue, root / "cache")
            snapshots = {
                ("alpha", "alpha-account"): usage.UsageSnapshot(
                    "alpha", "alpha-account", NOW,
                    {"alpha-plan-weekly": {"used_percent": 69, "resets_at": NOW + 40 * HOUR}},
                ),
                ("beta", "beta-account"): usage.UsageSnapshot(
                    "beta", "beta-account", NOW,
                    {"beta-plan-weekly": {"used_percent": 73, "resets_at": NOW + 40 * HOUR}},
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
                queue.add_task(_task(f"alpha-{index}", "alpha"))
                queue.add_task(_task(f"beta-{index}", "beta"))
            config = replace(_two_provider_config(queue, root / "cache"), max_jobs=8)
            snapshots = {
                ("alpha", "alpha-account"): usage.UsageSnapshot(
                    "alpha", "alpha-account", NOW,
                    {"alpha-plan-weekly": {"used_percent": 69, "resets_at": NOW + 40 * HOUR}},
                ),
                ("beta", "beta-account"): usage.UsageSnapshot(
                    "beta", "beta-account", NOW,
                    {"beta-plan-weekly": {"used_percent": 69, "resets_at": NOW + 40 * HOUR}},
                ),
            }
            dispatched: list[str] = []

            def fake_dispatch(config, queue, **kwargs):
                dispatched.append(kwargs["task_id"])
                return mock.Mock(to_dict=lambda: {"task_id": kwargs["task_id"]})

            with mock.patch.object(scout, "read_all", return_value=snapshots):
                with mock.patch.object(scout, "dispatch", side_effect=fake_dispatch):
                    report = scout.run_once(config, queue, now_epoch=NOW)

            self.assertEqual(report.errors, ())
            self.assertEqual(len(dispatched), 8)
            self.assertEqual(sum(1 for task_id in dispatched if task_id.startswith("alpha")), 6)
            self.assertEqual(sum(1 for task_id in dispatched if task_id.startswith("beta")), 2)
