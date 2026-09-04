"""Regression contracts for the 2026-08-29 Bonus Drain review repairs."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import config as config_module, db, scout, usage  # noqa: E402


NOW = 2_000_000_000
RESET = NOW + 1_000
ELIGIBILITY_KEY = f"alpha-account/alpha-weekly/{RESET}"


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def task(
    task_id: str,
    *,
    kind: str = "oneoff",
    created_at: str | None = None,
) -> dict[str, object]:
    return {
        "id": task_id,
        "title": task_id,
        "kind": kind,
        "cadence": "weekly" if kind == "recurring" else None,
        "priority": 2,
        "cwd": "/tmp",
        "goal": f"run {task_id}",
        "created_at": created_at or iso(NOW - 60),
        "active": True,
        "size": "small",
    }


def runtime(database: Path, *, router_path: str = "/bin/true") -> config_module.RuntimeConfig:
    router = config_module.AdapterConfig("router", "agent-router", (router_path,))
    return config_module.RuntimeConfig(
        schema_version=1,
        source_path=None,
        database=database,
        record_command=("/bin/true",),
        secret_refs=(),
        adapters=(router,),
        providers=(
            config_module.ProviderConfig(
                "alpha", config_module.DispatchBinding("router", "alpha"), frozenset(), "single",
            ),
        ),
        plans=(config_module.PlanConfig("alpha-plan", "alpha"),),
        accounts=(config_module.AccountConfig("alpha-account", "alpha", "alpha-plan"),),
        limits=(config_module.LimitConfig("alpha-weekly", "alpha-plan", 604_800, 95, 20_000, 6),),
        viewer={},
        pr_exceptions=(),
        usage_max_age_seconds=3_600,
        cache_dir=database.parent / "cache",
    )


def snapshots() -> dict[tuple[str, str], usage.UsageSnapshot]:
    return {
        ("alpha", "alpha-account"): usage.UsageSnapshot(
            "alpha", "alpha-account", NOW,
            {"alpha-weekly": {"used_percent": 20, "resets_at": RESET}},
        ),
    }


class BonusDrainReviewRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "queue.db")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_terminal_record_is_idempotent_and_rejects_a_conflicting_outcome(self) -> None:
        self.queue.add_task(task("terminal-once"))

        first = self.queue.record(
            "terminal-once", ELIGIBILITY_KEY, status="done", summary="proof",
        )
        replay = self.queue.record(
            "terminal-once", ELIGIBILITY_KEY, status="done", summary="proof",
        )

        self.assertEqual(replay.rowid_pk, first.rowid_pk)
        self.assertEqual(
            [event.status for event in self.queue.runs(task_id="terminal-once")],
            ["done"],
        )
        with self.assertRaisesRegex(db.QueueError, "already recorded as done"):
            self.queue.record(
                "terminal-once", ELIGIBILITY_KEY, status="skipped", summary="changed mind",
            )

    def test_keyed_terminal_replay_recognizes_legacy_cycle_history(self) -> None:
        self.queue.add_task(task("legacy-terminal"))
        first = self.queue.record(
            "legacy-terminal", None, cycle=RESET, status="done", summary="legacy proof",
        )

        replay = self.queue.record(
            "legacy-terminal", ELIGIBILITY_KEY, status="done", summary="legacy proof",
        )

        self.assertEqual(replay.rowid_pk, first.rowid_pk)
        self.assertEqual(len(self.queue.runs(task_id="legacy-terminal")), 1)

    def test_same_priority_tasks_are_ordered_by_time_eligible_not_kind(self) -> None:
        self.queue.add_task(task(
            "newer-oneoff", created_at=iso(NOW - 12 * 60 * 60),
        ))
        self.queue.add_task(task(
            "older-recurring", kind="recurring", created_at=iso(NOW - 30 * 24 * 60 * 60),
        ))
        self.queue.record(
            "older-recurring", "alpha-account/alpha-weekly/old", status="done",
            timestamp=iso(NOW - 5 * 24 * 60 * 60),
        )

        with mock.patch.object(db.time, "time", return_value=NOW):
            ordered = self.queue.eligible_tasks(RESET)

        self.assertEqual([item.id for item in ordered], ["older-recurring", "newer-oneoff"])

    def test_inflight_details_include_a_deterministic_age(self) -> None:
        self.queue.add_task(task("running"))
        self.queue.record(
            "running", ELIGIBILITY_KEY, status="dispatched", timestamp=iso(NOW - 125),
        )

        details = self.queue.inflight_details(now_epoch=NOW)

        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["task"], "running")
        self.assertEqual(details[0]["age_seconds"], 125)

    def test_terminal_from_an_older_cycle_does_not_hide_a_new_dispatch(self) -> None:
        self.queue.add_task(task("recurring-run", kind="recurring"))
        old_key = f"alpha-account/alpha-weekly/{RESET - 604_800}"
        self.queue.record(
            "recurring-run", old_key, status="dispatched", timestamp=iso(NOW - 500),
        )
        self.queue.record(
            "recurring-run", old_key, status="done", timestamp=iso(NOW - 400),
        )
        self.queue.record(
            "recurring-run", ELIGIBILITY_KEY, status="dispatched", timestamp=iso(NOW - 125),
        )

        details = self.queue.inflight_details(now_epoch=NOW)

        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["eligibility_key"], ELIGIBILITY_KEY)
        self.assertEqual(details[0]["age_seconds"], 125)

    def test_legacy_terminal_cycle_closes_the_matching_keyed_dispatch(self) -> None:
        self.queue.add_task(task("legacy-finish"))
        self.queue.record(
            "legacy-finish", ELIGIBILITY_KEY, status="dispatched", timestamp=iso(NOW - 125),
        )
        self.queue.record(
            "legacy-finish", None, cycle=RESET, status="done", timestamp=iso(NOW - 100),
        )

        self.assertEqual(self.queue.inflight_details(now_epoch=NOW), [])

    def test_scout_reports_the_global_inflight_blocker_with_task_ages(self) -> None:
        self.queue.add_task(task("running"))
        self.queue.add_task(task("would-run"))
        self.queue.record(
            "running", ELIGIBILITY_KEY, status="dispatched", timestamp=iso(NOW - 125),
        )

        with (
            mock.patch.object(scout, "read_all", return_value=snapshots()),
            mock.patch.object(scout, "dispatch") as dispatch_mock,
        ):
            report = scout.run_once(runtime(self.queue.path), self.queue, now_epoch=NOW)

        self.assertEqual(report.dispatched, ())
        self.assertEqual(report.errors, ())
        self.assertEqual(report.blockers[0]["kind"], "inflight")
        self.assertEqual(report.blockers[0]["runs"][0]["task"], "running")
        self.assertEqual(report.blockers[0]["runs"][0]["age_seconds"], 125)
        self.assertTrue(report.router_preflight[0]["available"])
        self.assertIsNotNone(report.router_preflight[0]["identity"])
        dispatch_mock.assert_not_called()

    def test_scout_preflights_router_before_claiming_any_task(self) -> None:
        self.queue.add_task(task("would-run"))
        missing = str(self.root / "missing-agent-router")

        with (
            mock.patch.object(scout, "read_all", return_value=snapshots()),
            mock.patch.object(scout, "dispatch") as dispatch_mock,
        ):
            report = scout.run_once(
                runtime(self.queue.path, router_path=missing), self.queue, now_epoch=NOW,
            )

        self.assertEqual(report.dispatched, ())
        self.assertEqual(report.blockers[0]["kind"], "router_unavailable")
        self.assertEqual(report.router_preflight[0]["executable"], missing)
        self.assertFalse(report.router_preflight[0]["available"])
        self.assertEqual(report.errors[0]["kind"], "router_unavailable")
        self.assertEqual(self.queue.claims(), [])
        dispatch_mock.assert_not_called()

    def test_scout_collapses_reconciliation_failure_to_one_tick_error(self) -> None:
        self.queue.add_task(task("ambiguous"))
        self.queue.add_task(task("would-run"))
        self.assertTrue(self.queue.claim(
            "ambiguous", ELIGIBILITY_KEY, "alpha", "alpha-account",
        ))
        self.queue.mark_ambiguous("ambiguous", ELIGIBILITY_KEY, detail="activation unknown")

        with (
            mock.patch.object(scout, "read_all", return_value=snapshots()),
            mock.patch.object(scout, "dispatch") as dispatch_mock,
        ):
            report = scout.run_once(runtime(self.queue.path), self.queue, now_epoch=NOW)

        self.assertEqual(report.dispatched, ())
        self.assertEqual(report.blockers[0]["kind"], "reconciliation_required")
        self.assertEqual(report.blockers[0]["tasks"], ["ambiguous"])
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0]["kind"], "reconciliation_required")
        dispatch_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
