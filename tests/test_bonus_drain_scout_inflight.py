"""Regression contract for the global Bonus Drain in-flight gate."""

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
ELIGIBILITY_KEY = "alpha-account/alpha-weekly/2000001000"


def _task(task_id: str) -> dict[str, object]:
    return {
        "id": task_id,
        "title": task_id,
        "kind": "oneoff",
        "priority": 1,
        "cwd": "/tmp",
        "goal": f"run {task_id}",
        "active": True,
    }


class ScoutInflightGateTests(unittest.TestCase):
    def test_running_job_prevents_any_new_scout_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = db.QueueDB(root / "queue.db")
            queue.initialize()
            queue.add_task(_task("already-running"))
            queue.add_task(_task("would-be-dispatched"))
            queue.record(
                "already-running", ELIGIBILITY_KEY, status="dispatched",
                provider_id="alpha", account_id="alpha-account", cycle=2_000_001_000,
            )
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
                        "alpha", config_module.DispatchBinding("router", "alpha"),
                        frozenset(), "single",
                    ),
                ),
                plans=(config_module.PlanConfig("alpha-plan", "alpha"),),
                accounts=(config_module.AccountConfig("alpha-account", "alpha", "alpha-plan"),),
                limits=(
                    config_module.LimitConfig(
                        "alpha-weekly", "alpha-plan", 604800, 95, 20000, 1,
                    ),
                ),
                viewer={},
                pr_exceptions=(),
                usage_max_age_seconds=3600,
                cache_dir=root / "cache",
            )
            snapshots = {
                ("alpha", "alpha-account"): usage.UsageSnapshot(
                    "alpha", "alpha-account", NOW,
                    {"alpha-weekly": {"used_percent": 20, "resets_at": NOW + 1_000}},
                ),
            }

            with mock.patch.object(scout, "read_all", return_value=snapshots):
                with mock.patch.object(scout, "dispatch") as dispatch_mock:
                    report = scout.run_once(config, queue, now_epoch=NOW)

            self.assertEqual(report.dispatched, ())
            self.assertEqual(report.errors, ())
            dispatch_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
