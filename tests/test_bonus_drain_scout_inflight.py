"""Regression contract for per-provider Bonus Drain in-flight caps."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import config as config_module, db, dispatcher, scout, usage  # noqa: E402


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


def _multi_account_config(
    queue: db.QueueDB,
    cache: Path,
    active_path: Path,
    *,
    max_jobs: int | None = None,
) -> config_module.RuntimeConfig:
    base = _two_provider_config(queue, cache)
    personal_activation = config_module.AdapterConfig(
        "alpha-personal-activation", "activation",
        ("switch", "--label", "Personal", "--active-path", str(active_path)),
    )
    business_activation = config_module.AdapterConfig(
        "alpha-business-activation", "activation",
        ("switch", "--label", "Business", "--active-path", str(active_path)),
    )
    return replace(
        base,
        adapters=(*base.adapters, personal_activation, business_activation),
        plans=(
            config_module.PlanConfig("alpha-personal-plan", "alpha"),
            config_module.PlanConfig("alpha-business-plan", "alpha"),
            config_module.PlanConfig("beta-plan", "beta"),
        ),
        accounts=(
            config_module.AccountConfig(
                "alpha-personal", "alpha", "alpha-personal-plan",
                activation_adapter_id="alpha-personal-activation",
            ),
            config_module.AccountConfig(
                "alpha-business", "alpha", "alpha-business-plan",
                activation_adapter_id="alpha-business-activation",
            ),
            base.accounts[1],
        ),
        limits=(
            _limit("alpha-personal-plan"),
            _limit("alpha-business-plan"),
            _limit("beta-plan"),
        ),
        max_jobs=max_jobs,
    )


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
            {"alpha-personal-plan-weekly": {
                "used_percent": personal_used, "resets_at": personal_reset,
            }},
        ),
        ("alpha", "alpha-business"): usage.UsageSnapshot(
            "alpha", "alpha-business", NOW,
            {"alpha-business-plan-weekly": {
                "used_percent": business_used, "resets_at": business_reset,
            }},
        ),
        ("beta", "beta-account"): usage.UsageSnapshot(
            "beta", "beta-account", NOW,
            {"beta-plan-weekly": {
                "used_percent": beta_used, "resets_at": NOW + 40 * HOUR,
            }},
        ),
    }


class ScoutActiveAccountSelectionTests(unittest.TestCase):
    def _tick(
        self,
        personal_used: float,
        business_used: float,
        *,
        personal_reset: int | None = None,
        business_reset: int | None = None,
    ) -> scout.TickPlan:
        self.queue.add_task(_task("alpha-next", "alpha"))
        snapshots = _multi_snapshots(
            personal_used,
            business_used,
            personal_reset=personal_reset,
            business_reset=business_reset,
        )
        with mock.patch.object(scout, "read_all", return_value=snapshots):
            return scout.plan_tick(
                self.config, self.queue, now_epoch=NOW, provider_holds=(),
            )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "queue.db")
        self.queue.initialize()
        self.active = self.root / "active"
        self.active.write_text("Business\n", encoding="utf-8")
        self.config = _multi_account_config(self.queue, self.root / "cache", self.active)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_both_drainable_selects_active_despite_inactive_surplus(self) -> None:
        tick = self._tick(50, 70)
        self.assertEqual(
            [(batch.provider_id, batch.account_id) for batch in tick.plan.batches],
            [("alpha", "alpha-business")],
        )
        self.assertEqual(
            tick.plan.closed[("alpha", "alpha-personal")],
            "queued behind alpha-business",
        )

    def test_active_drainable_beats_urgent_inactive_sibling(self) -> None:
        tick = self._tick(
            80,
            60,
            personal_reset=NOW + 20 * HOUR,
            business_reset=NOW + 40 * HOUR,
        )
        self.assertEqual([batch.account_id for batch in tick.plan.batches], ["alpha-business"])

    def test_only_active_drainable_selects_active(self) -> None:
        tick = self._tick(80, 70)
        self.assertEqual([batch.account_id for batch in tick.plan.batches], ["alpha-business"])

    def test_only_inactive_drainable_selects_inactive(self) -> None:
        tick = self._tick(70, 80)
        self.assertEqual([batch.account_id for batch in tick.plan.batches], ["alpha-personal"])

    def test_neither_account_drainable_dispatches_nothing(self) -> None:
        tick = self._tick(80, 80)
        self.assertEqual(tick.plan.batches, ())
        self.assertEqual(tick.allocations, {})

    def test_urgent_active_at_floor_does_not_block_drainable_inactive(self) -> None:
        tick = self._tick(70, 85, business_reset=NOW + 20 * HOUR)
        self.assertEqual([batch.account_id for batch in tick.plan.batches], ["alpha-personal"])

    def test_inflight_on_business_closes_personal_then_business_survives(self) -> None:
        self.queue.add_task(_task("already-running", "alpha"))
        self.queue.add_task(_task("alpha-next", "alpha"))
        self.queue.record(
            "already-running", f"alpha-business/alpha-business-plan-weekly/{NOW + 40 * HOUR}",
            status="dispatched", provider_id="alpha", account_id="alpha-business",
            cycle=NOW + 40 * HOUR,
        )
        self.active.write_text("Personal\n", encoding="utf-8")
        with mock.patch.object(scout, "read_all", return_value=_multi_snapshots(70, 70)):
            tick = scout.plan_tick(
                self.config, self.queue, now_epoch=NOW, provider_holds=(),
            )

        self.assertEqual([batch.account_id for batch in tick.plan.batches], ["alpha-business"])
        self.assertEqual(
            tick.plan.closed[("alpha", "alpha-personal")],
            "provider inflight on alpha-business",
        )

    def test_tight_global_cap_charges_multi_account_provider_once(self) -> None:
        for index in range(6):
            self.queue.add_task(_task(f"alpha-{index}", "alpha"))
            self.queue.add_task(_task(f"beta-{index}", "beta"))
        config = replace(self.config, max_jobs=8)
        with mock.patch.object(
            scout, "read_all", return_value=_multi_snapshots(69, 69, beta_used=69),
        ):
            tick = scout.plan_tick(config, self.queue, now_epoch=NOW, provider_holds=())

        self.assertEqual(
            {
                key: len(tasks)
                for key, tasks in tick.allocations.items()
            },
            {("alpha", "alpha-business"): 6, ("beta", "beta-account"): 2},
        )

    def test_unknown_marker_closes_only_its_provider(self) -> None:
        self.queue.add_task(_task("alpha-next", "alpha"))
        self.queue.add_task(_task("beta-next", "beta"))
        self.active.write_text("Unknown\n", encoding="utf-8")
        with mock.patch.object(
            scout, "read_all", return_value=_multi_snapshots(70, 70, beta_used=70),
        ):
            tick = scout.plan_tick(
                self.config, self.queue, now_epoch=NOW, provider_holds=(),
            )

        self.assertEqual(
            [(batch.provider_id, batch.account_id) for batch in tick.plan.batches],
            [("beta", "beta-account")],
        )
        self.assertTrue(all(key in tick.plan.closed for key in (
            ("alpha", "alpha-personal"), ("alpha", "alpha-business"),
        )))

    def test_inactive_switch_success_retains_concrete_account_attribution(self) -> None:
        self.queue.add_task(_task("alpha-next", "alpha"))
        activation_events: list[tuple[str, str]] = []

        def activate(action: str, account_id: str) -> None:
            activation_events.append((action, account_id))
            if action == "activate":
                self.active.write_text("Personal\n", encoding="utf-8")

        with mock.patch.object(scout, "read_all", return_value=_multi_snapshots(70, 80)):
            report = scout.run_once(
                self.config,
                self.queue,
                now_epoch=NOW,
                activation_call=activate,
                router_call=lambda *_args, **_kwargs: {
                    "dispatch": {"job_id": "alpha-job", "launched": True},
                },
            )

        self.assertEqual(report.errors, ())
        self.assertEqual(activation_events, [("activate", "alpha-personal")])
        self.assertEqual([item.account_id for item in report.dispatched], ["alpha-personal"])
        self.assertEqual(self.queue.claims()[0].account_id, "alpha-personal")

    def test_first_proven_refusal_stops_provider_and_other_provider_continues(self) -> None:
        self.queue.add_task(_task("alpha-one", "alpha"))
        self.queue.add_task(_task("alpha-two", "alpha"))
        self.queue.add_task(_task("beta-one", "beta"))
        activation_events: list[tuple[str, str]] = []
        router_providers: list[str] = []

        def activate(action: str, account_id: str) -> None:
            if account_id == "alpha-personal":
                activation_events.append((action, account_id))
                raise dispatcher.ActivationUnavailable(
                    "active work refused rotation", known_not_switched=True,
                )

        def route(argv: list[str], **_kwargs: object) -> dict[str, object]:
            router_providers.append(argv[argv.index("--provider") + 1])
            return {"dispatch": {"job_id": "beta-job", "launched": True}}

        with mock.patch.object(
            scout, "read_all", return_value=_multi_snapshots(70, 80, beta_used=70),
        ):
            report = scout.run_once(
                self.config,
                self.queue,
                now_epoch=NOW,
                activation_call=activate,
                router_call=route,
            )

        self.assertEqual(activation_events, [("activate", "alpha-personal")])
        self.assertEqual(router_providers, ["beta"])
        self.assertEqual([item.provider_id for item in report.dispatched], ["beta"])
        self.assertEqual([error["task_id"] for error in report.errors], ["alpha-one"])
        self.assertEqual(
            [attempt.state for attempt in self.queue.attempts(task_id="alpha-one")],
            ["aborted"],
        )
        self.assertEqual(self.queue.attempts(task_id="alpha-two"), [])
        self.assertFalse(any(claim.provider_id == "alpha" for claim in self.queue.claims()))
        self.assertEqual(self.queue.activation_leases(provider_id="alpha"), [])

    def test_dry_run_attributes_active_account_without_activation(self) -> None:
        self.queue.add_task(_task("alpha-next", "alpha"))
        with mock.patch.object(scout, "read_all", return_value=_multi_snapshots(50, 70)):
            report = scout.run_once(
                self.config,
                self.queue,
                now_epoch=NOW,
                dry_run=True,
                activation_call=lambda *_args: self.fail("dry run activated an account"),
                router_call=lambda *_args, **_kwargs: self.fail("dry run called the router"),
            )

        self.assertEqual(
            [(item["provider_id"], item["account_id"]) for item in report.previews],
            [("alpha", "alpha-business")],
        )


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

    def test_omitted_global_cap_lets_each_provider_fill_its_own_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = db.QueueDB(root / "queue.db")
            queue.initialize()
            for index in range(6):
                queue.add_task(_task(f"alpha-{index}", "alpha"))
                queue.add_task(_task(f"beta-{index}", "beta"))
            config = _two_provider_config(queue, root / "cache")
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
            self.assertEqual(len(dispatched), 12)
            self.assertEqual(sum(1 for task_id in dispatched if task_id.startswith("alpha")), 6)
            self.assertEqual(sum(1 for task_id in dispatched if task_id.startswith("beta")), 6)
