"""Fable pins persist and launch as claude-fable-5-1."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import config as config_module, db, dispatcher  # noqa: E402


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


class FableModelCanonicalizationTests(unittest.TestCase):
    def test_aliases_rewrite_to_claude_fable_5_1(self) -> None:
        self.assertEqual(db.canonical_model("fable"), "claude-fable-5-1")
        self.assertEqual(db.canonical_model("claude-fable-5"), "claude-fable-5-1")
        self.assertEqual(db.canonical_model("  fable  "), "claude-fable-5-1")
        self.assertEqual(db.canonical_model("claude-fable-5-1"), "claude-fable-5-1")
        self.assertEqual(db.canonical_model("opus"), "opus")
        self.assertEqual(db.canonical_model("opus[1m]"), "opus[1m]")
        self.assertIsNone(db.canonical_model(None))
        self.assertIsNone(db.canonical_model(""))
        self.assertTrue(db.legacy_exclusive_model("fable"))
        self.assertTrue(db.legacy_exclusive_model("claude-fable-5-1"))

    def test_add_and_set_model_store_the_canonical_fable_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            queue = db.QueueDB(Path(temporary) / "queue.db")
            queue.initialize()
            added = queue.add_task(_task("from-add", model="fable"))
            self.assertEqual(added.model, "claude-fable-5-1")
            stored = queue.task("from-add")
            assert stored is not None
            self.assertEqual(stored.model, "claude-fable-5-1")

            queue.add_task(_task("from-set", model="opus"))
            queue.set_model("from-set", "claude-fable-5")
            updated = queue.task("from-set")
            assert updated is not None
            self.assertEqual(updated.model, "claude-fable-5-1")

            queue.set_model("from-set", "opus[1m]")
            cleared = queue.task("from-set")
            assert cleared is not None
            self.assertEqual(cleared.model, "opus[1m]")

    def test_dispatch_passes_claude_fable_5_1_for_legacy_fable_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            router = config_module.AdapterConfig(
                "router", "agent-router", (str(root / "bin" / "agent-router"),),
                timeout_seconds=0.2, max_output_bytes=1024,
            )
            config = config_module.RuntimeConfig(
                schema_version=1,
                source_path=root / "config.json",
                database=root / "queue.db",
                record_command=("/bin/true",),
                secret_refs=(),
                adapters=(router,),
                providers=(
                    config_module.ProviderConfig(
                        "claude", config_module.DispatchBinding("router", "claude"),
                        frozenset({"legacy-exclusive"}), "single",
                    ),
                ),
                plans=(config_module.PlanConfig("claude-plan", "claude"),),
                accounts=(
                    config_module.AccountConfig("claude-account", "claude", "claude-plan"),
                ),
                limits=(
                    config_module.LimitConfig(
                        "claude-weekly", "claude-plan", 604800, 95, 20000, 1,
                    ),
                ),
                viewer={},
                pr_exceptions=(),
                usage_max_age_seconds=3600,
                cache_dir=root / "cache",
            )
            queue = db.QueueDB(config.database)
            queue.initialize()
            queue.add_task(_task("legacy-fable", model="opus"))
            with sqlite3.connect(queue.path) as connection:
                connection.execute(
                    "UPDATE tasks SET model=? WHERE id=?",
                    ("fable", "legacy-fable"),
                )
            leftover = queue.task("legacy-fable")
            assert leftover is not None
            self.assertEqual(leftover.model, "fable")

            seen: list[list[str]] = []

            def router_call(argv: list[str], **_kwargs: object) -> dict[str, object]:
                seen.append(argv)
                return {"dispatch": {"job_id": "job-fable", "launched": True}}

            dispatcher.dispatch(
                config, queue, task_id="legacy-fable",
                eligibility_key="manual/legacy-fable", requested_provider="claude",
                router_call=router_call,
            )
            launch = next(argv for argv in seen if "--dry-run" not in argv)
            model_index = launch.index("--model")
            self.assertEqual(launch[model_index + 1], "claude-fable-5-1")
