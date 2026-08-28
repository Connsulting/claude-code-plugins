"""Regression contracts for exclusive-first Bonus Drain scout allocation."""

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


def _task(task_id: str, priority: int, *, claude_only: bool = False) -> dict[str, object]:
    return {
        "id": task_id,
        "title": task_id,
        "kind": "oneoff",
        "priority": priority,
        "cwd": "/tmp",
        "goal": f"run {task_id}",
        "active": True,
        "claude_only": claude_only,
    }


class ExclusivePriorityTests(unittest.TestCase):
    def test_exclusive_work_precedes_portable_work_even_when_portable_batch_resets_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = db.QueueDB(root / "queue.db")
            queue.initialize()
            queue.add_task(_task("portable-p1", 1))
            queue.add_task(_task("portable-p2", 2))
            queue.add_task(_task("claude-only-p3", 3, claude_only=True))
            router = config_module.AdapterConfig("router", "agent-router", ("/bin/true",))
            config = config_module.RuntimeConfig(
                schema_version=1,
                source_path=None,
                database=queue.path,
                record_command=("/bin/true",),
                secret_refs=(),
                adapters=(router,),
                providers=(
                    config_module.ProviderConfig(
                        "claude", config_module.DispatchBinding("router", "claude"),
                        frozenset({"legacy-exclusive"}), "single",
                    ),
                    config_module.ProviderConfig(
                        "codex", config_module.DispatchBinding("router", "codex"),
                        frozenset(), "single",
                    ),
                ),
                plans=(
                    config_module.PlanConfig("claude-plan", "claude"),
                    config_module.PlanConfig("codex-plan", "codex"),
                ),
                accounts=(
                    config_module.AccountConfig("claude-account", "claude", "claude-plan"),
                    config_module.AccountConfig("codex-account", "codex", "codex-plan"),
                ),
                limits=(
                    config_module.LimitConfig(
                        "claude-weekly", "claude-plan", 604800, 95, 20000, 1,
                    ),
                    config_module.LimitConfig(
                        "codex-weekly", "codex-plan", 604800, 95, 20000, 1,
                    ),
                ),
                viewer={},
                pr_exceptions=(),
                usage_max_age_seconds=3600,
                cache_dir=root / "cache",
            )
            snapshots = {
                ("codex", "codex-account"): usage.UsageSnapshot(
                    "codex", "codex-account", NOW,
                    {"codex-weekly": {"used_percent": 20, "resets_at": NOW + 1000}},
                ),
                ("claude", "claude-account"): usage.UsageSnapshot(
                    "claude", "claude-account", NOW,
                    {"claude-weekly": {"used_percent": 20, "resets_at": NOW + 2000}},
                ),
            }

            with mock.patch.object(scout, "read_all", return_value=snapshots):
                tick = scout.plan_tick(config, queue, now_epoch=NOW)

            self.assertEqual(
                [task.id for task in tick.allocations[("claude", "claude-account")]],
                ["claude-only-p3"],
            )
            self.assertEqual(
                [task.id for task in tick.allocations[("codex", "codex-account")]],
                ["portable-p1"],
            )


if __name__ == "__main__":
    unittest.main()
