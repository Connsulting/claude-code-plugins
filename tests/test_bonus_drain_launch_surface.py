"""Launch surface contracts: background stays unchanged, t3 launches T3 Code threads."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import config as config_module, db, dispatcher  # noqa: E402


T3_THREAD_ID = "thr_01J9ZQ7K3V8M2N4P6R8T0W2Y4A"


def _task(task_id: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": task_id, "title": task_id, "kind": "oneoff", "priority": 2,
        "cwd": "/tmp", "goal": f"run {task_id}", "active": True,
    }
    values.update(overrides)
    return values


def _raw_config(root: Path, **top: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "schema_version": 1,
        "database": str(root / "state" / "queue.db"),
        "cache_dir": str(root / "cache"),
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
    raw.update(top)
    return raw


class LaunchSurfaceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def validate(self, raw: dict[str, object]) -> config_module.RuntimeConfig:
        return config_module.validate_config(
            raw, source_dir=self.root, source_path=self.root / "config.json",
        )

    def test_default_surface_is_background(self) -> None:
        cfg = self.validate(_raw_config(self.root))
        self.assertEqual(cfg.launch_surface, "background")
        self.assertIsNone(cfg.providers[0].launch_surface)
        self.assertEqual(cfg.launch_surface_for(cfg.providers[0]), "background")

    def test_global_t3_and_provider_override(self) -> None:
        cfg = self.validate(_raw_config(self.root, launch_surface="t3"))
        self.assertEqual(cfg.launch_surface_for(cfg.providers[0]), "t3")

        raw = _raw_config(self.root, launch_surface="t3")
        raw["providers"][0]["launch_surface"] = "background"  # type: ignore[index]
        cfg = self.validate(raw)
        self.assertEqual(cfg.providers[0].launch_surface, "background")
        self.assertEqual(cfg.launch_surface_for(cfg.providers[0]), "background")

        raw = _raw_config(self.root)
        raw["providers"][0]["launch_surface"] = "t3"  # type: ignore[index]
        cfg = self.validate(raw)
        self.assertEqual(cfg.launch_surface, "background")
        self.assertEqual(cfg.launch_surface_for(cfg.providers[0]), "t3")

    def test_unknown_surface_values_are_rejected(self) -> None:
        with self.assertRaises(config_module.ConfigError):
            self.validate(_raw_config(self.root, launch_surface="desktop"))
        raw = _raw_config(self.root)
        raw["providers"][0]["launch_surface"] = "T3"  # type: ignore[index]
        with self.assertRaises(config_module.ConfigError):
            self.validate(raw)

    def test_example_config_declares_the_background_default(self) -> None:
        example = json.loads((SKILL_ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example.get("launch_surface"), "background")
        schema = json.loads((SKILL_ROOT / "config.schema.json").read_text(encoding="utf-8"))
        top = schema["properties"]["launch_surface"]
        provider = schema["$defs"]["provider"]["properties"]["launch_surface"]
        self.assertEqual(top["enum"], ["background", "t3"])
        self.assertEqual(top["default"], "background")
        self.assertEqual(provider["enum"], ["background", "t3"])


class LaunchSurfaceDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
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
                    "claude", config_module.DispatchBinding("router", "claude"),
                    frozenset(), "single",
                ),
            ),
            plans=(), accounts=(), limits=(),
            viewer={}, pr_exceptions=(),
            usage_max_age_seconds=3600,
            cache_dir=self.root / "cache",
        )
        self.queue = db.QueueDB(self.config.database)
        self.queue.initialize()
        self.seen: list[list[str]] = []

    def router(self, argv: list[str], **_kwargs: object) -> dict[str, object]:
        self.seen.append(list(argv))
        if "--dry-run" in argv:
            return {"provider_id": "claude"}
        return {"dispatch": {"job_id": T3_THREAD_ID, "launched": True}}

    def run_task(self, cfg: config_module.RuntimeConfig, task_id: str, *, provider: str = "claude",
                 **task: object) -> dispatcher.DispatchResult:
        self.queue.add_task(_task(task_id, **task))
        return dispatcher.dispatch(
            cfg, self.queue, task_id=task_id, eligibility_key=f"manual/{task_id}",
            requested_provider=provider, router_call=self.router,
        )

    def launch(self) -> list[str]:
        return next(argv for argv in self.seen if "--dry-run" not in argv)

    def test_background_launch_argv_is_unchanged(self) -> None:
        result = self.run_task(self.config, "plain")
        self.assertNotIn("--surface", self.launch())
        self.assertEqual(result.surface, "background")
        self.assertEqual(self.queue.inflight()[0].surface, "background")

    def test_t3_surface_adds_flag_before_prompt(self) -> None:
        cfg = replace(self.config, launch_surface="t3")
        result = self.run_task(cfg, "t3-plain")
        launch = self.launch()
        index = launch.index("--surface")
        self.assertEqual(launch[index + 1], "t3")
        self.assertLess(index, launch.index("--json"))
        self.assertEqual(result.surface, "t3")

    def test_provider_override_beats_global(self) -> None:
        provider = replace(self.config.providers[0], launch_surface="background")
        cfg = replace(self.config, launch_surface="t3", providers=(provider,))
        self.run_task(cfg, "override-background")
        self.assertNotIn("--surface", self.launch())

        self.seen.clear()
        provider = replace(self.config.providers[0], launch_surface="t3")
        cfg = replace(self.config, providers=(provider,))
        self.run_task(cfg, "override-t3")
        self.assertIn("--surface", self.launch())

    def test_classification_dry_run_never_passes_surface(self) -> None:
        cfg = replace(self.config, launch_surface="t3")
        self.run_task(cfg, "auto-t3", provider="auto")
        classification = next(argv for argv in self.seen if "--dry-run" in argv)
        self.assertNotIn("--surface", classification)
        self.assertIn("--surface", self.launch())

    def test_t3_drops_claude_mcp_scope_and_notes_it(self) -> None:
        source_mcp = self.root / "claude-mcp.json"
        source_mcp.write_text(
            json.dumps({"mcpServers": {"project": {"command": "project-mcp"}}}),
            encoding="utf-8",
        )
        cfg = replace(self.config, launch_surface="t3")
        self.run_task(cfg, "t3-mcp", mcp=str(source_mcp))
        launch = self.launch()
        self.assertNotIn("--mcp-config", launch)
        self.assertNotIn("--strict-mcp-config", launch)
        mcp_dir = cfg.state_dir / "mcp"
        self.assertFalse(mcp_dir.exists() and any(mcp_dir.iterdir()))
        run = self.queue.inflight()[0]
        self.assertEqual(run.surface, "t3")
        self.assertIn("MCP scope", run.summary or "")
        self.assertIn("dropped", run.summary or "")

    def test_background_claude_mcp_scope_is_kept_without_a_drop_note(self) -> None:
        source_mcp = self.root / "claude-mcp.json"
        source_mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        self.run_task(self.config, "bg-mcp", mcp=str(source_mcp))
        self.assertIn("--mcp-config", self.launch())
        self.assertNotIn("dropped", self.queue.inflight()[0].summary or "")

    def test_t3_thread_id_is_recorded_verbatim(self) -> None:
        cfg = replace(self.config, launch_surface="t3")
        result = self.run_task(cfg, "thread-id")
        self.assertEqual(result.job_id, T3_THREAD_ID)
        run = self.queue.inflight()[0]
        self.assertEqual(run.router_job_id, T3_THREAD_ID)
        self.assertEqual(run.surface, "t3")


class LaunchSurfaceMigrationTests(unittest.TestCase):
    def test_existing_runs_table_gains_surface_column(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.db"
            queue = db.QueueDB(path)
            queue.initialize()
            with sqlite3.connect(path) as connection:
                connection.execute("ALTER TABLE runs DROP COLUMN surface")
            db.QueueDB(path).initialize()
            with sqlite3.connect(path) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
            self.assertIn("surface", columns)


if __name__ == "__main__":
    unittest.main()
