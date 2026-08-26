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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Iterator
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import adapters, cli, config as config_module, db, dispatcher  # noqa: E402


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
            "router", "agent-router", (str(self.root / "bin" / "agent-router"),),
            timeout_seconds=0.2, max_output_bytes=1024,
        )
        self.config = config_module.RuntimeConfig(
            schema_version=1,
            source_path=self.root / "config.json",
            database=self.root / "state" / "queue.db",
            record_command=("/bin/true",),
            secret_refs=(),
            adapters=(router,),
            providers=(
                config_module.ProviderConfig(
                    "alpha", config_module.DispatchBinding("router", "alpha-engine"),
                    frozenset({"legacy-exclusive", "cpu"}), "single",
                ),
                config_module.ProviderConfig(
                    "beta", config_module.DispatchBinding("router", "beta-engine"),
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
            limits=(
                config_module.LimitConfig("alpha-weekly", "alpha-plan", 604800, 95, 20000, 1),
                config_module.LimitConfig("beta-weekly", "beta-plan", 604800, 95, 20000, 1),
            ),
            viewer={},
            pr_exceptions=(),
            usage_max_age_seconds=3600,
            cache_dir=self.root / "cache",
        )
        self.queue = db.QueueDB(self.config.database)
        self.queue.initialize()
        self.queue.add_task(_task("portable"))

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
            self.assertEqual(cli.main(["dispatch", "portable", "alpha", "--json"]), 0)
            self.assertEqual(cli.main(["run-now", "portable", "alpha", "--json"]), 0)
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

    def test_manual_kick_is_router_only_and_activation_is_only_an_account_side_effect(self) -> None:
        from bonus_drain import kick

        activation = config_module.AdapterConfig(
            "switch", "activation", (str(self.root / "bin" / "account-switch"),),
        )
        alpha = replace(self.config.accounts[0], activation_adapter_id="switch")
        cfg = replace(self.config, adapters=(*self.config.adapters, activation), accounts=(alpha, self.config.accounts[1]))
        seen_argv: list[list[str]] = []
        activation_events: list[tuple[str, str]] = []

        def router(argv: list[str], **_kwargs: object) -> dict[str, object]:
            seen_argv.append(argv)
            return {"dispatch": {"job_id": "job-router", "launched": True}}

        result = kick.kick_task(
            cfg, self.queue, task_id="portable", requested_provider="alpha",
            eligibility_key="manual/router-only", now_epoch=NOW,
            router_call=router,
            activation_call=lambda action, account: activation_events.append((action, account)),
        )
        self.assertEqual(result.job_id, "job-router")
        self.assertEqual(activation_events, [("activate", "alpha-account")])
        self.assertEqual(len(seen_argv), 1)
        argv = seen_argv[0]
        self.assertEqual(argv[0], str(self.root / "bin" / "agent-router"))
        self.assertNotIn("--dry-run", argv)
        flattened = "\0".join(argv).lower()
        for forbidden in ("run-now.sh", "/bash", "claude", "codex", "grok", "account-switch"):
            self.assertNotIn(forbidden, flattened)

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
            with self.subTest(requested=requested):
                with self.assertRaises(dispatcher.DispatchError):
                    dispatcher.dispatch(
                        cfg, self.queue, task_id="portable",
                        eligibility_key=f"manual/{requested}", requested_provider=requested,
                        router_call=must_not_run,
                    )
        self.assertEqual(called, [])
        self.assertEqual(self.queue.claims(), [])

    def test_auto_classifier_uncertainty_is_dry_run_preclaim_and_retry_safe(self) -> None:
        cases: tuple[object, ...] = (
            "not-json",
            subprocess.TimeoutExpired(["agent-router"], 0.1),
            adapters.ProcessOutputLimit("too much output"),
        )
        for index, outcome in enumerate(cases):
            task_id = f"classify-{index}"
            self.queue.add_task(_task(task_id))
            calls: list[list[str]] = []

            def uncertain(argv: list[str], **_kwargs: object) -> object:
                calls.append(argv)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            for _attempt in range(2):
                with self.assertRaises(dispatcher.DispatchError) as raised:
                    dispatcher.dispatch(
                        self.config, self.queue, task_id=task_id,
                        eligibility_key=f"manual/classifier-{index}", requested_provider="auto",
                        router_call=uncertain,
                    )
                self.assertNotIsInstance(raised.exception, dispatcher.AmbiguousDispatch)
                self.assertEqual(self.queue.claim_for(task_id), None)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all("--dry-run" in argv for argv in calls))

    def test_post_spawn_uncertainty_retains_one_ambiguous_claim_and_never_relaunches(self) -> None:
        cases: dict[str, object] = {
            "timeout": subprocess.TimeoutExpired(["agent-router"], 0.1),
            "overflow": adapters.ProcessOutputLimit("too much output"),
            "nonzero": subprocess.CompletedProcess([], 7, b"", b"router failed"),
            "malformed": subprocess.CompletedProcess([], 0, b"not-json", b""),
            "missing-identity": subprocess.CompletedProcess([], 0, b"{}", b""),
        }
        for name, outcome in cases.items():
            task_id = f"launch-{name}"
            key = f"manual/{name}"
            self.queue.add_task(_task(task_id))
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
        self.queue.add_task(_task(task_id))
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
        with mock.patch.dict(os.environ, {"BONUS_TEST_ROUTER_SECRET": secret}, clear=False):
            with self.assertRaises(dispatcher.AmbiguousDispatch) as raised:
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

    def test_explicit_router_launched_false_is_known_not_launched_and_retry_safe(self) -> None:
        task_id = "known-negative"
        key = "manual/known-negative"
        self.queue.add_task(_task(task_id))
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
        codex = config_module.ProviderConfig(
            "codex", config_module.DispatchBinding("router", "codex"), frozenset(), "single",
        )
        cfg = replace(self.config, providers=(codex,), plans=(), accounts=(), limits=())
        task_id = "missing-codex"
        key = "manual/missing-codex"
        self.queue.add_task(_task(task_id))
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

    def test_codex_ignores_task_mcp_scoping_for_explicit_and_auto_kicks(self) -> None:
        codex = config_module.ProviderConfig(
            "codex", config_module.DispatchBinding("router", "codex"), frozenset(), "single",
        )
        cfg = replace(self.config, providers=(codex,), plans=(), accounts=(), limits=())
        cases = (
            ("codex-none-explicit", "none", "codex"),
            ("codex-none-auto", "none", "auto"),
            ("codex-connectors-explicit", "project-connectors", "codex"),
            ("codex-connectors-auto", "project-connectors", "auto"),
        )
        for task_id, task_mcp, requested_provider in cases:
            with self.subTest(task_id=task_id, requested_provider=requested_provider):
                self.queue.add_task(_task(task_id, mcp=task_mcp))
                seen: list[list[str]] = []

                def router(argv: list[str], **_kwargs: object) -> dict[str, object]:
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
        claude = config_module.ProviderConfig(
            "claude", config_module.DispatchBinding("router", "claude"), frozenset(), "single",
        )
        cfg = replace(self.config, providers=(claude,), plans=(), accounts=(), limits=())
        source_mcp = self.root / "claude-mcp.json"
        source_mcp.write_text(
            json.dumps({"mcpServers": {"project": {"command": "project-mcp"}}}),
            encoding="utf-8",
        )
        self.queue.add_task(_task("claude-mcp", mcp=str(source_mcp)))
        seen: list[list[str]] = []

        def router(argv: list[str], **_kwargs: object) -> dict[str, object]:
            seen.append(argv)
            return {"dispatch": {"job_id": "job-claude-mcp", "launched": True}}

        dispatcher.dispatch(
            cfg, self.queue, task_id="claude-mcp", eligibility_key="manual/claude-mcp",
            requested_provider="claude", router_call=router,
        )
        launch = seen[0]
        mcp_index = launch.index("--mcp-config")
        self.assertEqual(launch[mcp_index + 2], "--strict-mcp-config")
        self.assertEqual(
            json.loads(Path(launch[mcp_index + 1]).read_text(encoding="utf-8")),
            {"mcpServers": {"project": {"command": "project-mcp"}}},
        )

    def test_router_mcp_flag_parser_rejection_is_known_not_launched(self) -> None:
        claude = config_module.ProviderConfig(
            "claude", config_module.DispatchBinding("router", "claude"), frozenset(), "single",
        )
        cfg = replace(self.config, providers=(claude,), plans=(), accounts=(), limits=())
        source_mcp = self.root / "rejected-mcp.json"
        source_mcp.write_text(
            json.dumps({"mcpServers": {"project": {"command": "project-mcp"}}}),
            encoding="utf-8",
        )
        self.queue.add_task(_task("mcp-parser-rejection", mcp=str(source_mcp)))
        rejected = subprocess.CompletedProcess(
            [], 2, b"", (
                b"agent-router: --mcp-config is a claude only flag, but this task routed "
                b"to codex: rerun with --provider claude, or drop --mcp-config"
            ),
        )
        with mock.patch("bonus_drain.adapters.run_bounded_process", return_value=rejected) as launched:
            with self.assertRaises(dispatcher.KnownDispatchFailure):
                dispatcher.dispatch(
                    cfg, self.queue, task_id="mcp-parser-rejection",
                    eligibility_key="manual/mcp-parser-rejection", requested_provider="claude",
                )
        argv = launched.call_args.args[0]
        self.assertIn("--mcp-config", argv)
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
        raw = {
            "schema_version": 1,
            "database": str(configured_db),
            "cache_dir": str(self.config.cache_dir),
            "record_command": ["/bin/true"],
            "adapters": [{"id": "router", "kind": "agent-router", "argv": ["/bin/true"]}],
            "providers": [{
                "id": "alpha", "account_mode": "single",
                "dispatch": {"adapter_id": "router", "provider": "alpha-engine"},
            }],
            "plans": [{"id": "alpha-plan", "provider_id": "alpha"}],
            "accounts": [{"id": "alpha-account", "provider_id": "alpha", "plan_id": "alpha-plan"}],
            "limits": [{
                "id": "alpha-weekly", "plan_id": "alpha-plan", "window_seconds": 604800,
                "ceiling_percent": 95, "lead_seconds": 20000, "batch_size": 1,
            }],
            "viewer": {},
            "pr_exceptions": [],
        }
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
            code = cli.main(["queue", "--config", str(self.config.source_path), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(payloads), 1)
        self.assertIn("portable", {row["id"] for row in payloads[0]["tasks"]})
        self.assertFalse(poison_db.exists())

        compatibility_db = self.root / "other.db"
        compatibility_queue = db.QueueDB(compatibility_db)
        compatibility_queue.initialize()
        compatibility_queue.add_task(_task("queue-compatibility"))
        with _capture_cli_json() as payloads:
            code = cli.main([
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
