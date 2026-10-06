"""Tasks that invoke the implement skill run only on providers declaring the implement capability."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock
from tests.readiness_fixture import review_json, reviewed


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import cli, config as config_module, db, dispatcher, scout, usage  # noqa: E402


NOW = 2_000_000_000
HOUR = 3_600
RESET = NOW + 40 * HOUR
IMPLEMENT_GOAL = "/implement the parser fix described in the context"


def _task(task_id: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": task_id, "title": task_id, "kind": "oneoff", "priority": 2,
        "cwd": "/tmp", "goal": f"run {task_id}", "active": True, "size": "small",
    }
    values.update(overrides)
    return values


def _task_obj(**overrides: object) -> db.Task:
    base = db.Task(
        id="t", title="t", kind="oneoff", priority=2, cadence=None, cwd="/tmp",
        goal="run t", context=None, constraints=None, precondition=None, done_when=None,
        created_at="2026-01-01T00:00:00Z", active=True,
    )
    return replace(base, **overrides)


def _provider(provider_id: str, *capabilities: str) -> config_module.ProviderConfig:
    return config_module.ProviderConfig(
        provider_id, config_module.DispatchBinding("router", provider_id),
        frozenset(capabilities), "single",
    )


def _limit(plan_id: str) -> config_module.LimitConfig:
    return config_module.LimitConfig(
        f"{plan_id}-weekly", plan_id, 604800, 95, 259200, 6,
        max_percent_per_window=0.5, estimated_percent_per_job=1.0,
        pacing_window_seconds=HOUR,
    )


def _config(database: Path, providers: tuple[str, ...]) -> config_module.RuntimeConfig:
    capabilities = {"codex": ("implement",), "grok": ()}
    router = config_module.AdapterConfig(
        "router", "agent-router", ("/bin/true",), timeout_seconds=0.2, max_output_bytes=1024,
    )
    return config_module.RuntimeConfig(
        schema_version=1,
        source_path=None,
        database=database,
        record_command=("/bin/true",),
        secret_refs=(),
        adapters=(router,),
        providers=tuple(_provider(item, *capabilities[item]) for item in providers),
        plans=tuple(config_module.PlanConfig(f"{item}-plan", item) for item in providers),
        accounts=tuple(
            config_module.AccountConfig(f"{item}-account", item, f"{item}-plan")
            for item in providers
        ),
        limits=tuple(_limit(f"{item}-plan") for item in providers),
        viewer={},
        pr_exceptions=(),
        usage_max_age_seconds=3600,
        cache_dir=database.parent / "cache",
    )


def _snapshots(providers: tuple[str, ...]) -> dict[tuple[str, str], usage.UsageSnapshot]:
    return {
        (item, f"{item}-account"): usage.UsageSnapshot(
            item, f"{item}-account", NOW,
            {f"{item}-plan-weekly": {"used_percent": 70, "resets_at": RESET}},
        )
        for item in providers
    }


class PredicateTests(unittest.TestCase):
    def test_positive_forms(self) -> None:
        positives = {
            "use_implement": _task_obj(use_implement=True),
            "goal slash": _task_obj(goal="/implement the ticket"),
            "context slash": _task_obj(context="Run it with /implement once the base is set."),
            "constraints prose": _task_obj(constraints="Use the implement skill for this change."),
            "done_when prose": _task_obj(done_when="Done via the Implement_Skill driver"),
            "precondition codex": _task_obj(precondition="$implement must be available"),
            "backticked": _task_obj(goal="Use the `implement` skill for the fix"),
            "single quoted": _task_obj(goal="Run the 'implement' skill on it"),
            "double quoted": _task_obj(context='Hand it to the "implement" skill'),
            "bold": _task_obj(constraints="Drive it with the **implement** skill"),
            "skill named": _task_obj(goal="Use the skill named `implement` to fix the parser"),
            "skill called": _task_obj(context="Hand it to the skill called implement"),
            "skill wrapped": _task_obj(constraints="Run skill `implement` once the base exists"),
        }
        for label, task in positives.items():
            with self.subTest(label):
                self.assertTrue(db.task_invokes_implement(task))

    def test_negative_forms(self) -> None:
        negatives = {
            "plain": _task_obj(goal="Fix the parser"),
            "implementation": _task_obj(goal="See /implementation notes"),
            "verb": _task_obj(goal="implement the feature"),
            "skill tree": _task_obj(goal="implement the skill tree page"),
            "skills list": _task_obj(goal="implement skills search in the catalog"),
            "skill to implement": _task_obj(goal="use this skill to implement the parser"),
            "skill implementation": _task_obj(goal="a skill implementation guide"),
            "skill path": _task_obj(context="Read ~/.claude/skills/implement/SKILL.md first"),
            "title only": _task_obj(title="/implement in title is not rendered"),
        }
        for label, task in negatives.items():
            with self.subTest(label):
                self.assertFalse(db.task_invokes_implement(task))


class CapabilityRoutingCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "state" / "queue.db")
        self.queue.initialize()


class PlanningTests(CapabilityRoutingCase):
    def _plan(self, providers: tuple[str, ...]) -> scout.TickPlan:
        config = _config(self.queue.path, providers)
        with mock.patch.object(scout, "read_all", return_value=_snapshots(providers)):
            return scout.plan_tick(config, self.queue, now_epoch=NOW)

    @staticmethod
    def _allocated(tick: scout.TickPlan) -> dict[str, str]:
        return {
            task.id: provider_id
            for (provider_id, _account), tasks in tick.allocations.items()
            for task in tasks
        }

    def test_implement_task_goes_to_codex_never_grok(self) -> None:
        self.queue.add_task(reviewed(_task("impl", goal=IMPLEMENT_GOAL)))
        self.queue.add_task(reviewed(_task("flag", use_implement=True)))

        allocated = self._allocated(self._plan(("grok", "codex")))

        self.assertEqual(allocated, {"impl": "codex", "flag": "codex"})

    def test_only_grok_open_holds_implement_work_with_a_named_blocker(self) -> None:
        self.queue.add_task(reviewed(_task("impl", goal=IMPLEMENT_GOAL)))
        self.queue.add_task(reviewed(_task("plain")))
        config = _config(self.queue.path, ("grok",))

        with mock.patch.object(scout, "read_all", return_value=_snapshots(("grok",))):
            report = scout.run_once(config, self.queue, now_epoch=NOW, dry_run=True)

        self.assertEqual([item["task_id"] for item in report.previews], ["plain"])
        self.assertEqual(report.previews[0]["provider_id"], "grok")
        holds = [item for item in report.blockers if item.get("kind") == "provider_capability"]
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["task_id"], "impl")
        self.assertEqual(holds[0]["provider_id"], "grok")
        self.assertEqual(holds[0]["capability"], "implement")
        self.assertIn("lacks the implement capability", holds[0]["reason"])
        self.assertIn("provider_capability", json.dumps(report.to_dict()))

    def test_only_implement_work_with_only_grok_reports_the_blocker(self) -> None:
        # grok would otherwise have no eligible work, so its gate never opens on its own.
        self.queue.add_task(reviewed(_task("impl", goal=IMPLEMENT_GOAL)))

        tick = self._plan(("grok",))

        self.assertEqual(self._allocated(tick), {})
        holds = [item for item in tick.dependency_holds if item["kind"] == "provider_capability"]
        self.assertEqual([(item["task_id"], item["provider_id"]) for item in holds], [("impl", "grok")])

    def test_implement_task_waiting_on_a_held_work_group_gets_no_capability_blocker(self) -> None:
        self.queue.add_task(reviewed(_task("plain", priority=1, work_group="parser")))
        self.queue.add_task(reviewed(_task("impl", goal=IMPLEMENT_GOAL, work_group="parser")))

        tick = self._plan(("grok",))

        self.assertEqual(self._allocated(tick), {"plain": "grok"})
        self.assertFalse([item for item in tick.dependency_holds if item["kind"] == "provider_capability"])

    def test_grok_closed_by_usage_reports_no_capability_blocker(self) -> None:
        self.queue.add_task(reviewed(_task("impl", goal=IMPLEMENT_GOAL)))
        config = _config(self.queue.path, ("grok",))
        exhausted = {
            ("grok", "grok-account"): usage.UsageSnapshot(
                "grok", "grok-account", NOW,
                {"grok-plan-weekly": {"used_percent": 100, "resets_at": RESET}},
            ),
        }
        with mock.patch.object(scout, "read_all", return_value=exhausted):
            tick = scout.plan_tick(config, self.queue, now_epoch=NOW)

        self.assertEqual(self._allocated(tick), {})
        self.assertFalse([item for item in tick.dependency_holds if item["kind"] == "provider_capability"])

    def test_plain_task_still_allocates_to_grok(self) -> None:
        self.queue.add_task(reviewed(_task("plain")))

        tick = self._plan(("grok",))

        self.assertEqual(self._allocated(tick), {"plain": "grok"})
        self.assertFalse([item for item in tick.dependency_holds if item["kind"] == "provider_capability"])


class ClaimTwinTests(CapabilityRoutingCase):
    def test_claim_refuses_implement_work_on_an_incapable_provider(self) -> None:
        self.queue.add_task(reviewed(_task("impl", goal=IMPLEMENT_GOAL)))
        self.queue.add_task(reviewed(_task("plain")))

        refused = self.queue.claim(
            "impl", "grok-account/k/1", "grok", "grok-account",
            provider_capabilities=(), now_epoch=NOW,
        )
        plain = self.queue.claim(
            "plain", "grok-account/k/1", "grok", "grok-account",
            provider_capabilities=(), now_epoch=NOW,
        )
        capable = self.queue.claim(
            "impl", "codex-account/k/1", "codex", "codex-account",
            provider_capabilities=("implement",), now_epoch=NOW,
        )

        self.assertIsNone(refused)
        self.assertIsNotNone(plain)
        self.assertIsNotNone(capable)


class AutoClassificationTests(CapabilityRoutingCase):
    def _router(self, seen: list[list[str]]):
        def router_call(argv: list[str], **_kwargs: object) -> dict[str, object]:
            seen.append(list(argv))
            if "--dry-run" in argv:
                return {"provider": "grok"}
            return {"dispatch": {"job_id": "job-1", "launched": True}}
        return router_call

    def test_incompatible_classification_falls_through_to_a_capable_provider(self) -> None:
        self.queue.add_task(reviewed(_task("impl", goal=IMPLEMENT_GOAL)))
        config = _config(self.queue.path, ("grok", "codex"))
        seen: list[list[str]] = []

        dispatcher.dispatch(
            config, self.queue, task_id="impl", eligibility_key="manual/impl",
            requested_provider="auto", router_call=self._router(seen),
        )

        classify = next(argv for argv in seen if "--dry-run" in argv)
        self.assertIn("Required capabilities: implement", classify[-1])
        launch = next(argv for argv in seen if "--dry-run" not in argv)
        self.assertEqual(launch[launch.index("--provider") + 1], "codex")

    def test_no_capable_provider_refuses_before_any_claim(self) -> None:
        self.queue.add_task(reviewed(_task("impl", goal=IMPLEMENT_GOAL)))
        config = _config(self.queue.path, ("grok",))
        seen: list[list[str]] = []

        with self.assertRaisesRegex(
            dispatcher.InvalidRoute,
            "no configured provider is compatible with task impl: it invokes the implement "
            "skill and needs capability implement",
        ):
            dispatcher.dispatch(
                config, self.queue, task_id="impl", eligibility_key="manual/impl",
                requested_provider="auto", router_call=self._router(seen),
            )

        self.assertTrue(all("--dry-run" in argv for argv in seen))
        self.assertEqual(self.queue.attempts(), [])
        self.assertIsNone(self.queue.claim_for("impl", "manual/impl"))
        self.assertTrue(self.queue.readiness("impl")["ready"])


class DispatchBoundaryTests(AutoClassificationTests):
    def _assert_unclaimed(self, task_id: str) -> None:
        self.assertEqual(self.queue.attempts(), [])
        self.assertIsNone(self.queue.claim_for(task_id, f"manual/{task_id}"))
        self.assertTrue(self.queue.readiness(task_id)["ready"])

    def test_plain_task_auto_classified_to_grok_launches_on_grok(self) -> None:
        self.queue.add_task(reviewed(_task("plain")))
        seen: list[list[str]] = []

        dispatcher.dispatch(
            _config(self.queue.path, ("grok", "codex")), self.queue, task_id="plain",
            eligibility_key="manual/plain", requested_provider="auto", router_call=self._router(seen),
        )

        launch = next(argv for argv in seen if "--dry-run" not in argv)
        self.assertEqual(launch[launch.index("--provider") + 1], "grok")

    def test_other_mismatch_keeps_the_original_refusal_before_the_claim(self) -> None:
        self.queue.add_task(reviewed(_task("pinned", allowed_providers=["codex"])))
        seen: list[list[str]] = []

        with self.assertRaisesRegex(
            dispatcher.InvalidRoute, "provider grok is incompatible with task pinned",
        ):
            dispatcher.dispatch(
                _config(self.queue.path, ("grok", "codex")), self.queue, task_id="pinned",
                eligibility_key="manual/pinned", requested_provider="auto",
                router_call=self._router(seen),
            )

        self.assertTrue(all("--dry-run" in argv for argv in seen))
        self._assert_unclaimed("pinned")

    def test_explicit_grok_dispatch_of_implement_work_is_refused_before_the_claim(self) -> None:
        self.queue.add_task(reviewed(_task("impl", goal=IMPLEMENT_GOAL)))
        config = _config(self.queue.path, ("grok", "codex"))
        seen: list[list[str]] = []

        with self.assertRaisesRegex(dispatcher.InvalidRoute, "provider grok is incompatible with task impl"):
            dispatcher.dispatch(
                config, self.queue, task_id="impl", eligibility_key="manual/impl",
                requested_provider="grok", router_call=self._router(seen),
            )
        self.assertEqual(seen, [])
        self._assert_unclaimed("impl")

        dispatcher.dispatch(
            config, self.queue, task_id="impl", eligibility_key="manual/impl",
            requested_provider="codex", router_call=self._router(seen),
        )
        self.assertEqual(seen[-1][seen[-1].index("--provider") + 1], "codex")

    def test_viewer_run_now_on_grok_is_refused_before_the_claim(self) -> None:
        import importlib.util
        import os

        server = SKILL_ROOT / "services" / "jobs-viewer" / "server.py"
        spec = importlib.util.spec_from_file_location("bonus_jobs_viewer_implement_test", server)
        assert spec is not None and spec.loader is not None
        viewer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(viewer)
        self.queue.add_task(reviewed(_task("impl", goal=IMPLEMENT_GOAL)))
        config = _config(self.queue.path, ("grok", "codex"))

        with (
            mock.patch.object(viewer.graph_config, "load_config", return_value=config),
            mock.patch.dict(os.environ, {"BONUS_DRAIN_CONFIG": str(self.root / "config.json")}),
        ):
            ok, message = viewer.run_task_now("impl", "grok")

        self.assertFalse(ok)
        self.assertIn("provider grok is incompatible with task impl", message)
        self.assertEqual(self.queue.attempts(), [])
        self.assertTrue(self.queue.readiness("impl")["ready"])


    def test_viewer_run_now_launches_on_the_chosen_capable_provider(self) -> None:
        import importlib.util
        import os

        server = SKILL_ROOT / "services" / "jobs-viewer" / "server.py"
        spec = importlib.util.spec_from_file_location("bonus_jobs_viewer_launch_test", server)
        assert spec is not None and spec.loader is not None
        viewer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(viewer)
        self.queue.add_task(reviewed(_task("plain")))
        self.queue.add_task(reviewed(_task("impl", goal=IMPLEMENT_GOAL)))
        config = _config(self.queue.path, ("grok", "codex"))
        launches: list[list[str]] = []

        def router(argv, **_kwargs):
            launches.append(list(argv))
            return {"dispatch": {"job_id": f"job-{len(launches)}", "launched": True}}

        # run_task_now offers no router_call seam; fake only the router subprocess boundary.
        with (
            mock.patch.object(viewer.graph_config, "load_config", return_value=config),
            mock.patch.object(dispatcher, "_subprocess_call", side_effect=router),
            mock.patch.dict(os.environ, {"BONUS_DRAIN_CONFIG": str(self.root / "config.json")}),
        ):
            plain_ok, plain_message = viewer.run_task_now("plain", "grok")
            impl_ok, impl_message = viewer.run_task_now("impl", "codex")

        self.assertTrue(plain_ok, plain_message)
        self.assertTrue(impl_ok, impl_message)
        self.assertIn("launched on grok", plain_message)
        self.assertIn("launched on codex", impl_message)
        self.assertEqual(
            [argv[argv.index("--provider") + 1] for argv in launches], ["grok", "codex"],
        )


class LongHorizonCoordinatorTests(CapabilityRoutingCase):
    def test_coordinator_turn_needs_the_implement_capability(self) -> None:
        from bonus_drain import goals

        store = goals.GoalStore(self.queue)
        store.create(dict(
            id="release", title="Release", cwd=str(self.root), outcome="The release works",
            authority="Local disposable work only.",
            acceptance=[{"id": "journey", "proof": "Run the journey"}],
            merge_policy="stack", max_turns=4, deadline=NOW + 3600, max_inflight=1,
            coordinator={"model": "gpt-6-astra"}, task_ids=[],
        ), now=NOW)
        store.tick(now=NOW)
        coordinator = store.show("release")["coordinator_task"]
        task = self.queue.task(coordinator)
        assert task is not None
        self.assertTrue(db.task_invokes_implement(task))

        def eligible(*capabilities: str) -> list[str]:
            return [item.id for item in self.queue.eligible_tasks(
                0, provider_id="grok", capabilities=capabilities, now_epoch=NOW,
            )]

        self.assertNotIn(coordinator, eligible())
        self.assertIn(coordinator, eligible("implement"))


class CLIRefusalTests(CapabilityRoutingCase):
    def setUp(self) -> None:
        super().setUp()
        raw = {
            "schema_version": 1,
            "database": str(self.queue.path),
            "cache_dir": str(self.root / "cache"),
            "record_command": ["/bin/true"],
            "adapters": [{"id": "router", "kind": "agent-router", "argv": ["/bin/true"]}],
            "providers": [
                {"id": "codex", "account_mode": "single", "capabilities": ["implement"],
                 "dispatch": {"adapter_id": "router", "provider": "codex"}},
                {"id": "grok", "account_mode": "single",
                 "dispatch": {"adapter_id": "router", "provider": "grok"}},
            ],
            "plans": [
                {"id": "codex-plan", "provider_id": "codex"},
                {"id": "grok-plan", "provider_id": "grok"},
            ],
            "accounts": [
                {"id": "codex-account", "provider_id": "codex", "plan_id": "codex-plan"},
                {"id": "grok-account", "provider_id": "grok", "plan_id": "grok-plan"},
            ],
            "limits": [
                {"id": f"{item}-weekly", "plan_id": f"{item}-plan", "window_seconds": 604800,
                 "ceiling_percent": 95, "lead_seconds": 20000, "batch_size": 1}
                for item in ("codex", "grok")
            ],
            "viewer": {},
            "pr_exceptions": [],
        }
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")

    def run_cli(self, *argv: str) -> tuple[int, str]:
        stderr = io.StringIO()
        real_json = cli._json
        # Success payloads bind the real stdout at import; keep only the stderr refusals.
        quiet = mock.patch.object(
            cli, "_json", side_effect=lambda value, **kw: real_json(value, **kw) if "stream" in kw else None,
        )
        with redirect_stderr(stderr), quiet:
            code = cli.main([argv[0], "--config", str(self.config_path), *argv[1:]])
        return code, stderr.getvalue()

    def add(self, task_id: str, goal: str, *extra: str) -> tuple[int, str]:
        return self.run_cli(
            "add", "--id", task_id, "--title", task_id, "--kind", "oneoff", "--size", "small",
            "--cwd", "/tmp", "--goal", goal, *extra,
            "--readiness-review", review_json(goal=goal), "--json",
        )

    def assert_refused(self, code: int, stderr: str, provider: str = "grok") -> None:
        self.assertNotEqual(code, 0)
        self.assertIn(
            f"provider {provider} lacks the implement capability, so it cannot run a task that "
            "invokes the implement skill; drop it from providers or use auto",
            stderr,
        )

    def test_add_refuses_implement_work_pinned_to_grok(self) -> None:
        code, stderr_text = self.add("slash", IMPLEMENT_GOAL, "--providers", "grok")
        self.assert_refused(code, stderr_text)
        code, stderr_text = self.add("flagged", "Fix the parser", "--providers", "grok", "--use-implement", "1")
        self.assert_refused(code, stderr_text)
        self.assertEqual(self.queue.tasks(), [])

    def test_grant_scope_counts_as_prompt_text(self) -> None:
        implement_grant = json.dumps({
            "id": "parser-pr", "kind": "sacred_path",
            "scope": "Run /implement for the parser fix and publish its PR",
        })
        plain_grant = json.dumps({"id": "publish", "kind": "sacred_path", "scope": "publish the PR"})

        code, stderr = self.add(
            "granted", "Fix the parser", "--providers", "grok", "--grant", implement_grant,
        )
        self.assert_refused(code, stderr)
        self.assertIsNone(self.queue.task("granted"))

        code, stderr = self.add(
            "granted-codex", "Fix the parser", "--providers", "codex", "--grant", implement_grant,
        )
        self.assertEqual(code, 0, stderr)
        stored = self.queue.task("granted-codex")
        assert stored is not None
        self.assertTrue(db.task_invokes_implement(stored))

        code, stderr = self.add(
            "plain-grant", "Fix the parser", "--providers", "grok", "--grant", plain_grant,
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.queue.task("plain-grant").allowed_providers, ("grok",))

    def test_add_accepts_implement_work_pinned_to_codex(self) -> None:
        code, _ = self.add("ok", IMPLEMENT_GOAL, "--providers", "codex")

        self.assertEqual(code, 0)
        stored = self.queue.task("ok")
        assert stored is not None
        self.assertEqual(stored.allowed_providers, ("codex",))

    def test_edit_that_adds_implement_to_a_grok_pinned_task_is_refused(self) -> None:
        self.assertEqual(self.add("pinned", "Fix the parser", "--providers", "grok")[0], 0)
        changes = {"goal": IMPLEMENT_GOAL, "readiness_review": json.loads(review_json(goal=IMPLEMENT_GOAL))}

        code, stderr = self.run_cli("edit", "pinned", "--changes", json.dumps(changes))

        self.assert_refused(code, stderr)
        stored = self.queue.task("pinned")
        assert stored is not None
        self.assertEqual(stored.goal, "Fix the parser")

    def test_set_providers_refuses_grok_and_auto_clears_the_pin(self) -> None:
        self.assertEqual(self.add("impl", IMPLEMENT_GOAL, "--providers", "codex")[0], 0)

        code, stderr = self.run_cli("set-providers", "impl", "grok")
        self.assert_refused(code, stderr)
        stored = self.queue.task("impl")
        assert stored is not None
        self.assertEqual(stored.allowed_providers, ("codex",))

        payloads: list[object] = []
        with mock.patch.object(cli, "_json", side_effect=lambda value, **_k: payloads.append(value)):
            code = cli.main([
                "set-providers", "--config", str(self.config_path), "impl", "auto", "--json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(payloads[0]["task"]["allowed_providers"], [])
        stored = self.queue.task("impl")
        assert stored is not None
        self.assertEqual(stored.allowed_providers, ())

        code, stderr = self.run_cli("set-providers", "impl", "auto,codex")
        self.assertNotEqual(code, 0)
        self.assertIn("auto cannot be combined", stderr)
        stored = self.queue.task("impl")
        assert stored is not None
        self.assertEqual(stored.allowed_providers, ())

    def test_set_providers_liveness(self) -> None:
        self.assertEqual(self.add("impl", IMPLEMENT_GOAL)[0], 0)
        self.assertEqual(self.add("plain", "Fix the parser")[0], 0)

        self.assertEqual(self.run_cli("set-providers", "impl", "codex", "--json")[0], 0)
        self.assertEqual(self.run_cli("set-providers", "plain", "grok", "--json")[0], 0)

        self.assertEqual(self.queue.task("impl").allowed_providers, ("codex",))
        self.assertEqual(self.queue.task("plain").allowed_providers, ("grok",))

    def test_edit_liveness(self) -> None:
        self.assertEqual(self.add("pinned", "Fix the parser", "--providers", "grok")[0], 0)
        self.assertEqual(self.add("capable", "Fix the lexer", "--providers", "codex")[0], 0)

        def edit(task_id: str, goal: str) -> int:
            changes = {"goal": goal, "readiness_review": json.loads(review_json(goal=goal))}
            return self.run_cli("edit", task_id, "--changes", json.dumps(changes))[0]

        self.assertEqual(edit("pinned", "Fix the parser and its tests"), 0)
        self.assertEqual(edit("capable", IMPLEMENT_GOAL), 0)
        self.assertEqual(self.queue.task("pinned").goal, "Fix the parser and its tests")
        self.assertEqual(self.queue.task("capable").goal, IMPLEMENT_GOAL)

    def test_legacy_provider_filters_apply_the_capability_gate(self) -> None:
        self.assertEqual(self.add("impl", IMPLEMENT_GOAL)[0], 0)
        self.assertEqual(self.add("plain", "Fix the parser")[0], 0)

        def picked(flag: str) -> list[str]:
            payloads: list[object] = []
            with mock.patch.object(cli, "_json", side_effect=lambda value, **_k: payloads.append(value)):
                code = cli.main(["pick", "--config", str(self.config_path), "10", "0", flag])
            self.assertEqual(code, 0)
            return sorted(item["id"] for item in payloads[0])

        def counted(command: str, flag: str) -> str:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = cli.main([command, "--config", str(self.config_path), "0", flag])
            self.assertEqual(code, 0)
            return stdout.getvalue().strip()

        self.assertEqual(picked("--grok"), ["plain"])
        self.assertEqual(picked("--codex"), ["impl", "plain"])
        self.assertEqual(counted("count-eligible", "--grok"), "1")
        self.assertEqual(counted("count-eligible", "--codex"), "2")
        self.assertEqual(counted("eligible", "--grok"), "1")


if __name__ == "__main__":
    unittest.main()
