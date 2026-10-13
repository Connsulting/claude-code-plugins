"""Red contracts for manual Bonus Drain dispatch and its scheduled sibling."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.readiness_fixture import isolated_cli, proxy_fixture, reviewed

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import adapters, cli, db, dispatcher
from bonus_drain import config as config_module

NOW = 2_000_000_000


def _task(task_id: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": task_id,
        "title": task_id,
        "kind": "oneoff",
        "priority": 2,
        "cwd": "/tmp",
        "goal": f"run {task_id}",
        "active": True,
    }
    values.update(overrides)
    return values


@contextlib.contextmanager
def _capture_cli_json() -> Iterator[list[object]]:
    """Capture CLI payloads without relying on its import-bound stdout default."""

    payloads: list[object] = []
    with mock.patch.object(
        cli, "_json", side_effect=lambda value, **_kwargs: payloads.append(value),
    ):
        yield payloads


class KickContractTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        router = config_module.AdapterConfig(
            "router", "t3-thread", (str(self.root / "bin" / "t3-thread"),),
            timeout_seconds=0.2, max_output_bytes=1024,
        )
        self.config = proxy_fixture(config_module.RuntimeConfig(schema_version=1, source_path=self.root / "config.json", database=self.root / "state" / "queue.db", record_command=("/bin/true",), secret_refs=(), adapters=(router,), providers=(
                config_module.ProviderConfig("alpha", config_module.DispatchBinding("router", "alpha-engine"), frozenset({"legacy-exclusive", "cpu"})),
                config_module.ProviderConfig("beta", config_module.DispatchBinding("router", "beta-engine"), frozenset()),
            ), limits=(
                config_module.LimitConfig("alpha-weekly", "alpha", 604800, 95, 20000, 1),
                config_module.LimitConfig("beta-weekly", "beta", 604800, 95, 20000, 1),
            ), viewer={}, pr_exceptions=(), usage_max_age_seconds=3600, cache_dir=self.root / "cache", proxy_secret_ref=None), (("alpha", "alpha-account"), ("beta", "beta-account"),))
        self.queue = db.QueueDB(self.config.database)
        self.queue.initialize()
        self.queue.add_task(reviewed(_task("portable")))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _router(argv: list[str], **_kwargs: object) -> dict[str, object]:
        if "--dry-run" in argv:
            return {"provider_id": "alpha"}
        return {"dispatch": {"job_id": "job-1", "launched": True}}


    def test_cli_and_viewer_name_the_same_manual_kick_service(self) -> None:
        from bonus_drain import kick

        server_path = SKILL_ROOT / "services" / "jobs-viewer" / "server.py"
        spec = importlib.util.spec_from_file_location("bonus_jobs_viewer_kick_test", server_path)
        assert spec is not None and spec.loader is not None
        viewer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(viewer)

        cli_service = getattr(cli, "kick_task", None)
        if cli_service is None:
            cli_service = getattr(getattr(cli, "kick", None), "kick_task", None)
        viewer_service = getattr(viewer, "kick_task", None)
        self.assertIs(cli_service, kick.kick_task)
        self.assertIs(viewer_service, kick.kick_task)

        result = dispatcher.DispatchResult(
            "portable", "manual/key", "alpha", "alpha-account", "job-shared", "prompt",
        )
        owner = cli if getattr(cli, "kick_task", None) is not None else cli.kick
        with (
            mock.patch.object(cli, "_queue", return_value=(self.config, self.queue)),
            mock.patch.object(owner, "kick_task", return_value=result) as shared,
            _capture_cli_json() as payloads,
        ):
            self.assertEqual(isolated_cli(["dispatch", "portable", "alpha", "--json"]), 0)
            self.assertEqual(isolated_cli(["run-now", "portable", "alpha", "--json"]), 0)
        self.assertEqual(
            [payload["dispatch"]["job_id"] for payload in payloads],
            ["job-shared", "job-shared"],
        )
        self.assertEqual(shared.call_count, 2)
        self.assertEqual(
            [
                call.kwargs.get("requested_provider")
                if "requested_provider" in call.kwargs else call.args[3]
                for call in shared.call_args_list
            ],
            ["alpha", "alpha"],
        )


    def test_dispatched_prompt_terminalizes_instead_of_leaving_a_blocked_lease(self) -> None:
        prompt = dispatcher.render_prompt(
            self.config,
            self.queue.task("portable"),
            "manual/terminal-before-blocked",
            "alpha",
            "alpha-account",
        )

        self.assertIn(
            "Never leave this background run blocked, waiting for input, or otherwise non-terminal.",
            prompt,
        )
        self.assertIn(
            "If the work itself cannot be completed, record failed with the blocker before exiting",
            prompt,
        )
        self.assertLess(
            prompt.index("If the work itself cannot be completed, record failed with the blocker before exiting"),
            prompt.index("When finished, record exactly one terminal event"),
        )



























    def test_nonselect_accounts_action_does_not_open_queue(self) -> None:
        with (
            mock.patch.object(cli, "_load_config", return_value=self.config),
            mock.patch.object(cli, "_queue") as opened,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(isolated_cli(["accounts", "labels"]), 0)
        opened.assert_not_called()

    def test_runtime_injected_non_router_classifier_and_launcher_are_rejected_before_execution(self) -> None:
        bad = config_module.AdapterConfig("bad", "usage", ("/bin/false",))
        bad_provider = replace(
            self.config.providers[0],
            dispatch=config_module.DispatchBinding("bad", "alpha-engine"),
        )
        cfg = replace(
            self.config,
            adapters=(bad, self.config.adapters[0]),
            providers=(bad_provider, self.config.providers[1]),
        )
        called: list[list[str]] = []

        def must_not_run(argv: list[str], **_kwargs: object) -> dict[str, object]:
            called.append(argv)
            return {"provider_id": "alpha", "dispatch": {"job_id": "unexpected"}}

        for requested in ("auto", "alpha"):
            with (
                self.subTest(requested=requested),
                self.assertRaises(dispatcher.DispatchError),
            ):
                dispatcher.dispatch(
                    cfg, self.queue, task_id="portable",
                    eligibility_key=f"manual/{requested}", requested_provider=requested,
                    router_call=must_not_run,
                )
        self.assertEqual(called, [])
        self.assertEqual(self.queue.claims(), [])

    def test_auto_without_an_open_gate_does_not_launch_or_claim(self) -> None:
        self.queue.add_task(reviewed(_task("classify-none")))
        calls: list[list[str]] = []

        def must_not_launch(argv: list[str], **_kwargs: object) -> dict[str, object]:
            calls.append(argv)
            return {"threadId": "unexpected"}

        result = dispatcher.dispatch(
            self.config, self.queue, task_id="classify-none",
            eligibility_key="manual/classify-none", requested_provider="auto",
            router_call=must_not_launch,
        )
        self.assertEqual(result.provider_id, "alpha")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--dry-run", calls[0])

    def test_post_spawn_uncertainty_retains_one_ambiguous_claim_and_never_relaunches(self) -> None:
        cases: dict[str, object] = {
            "timeout": subprocess.TimeoutExpired(["t3-thread"], 0.1),
            "overflow": adapters.ProcessOutputLimit("too much output"),
            "nonzero": subprocess.CompletedProcess([], 7, b"", b"router failed"),
            "malformed": subprocess.CompletedProcess([], 0, b"not-json", b""),
            "missing-identity": subprocess.CompletedProcess([], 0, b"{}", b""),
        }
        for name, outcome in cases.items():
            task_id = f"launch-{name}"
            key = f"manual/{name}"
            self.queue.add_task(reviewed(_task(task_id)))
            side_effect = outcome if isinstance(outcome, BaseException) else None
            return_value = None if side_effect is not None else outcome
            with mock.patch(
                "bonus_drain.adapters.run_bounded_process",
                side_effect=side_effect,
                return_value=return_value,
            ) as launched:
                with self.assertRaises(dispatcher.AmbiguousDispatch):
                    dispatcher.dispatch(
                        self.config, self.queue, task_id=task_id,
                        eligibility_key=key, requested_provider="alpha",
                    )
                claim = self.queue.claim_for(task_id, key)
                self.assertIsNotNone(claim)
                self.assertEqual(claim.state, "ambiguous")
                with self.assertRaises(dispatcher.AlreadyClaimed):
                    dispatcher.dispatch(
                        self.config, self.queue, task_id=task_id,
                        eligibility_key=key, requested_provider="alpha",
                    )
                self.assertEqual(launched.call_count, 1)

        task_id = "launch-bookkeeping"
        key = "manual/bookkeeping"
        self.queue.add_task(reviewed(_task(task_id)))
        launched = subprocess.CompletedProcess(
            [], 0, b'{"dispatch":{"job_id":"job-bookkeeping","launched":true}}', b"",
        )
        with (
            mock.patch("bonus_drain.adapters.run_bounded_process", return_value=launched),
            mock.patch.object(self.queue, "record", side_effect=db.QueueError("read only")),
            self.assertRaises(dispatcher.AmbiguousDispatch),
        ):
            dispatcher.dispatch(
                self.config, self.queue, task_id=task_id,
                eligibility_key=key, requested_provider="alpha",
            )
        self.assertEqual(self.queue.claim_for(task_id, key).state, "ambiguous")

    def test_post_spawn_router_diagnostics_redact_configured_secrets_before_persistence_or_viewing(self) -> None:
        secret = "configured-router-secret-value"
        adapter = replace(
            self.config.adapters[0], secret_refs={"ROUTER_TOKEN": "router-secret"},
        )
        cfg = replace(
            self.config,
            adapters=(adapter,),
            secret_refs=(
                config_module.SecretRef(
                    "router-secret", "env", name="BONUS_TEST_ROUTER_SECRET",
                ),
            ),
        )
        completed = subprocess.CompletedProcess(
            [], 9,
            f"stdout token={secret} " + ("x" * 800),
            f"stderr token={secret} " + ("y" * 800),
        )
        key = "manual/redacted-launch"
        with (
            mock.patch.dict(os.environ, {'BONUS_TEST_ROUTER_SECRET': secret}, clear=False),
            self.assertRaises(dispatcher.AmbiguousDispatch) as raised,
        ):
            dispatcher.dispatch(
                cfg, self.queue, task_id="portable", eligibility_key=key,
                requested_provider="alpha", router_call=lambda *_args, **_kwargs: completed,
            )

        claim = self.queue.claim_for("portable", key)
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(claim.state, "ambiguous")
        for diagnostic in (str(raised.exception), claim.detail or ""):
            self.assertNotIn(secret, diagnostic)
            self.assertIn("token=[REDACTED]", diagnostic)
            self.assertLessEqual(len(diagnostic), 1000)

    def _router_with_bound_secret(self) -> config_module.RuntimeConfig:
        adapter = replace(
            self.config.adapters[0], secret_refs={"TYPESAFE_API_KEY": "classifier-key"},
        )
        return replace(
            self.config,
            adapters=(adapter,),
            secret_refs=(
                config_module.SecretRef(
                    "classifier-key", "env", name="BONUS_TEST_CLASSIFIER_KEY",
                ),
            ),
        )

    @staticmethod
    def _completed_router(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        if "--dry-run" in argv:
            return subprocess.CompletedProcess([], 0, b'{"provider_id":"alpha"}', b"")
        return subprocess.CompletedProcess(
            [], 0, b'{"dispatch":{"job_id":"job-bound","launched":true}}', b"",
        )

    @staticmethod
    def _example_with_router_binding(ref_id: str) -> dict[str, object]:
        example = json.loads((SKILL_ROOT / "config.example.json").read_text(encoding="utf-8"))
        example["secret_refs"] += [
            {"id": "typesafe-api-key", "source": "env", "name": "TYPESAFE_API_KEY"},
        ]
        example["adapters"][0]["secret_refs"] = {"TYPESAFE_API_KEY": ref_id}
        return example

    def test_router_secret_ref_binding_may_export_a_credential_shaped_name(self) -> None:
        validated = config_module.validate_config(
            self._example_with_router_binding("typesafe-api-key"),
            source_dir=SKILL_ROOT, source_path=SKILL_ROOT / "config.example.json",
        )
        self.assertEqual(
            dict(validated.adapter("router").secret_refs),
            {"TYPESAFE_API_KEY": "typesafe-api-key"},
        )

    def test_binding_to_an_undeclared_ref_fails_without_echoing_the_value(self) -> None:
        pasted = "pasted-literal-credential-value"
        with self.assertRaises(config_module.ConfigError) as raised:
            config_module.validate_config(
                self._example_with_router_binding(pasted),
                source_dir=SKILL_ROOT, source_path=SKILL_ROOT / "config.example.json",
            )
        self.assertIn("adapters[0].secret_refs.TYPESAFE_API_KEY", str(raised.exception))
        self.assertNotIn(pasted, str(raised.exception))

    def test_inline_credential_fields_outside_secret_refs_are_still_rejected(self) -> None:
        example = json.loads((SKILL_ROOT / "config.example.json").read_text(encoding="utf-8"))
        example["providers"][0]["api_key"] = "inline-value"
        with self.assertRaisesRegex(config_module.ConfigError, "inline secret-like field"):
            config_module.validate_config(
                example, source_dir=SKILL_ROOT, source_path=SKILL_ROOT / "config.example.json",
            )

    def test_router_subprocess_receives_bound_secret_refs_and_nothing_else(self) -> None:
        cfg = self._router_with_bound_secret()
        secret = "bound-classifier-key-value"
        environ = {
            "BONUS_TEST_CLASSIFIER_KEY": secret,
            "BONUS_TEST_UNRELATED_SECRET": "must-not-reach-the-router",
        }
        with (
            mock.patch.dict(os.environ, environ, clear=False),
            mock.patch(
                "bonus_drain.adapters.run_bounded_process", side_effect=self._completed_router,
            ) as spawned,
        ):
            dispatcher.dispatch(
                cfg, self.queue, task_id="portable", eligibility_key="manual/bound-secret",
                requested_provider="auto",
            )

        phases = [("--dry-run" in call.args[0], call.kwargs["env"]) for call in spawned.call_args_list]
        self.assertEqual([dry for dry, _env in phases], [False])
        for _dry, env in phases:
            self.assertEqual(env.get("TYPESAFE_API_KEY"), secret)
            self.assertNotIn("BONUS_TEST_CLASSIFIER_KEY", env)
            self.assertNotIn("BONUS_TEST_UNRELATED_SECRET", env)

    def test_router_subprocess_without_binding_gets_no_ambient_secret(self) -> None:
        with (
            mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ambient"}, clear=False),
            mock.patch(
                "bonus_drain.adapters.run_bounded_process", side_effect=self._completed_router,
            ) as spawned,
        ):
            dispatcher.dispatch(
                self.config, self.queue, task_id="portable", eligibility_key="manual/unbound",
                requested_provider="alpha",
            )
        self.assertNotIn("TYPESAFE_API_KEY", spawned.call_args.kwargs["env"])

    def test_unresolvable_bound_router_secret_is_known_not_launched_and_retry_safe(self) -> None:
        cfg = self._router_with_bound_secret()
        environ = {key: value for key, value in os.environ.items() if key != "BONUS_TEST_CLASSIFIER_KEY"}
        for requested, failure in (
            ("auto", dispatcher.KnownDispatchFailure),
            ("alpha", dispatcher.KnownDispatchFailure),
        ):
            key = f"manual/unresolved-{requested}"
            with (
                mock.patch.dict(os.environ, environ, clear=True),
                mock.patch("bonus_drain.adapters.run_bounded_process") as spawned,
                self.assertRaises(failure) as raised,
            ):
                dispatcher.dispatch(
                    cfg, self.queue, task_id="portable", eligibility_key=key,
                    requested_provider=requested,
                )
            self.assertNotIsInstance(raised.exception, dispatcher.AmbiguousDispatch)
            self.assertIn("classifier-key", str(raised.exception))
            spawned.assert_not_called()
            self.assertEqual(self.queue.claim_for("portable", key), None)

    def test_explicit_router_launched_false_is_known_not_launched_and_retry_safe(self) -> None:
        task_id = "known-negative"
        key = "manual/known-negative"
        self.queue.add_task(reviewed(_task(task_id)))
        negative = subprocess.CompletedProcess(
            [], 9, b'{"launched":false,"error":"admission refused"}', b"router refused",
        )
        with mock.patch("bonus_drain.adapters.run_bounded_process", return_value=negative):
            for _attempt in range(2):
                with self.assertRaises(dispatcher.KnownDispatchFailure):
                    dispatcher.dispatch(
                        self.config, self.queue, task_id=task_id,
                        eligibility_key=key, requested_provider="alpha",
                    )
                self.assertEqual(self.queue.claim_for(task_id, key), None)

    def test_router_missing_codex_executable_is_known_not_launched_and_retry_safe(self) -> None:
        codex = config_module.ProviderConfig("codex", config_module.DispatchBinding("router", "codex"), frozenset())
        cfg = replace(self.config, providers=(codex,), limits=())
        task_id = "missing-codex"
        key = "manual/missing-codex"
        self.queue.add_task(reviewed(_task(task_id)))
        rejected = subprocess.CompletedProcess(
            [], 1, b"", (
                b"could not run `codex app-server daemon start`: No such file or directory"
            ),
        )
        with mock.patch("bonus_drain.adapters.run_bounded_process", return_value=rejected):
            for _attempt in range(2):
                with self.assertRaises(dispatcher.KnownDispatchFailure) as raised:
                    dispatcher.dispatch(
                        cfg, self.queue, task_id=task_id, eligibility_key=key,
                        requested_provider="codex",
                    )
                self.assertIn("codex app-server daemon start", str(raised.exception))
                self.assertEqual(self.queue.claim_for(task_id, key), None)

    def test_codex_daemon_start_timeout_is_known_not_launched(self) -> None:
        codex = config_module.ProviderConfig("codex", config_module.DispatchBinding("router", "codex"), frozenset())
        cfg = replace(self.config, providers=(codex,), limits=())
        task_id = "daemon-timeout"
        key = "manual/daemon-timeout"
        self.queue.add_task(reviewed(_task(task_id)))
        rejected = subprocess.CompletedProcess(
            [], 1, b"", (
                b"t3-thread: `/home/user/.local/bin/codex app-server daemon start` "
                b"timed out after 10s\n"
            ),
        )
        with mock.patch("bonus_drain.adapters.run_bounded_process", return_value=rejected):
            for _attempt in range(2):
                with self.assertRaises(dispatcher.KnownDispatchFailure):
                    dispatcher.dispatch(
                        cfg, self.queue, task_id=task_id, eligibility_key=key,
                        requested_provider="codex",
                    )
                self.assertIsNone(self.queue.claim_for(task_id, key))

    def test_codex_ignores_task_mcp_scoping_for_explicit_and_auto_kicks(self) -> None:
        codex = config_module.ProviderConfig("codex", config_module.DispatchBinding("router", "codex"), frozenset())
        cfg = replace(self.config, providers=(codex,), limits=())
        cases = (
            ("codex-none-explicit", "none", "codex"),
            ("codex-none-auto", "none", "auto"),
            ("codex-connectors-explicit", "project-connectors", "codex"),
            ("codex-connectors-auto", "project-connectors", "auto"),
        )
        for task_id, task_mcp, requested_provider in cases:
            with self.subTest(task_id=task_id, requested_provider=requested_provider):
                self.queue.add_task(reviewed(_task(task_id, mcp=task_mcp)))
                seen: list[list[str]] = []

                def router(
                    argv: list[str], *, seen=seen, task_id=task_id, **_kwargs: object,
                ) -> dict[str, object]:
                    seen.append(argv)
                    if "--dry-run" in argv:
                        return {"provider_id": "codex"}
                    return {"dispatch": {"job_id": f"job-{task_id}", "launched": True}}

                result = dispatcher.dispatch(
                    cfg, self.queue, task_id=task_id, eligibility_key=f"manual/{task_id}",
                    requested_provider=requested_provider, router_call=router,
                )
                self.assertEqual(result.provider_id, "codex")
                launch = next(argv for argv in seen if "--dry-run" not in argv)
                self.assertNotIn("--mcp-config", launch)
                self.assertNotIn("--strict-mcp-config", launch)

    def test_claude_preserves_task_mcp_scoping(self) -> None:
        claude = config_module.ProviderConfig("claude", config_module.DispatchBinding("router", "claude"), frozenset())
        cfg = replace(self.config, providers=(claude,), limits=())
        source_mcp = self.root / "claude-mcp.json"
        source_mcp.write_text(
            json.dumps({"mcpServers": {"project": {"command": "project-mcp"}}}),
            encoding="utf-8",
        )
        self.queue.add_task(reviewed(_task("claude-mcp", mcp=str(source_mcp))))
        seen: list[list[str]] = []

        def router(argv: list[str], **_kwargs: object) -> dict[str, object]:
            seen.append(argv)
            return {"dispatch": {"job_id": "job-claude-mcp", "launched": True}}

        dispatcher.dispatch(
            cfg, self.queue, task_id="claude-mcp", eligibility_key="manual/claude-mcp",
            requested_provider="claude", router_call=router,
        )
        launch = seen[0]
        self.assertNotIn("--mcp-config", launch)
        self.assertNotIn("--strict-mcp-config", launch)
        self.assertEqual(launch[launch.index("--engine") + 1], "claudeAgent")

    def test_named_mcp_selection_does_not_validate_unselected_server_secrets(self) -> None:
        claude = config_module.ProviderConfig("claude", config_module.DispatchBinding("router", "claude"), frozenset())
        cfg = replace(self.config, providers=(claude,), limits=())
        project = self.root / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".mcp.json").write_text(json.dumps({
            "mcpServers": {
                "wiki": {"command": "wiki-mcp"},
                "unselected": {
                    "command": "other-mcp",
                    "env": {"TOKEN": "literal-local-secret"},
                },
            },
        }), encoding="utf-8")
        self.queue.add_task(reviewed(_task("named-mcp", cwd=str(project), mcp="wiki")))
        seen: list[list[str]] = []

        def router(argv: list[str], **_kwargs: object) -> dict[str, object]:
            seen.append(argv)
            return {"dispatch": {"job_id": "job-named-mcp", "launched": True}}

        with mock.patch.dict(os.environ, {"HOME": str(self.root)}, clear=False):
            dispatcher.dispatch(
                cfg, self.queue, task_id="named-mcp", eligibility_key="manual/named-mcp",
                requested_provider="claude", router_call=router,
            )

        launch = seen[0]
        self.assertNotIn("--mcp-config", launch)
        self.assertNotIn("--strict-mcp-config", launch)

    def test_set_mcp_updates_the_canonical_task_contract(self) -> None:
        with _capture_cli_json() as payloads:
            code = isolated_cli([
                "set-mcp", "--database", str(self.config.database),
                "portable", "none", "--json",
            ])

        self.assertEqual(code, 2)
        self.assertIn("invalid choice", payloads[0]["error"])

    def test_set_providers_updates_the_canonical_task_contract(self) -> None:
        with _capture_cli_json() as payloads:
            code = isolated_cli([
                "set-providers", "--database", str(self.config.database),
                "portable", "beta", "--json",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(payloads[0]["task"]["allowed_providers"], ["beta"])
        self.assertEqual(self.queue.task("portable").allowed_providers, ("beta",))

    def test_router_mcp_flag_parser_rejection_is_known_not_launched(self) -> None:
        claude = config_module.ProviderConfig("claude", config_module.DispatchBinding("router", "claude"), frozenset())
        cfg = replace(self.config, providers=(claude,), limits=())
        source_mcp = self.root / "rejected-mcp.json"
        source_mcp.write_text(
            json.dumps({"mcpServers": {"project": {"command": "project-mcp"}}}),
            encoding="utf-8",
        )
        self.queue.add_task(reviewed(_task("mcp-parser-rejection", mcp=str(source_mcp))))
        rejected = subprocess.CompletedProcess(
            [], 2, b"", (
                b"t3-thread: --mcp-config is a claude only flag, but this task routed "
                b"to codex: rerun with --provider claude, or drop --mcp-config"
            ),
        )
        with (
            mock.patch('bonus_drain.adapters.run_bounded_process', return_value=rejected) as launched,
            self.assertRaises(dispatcher.KnownDispatchFailure),
        ):
            dispatcher.dispatch(
                cfg, self.queue, task_id="mcp-parser-rejection",
                eligibility_key="manual/mcp-parser-rejection", requested_provider="claude",
            )
        argv = launched.call_args.args[0]
        self.assertNotIn("--mcp-config", argv)
        self.assertEqual(self.queue.claim_for("mcp-parser-rejection"), None)

    def test_racing_concrete_kicks_produce_exactly_one_router_launch(self) -> None:
        from bonus_drain import kick

        launches: list[list[str]] = []
        lock = threading.Lock()

        def router(argv: list[str], **_kwargs: object) -> dict[str, object]:
            with lock:
                launches.append(argv)
            time.sleep(0.05)
            return {"dispatch": {"job_id": "job-race", "launched": True}}

        def invoke() -> str:
            try:
                return kick.kick_task(
                    self.config, self.queue, task_id="portable", requested_provider="alpha",
                    eligibility_key="manual/race", now_epoch=NOW, router_call=router,
                ).job_id
            except dispatcher.AlreadyClaimed:
                return "already-claimed"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _index: invoke(), range(2)))
        self.assertCountEqual(outcomes, ["job-race", "already-claimed"])
        self.assertEqual(len(launches), 1)

    def test_json_config_graph_is_authoritative_but_queue_allows_database_compatibility(self) -> None:
        configured_db = self.config.database
        poison_db = self.root / "poison" / "queue.db"
        raw = {"schema_version": 1, "database": str(configured_db), "cache_dir": str(self.config.cache_dir), "record_command": ["/bin/true"], "adapters": [{"id": "router", "kind": "t3-thread", "argv": ["/bin/true"]}], "providers": [{
                "id": "alpha", "dispatch": {"adapter_id": "router", "provider": "alpha-engine"},
            }], "limits": [{
                "id": "alpha-weekly", "provider_id": "alpha", "window_seconds": 604800,
                "ceiling_percent": 95, "lead_seconds": 20000, "batch_size": 1,
            }], "viewer": {}, "pr_exceptions": [], "secret_refs": [{"id": "proxy", "source": "file", "path": "/unused/proxy.env"}], "proxy_secret_ref": "proxy"}
        self.config.source_path.write_text(json.dumps(raw), encoding="utf-8")
        validated = config_module.validate_config(
            raw, source_dir=self.root, source_path=self.config.source_path,
            environ={"BONUS_DB": str(poison_db)},
        )
        self.assertEqual(validated.database, configured_db.resolve())

        with (
            mock.patch.dict(os.environ, {"BONUS_DB": str(poison_db)}, clear=False),
            _capture_cli_json() as payloads,
        ):
            code = isolated_cli(["queue", "--config", str(self.config.source_path), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(payloads), 1)
        self.assertIn("portable", {row["id"] for row in payloads[0]["tasks"]})
        self.assertFalse(poison_db.exists())

        compatibility_db = self.root / "other.db"
        compatibility_queue = db.QueueDB(compatibility_db)
        compatibility_queue.initialize()
        compatibility_queue.add_task(reviewed(_task("queue-compatibility")))
        with _capture_cli_json() as payloads:
            code = isolated_cli([
                "queue", "--config", str(self.config.source_path),
                "--database", str(compatibility_db), "--json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(
            {row["id"] for row in payloads[0]["tasks"]},
            {"queue-compatibility"},
        )

        parser = cli.build_parser()
        graph_commands = {
            "viewer": ["viewer", "--config", str(self.config.source_path)],
            "scout": ["scout", "--config", str(self.config.source_path)],
            "refresh": ["refresh", "--config", str(self.config.source_path)],
            "dispatch": ["dispatch", "portable", "alpha", "--config", str(self.config.source_path)],
        }
        for command, argv in graph_commands.items():
            with self.subTest(command=command):
                graph_config = cli._load_config(
                    parser.parse_args(argv), graph_required=True,
                )
                self.assertEqual(graph_config.database, configured_db.resolve())
                self.assertEqual(graph_config.provider("alpha").id, "alpha")

        for command, argv in graph_commands.items():
            if command == "refresh":
                continue
            with self.subTest(command=command, database_override="rejected"):
                arguments = parser.parse_args([
                    *argv, "--database", str(compatibility_db),
                ])
                with self.assertRaises(cli.CLIError):
                    cli._load_config(arguments, graph_required=True)


if __name__ == "__main__":
    unittest.main()
