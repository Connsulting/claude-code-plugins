"""Scout cadence contracts: host-load gate, tick lock, slot-freed marker, and path unit."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import cli, config as config_module, db, lifecycle, reconcile, scout  # noqa: E402
from tests.test_bonus_drain_review_repairs import (  # noqa: E402
    ELIGIBILITY_KEY, NOW, RESET, runtime, snapshots, task, verified_outcome,
)


GIB = 2 ** 30


def _gate(**changes: object) -> config_module.HostLoadGateConfig:
    values: dict[str, object] = {
        "enabled": True,
        "cpu_some_avg60_max": 40.0,
        "mem_available_min_gib": 4.0,
        "load_per_cpu_max": 1.5,
    }
    values.update(changes)
    return config_module.HostLoadGateConfig(**values)


def _load(
    cpu: float | None = 5.0,
    mem_gib: float | None = 64.0,
    load1: float | None = 1.0,
    cpu_count: int | None = 16,
) -> scout.HostLoad:
    return scout.HostLoad(
        cpu_some_avg60=cpu,
        mem_available_bytes=None if mem_gib is None else int(mem_gib * GIB),
        load1=load1,
        cpu_count=cpu_count,
    )


def _pressured() -> scout.HostLoad:
    return _load(cpu=80.0)


def _calm() -> scout.HostLoad:
    return _load(cpu=5.0)


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


@contextmanager
def _capture_cli_json() -> Iterator[list[object]]:
    payloads: list[object] = []
    with mock.patch.object(
        cli, "_json", side_effect=lambda value, **_kwargs: payloads.append(value),
    ):
        yield payloads


class HostPressureVerdictTests(unittest.TestCase):
    def test_psi_above_threshold_blocks_with_measured_value(self) -> None:
        blocker = scout.host_pressure(_gate(), _load(cpu=55.1))
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker["kind"], "host_pressure")
        self.assertEqual(blocker["source"], "psi")
        self.assertAlmostEqual(blocker["cpu_some_avg60"], 55.1)
        self.assertTrue(blocker["reasons"])
        self.assertIsInstance(blocker["message"], str)
        self.assertTrue(blocker["message"])
        self.assertIn("thresholds", blocker)

    def test_psi_at_or_below_threshold_with_ample_memory_is_clear(self) -> None:
        self.assertIsNone(scout.host_pressure(_gate(), _load(cpu=40.0)))
        self.assertIsNone(scout.host_pressure(_gate(), _load(cpu=12.5)))

    def test_psi_above_threshold_is_not_masked_by_calm_loadavg(self) -> None:
        blocker = scout.host_pressure(_gate(), _load(cpu=41.0, load1=0.1))
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker["source"], "psi")

    def test_low_memory_blocks_even_with_calm_cpu(self) -> None:
        blocker = scout.host_pressure(_gate(), _load(cpu=1.0, mem_gib=2.0))
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker["kind"], "host_pressure")
        self.assertAlmostEqual(blocker["mem_available_gib"], 2.0, places=3)

    def test_memory_at_minimum_is_clear(self) -> None:
        self.assertIsNone(scout.host_pressure(_gate(), _load(cpu=1.0, mem_gib=4.0)))

    def test_missing_psi_falls_back_to_loadavg_above_ratio(self) -> None:
        # 34.1 / 16 = 2.13 > 1.5
        blocker = scout.host_pressure(_gate(), _load(cpu=None, load1=34.1, cpu_count=16))
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker["kind"], "host_pressure")
        self.assertEqual(blocker["source"], "loadavg")
        self.assertAlmostEqual(blocker["load_per_cpu"], 34.1 / 16, places=3)

    def test_missing_psi_falls_back_to_loadavg_below_ratio(self) -> None:
        # 16 / 16 = 1.0 <= 1.5
        self.assertIsNone(
            scout.host_pressure(_gate(), _load(cpu=None, load1=16.0, cpu_count=16)),
        )

    def test_nothing_readable_gives_no_verdict(self) -> None:
        self.assertIsNone(
            scout.host_pressure(
                _gate(), _load(cpu=None, mem_gib=None, load1=None, cpu_count=None),
            ),
        )

    def test_disabled_gate_never_blocks(self) -> None:
        load = _load(cpu=99.0, mem_gib=0.5, load1=500.0, cpu_count=4)
        self.assertIsNotNone(scout.host_pressure(_gate(), load))
        self.assertIsNone(scout.host_pressure(_gate(enabled=False), load))

    def test_thresholds_are_honored_from_the_gate(self) -> None:
        load = _load(cpu=30.0)
        self.assertIsNone(scout.host_pressure(_gate(), load))
        self.assertIsNotNone(scout.host_pressure(_gate(cpu_some_avg60_max=20.0), load))


class ReadHostLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.proc = Path(self.temporary.name)

    def _write_meminfo_and_loadavg(self) -> None:
        (self.proc / "meminfo").write_text(
            "MemTotal:       65536000 kB\n"
            "MemFree:         1024000 kB\n"
            "MemAvailable:    7340032 kB\n"
            "Buffers:          123456 kB\n",
            encoding="utf-8",
        )
        (self.proc / "loadavg").write_text("34.10 30.00 28.00 5/1234 999\n", encoding="utf-8")

    def test_reads_psi_meminfo_and_loadavg(self) -> None:
        (self.proc / "pressure").mkdir()
        (self.proc / "pressure" / "cpu").write_text(
            "some avg10=61.20 avg60=55.10 avg300=40.00 total=123\n"
            "full avg10=10.00 avg60=9.00 avg300=8.00 total=45\n",
            encoding="utf-8",
        )
        self._write_meminfo_and_loadavg()

        load = scout.read_host_load(self.proc, cpu_count=lambda: 16)

        self.assertAlmostEqual(load.cpu_some_avg60, 55.10)
        self.assertEqual(load.mem_available_bytes, 7340032 * 1024)
        self.assertAlmostEqual(load.load1, 34.10)
        self.assertEqual(load.cpu_count, 16)

    def test_missing_psi_still_reads_loadavg(self) -> None:
        self._write_meminfo_and_loadavg()

        load = scout.read_host_load(self.proc, cpu_count=lambda: 8)

        self.assertIsNone(load.cpu_some_avg60)
        self.assertAlmostEqual(load.load1, 34.10)
        self.assertEqual(load.mem_available_bytes, 7340032 * 1024)
        # The fallback path is live end to end: 34.1 / 8 exceeds 1.5.
        blocker = scout.host_pressure(_gate(), load)
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker["source"], "loadavg")

    def test_empty_proc_root_reads_nothing(self) -> None:
        load = scout.read_host_load(self.proc, cpu_count=lambda: None)

        self.assertIsNone(load.cpu_some_avg60)
        self.assertIsNone(load.mem_available_bytes)
        self.assertIsNone(load.load1)
        self.assertIsNone(scout.host_pressure(_gate(), load))

    def test_unparseable_sources_yield_none(self) -> None:
        (self.proc / "pressure").mkdir()
        (self.proc / "pressure" / "cpu").write_text("garbage\n", encoding="utf-8")
        (self.proc / "meminfo").write_text("MemTotal: 1 kB\n", encoding="utf-8")
        (self.proc / "loadavg").write_text("not-a-number\n", encoding="utf-8")

        load = scout.read_host_load(self.proc, cpu_count=lambda: 4)

        self.assertIsNone(load.cpu_some_avg60)
        self.assertIsNone(load.mem_available_bytes)
        self.assertIsNone(load.load1)


class HostLoadGateConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def validate(self, raw: dict[str, object]) -> config_module.RuntimeConfig:
        return config_module.validate_config(
            raw, source_dir=self.root, source_path=self.root / "config.json",
        )

    def test_absent_block_uses_defaults(self) -> None:
        gate = self.validate(_raw_config(self.root)).host_load_gate
        self.assertTrue(gate.enabled)
        self.assertEqual(gate.cpu_some_avg60_max, 40.0)
        self.assertEqual(gate.mem_available_min_gib, 4.0)
        self.assertEqual(gate.load_per_cpu_max, 1.5)
        self.assertEqual(gate, config_module.HostLoadGateConfig())

    def test_overrides_are_honored(self) -> None:
        gate = self.validate(_raw_config(self.root, host_load_gate={
            "cpu_some_avg60_max": 25,
            "mem_available_min_gib": 8.5,
            "load_per_cpu_max": 2,
        })).host_load_gate
        self.assertTrue(gate.enabled)
        self.assertEqual(gate.cpu_some_avg60_max, 25.0)
        self.assertEqual(gate.mem_available_min_gib, 8.5)
        self.assertEqual(gate.load_per_cpu_max, 2.0)

    def test_gate_can_be_disabled(self) -> None:
        gate = self.validate(
            _raw_config(self.root, host_load_gate={"enabled": False}),
        ).host_load_gate
        self.assertFalse(gate.enabled)
        self.assertEqual(gate.cpu_some_avg60_max, 40.0)

    def test_invalid_values_are_rejected(self) -> None:
        invalid = {
            "unknown key": {"surprise": 1},
            "non-bool enabled": {"enabled": "yes"},
            "integer enabled": {"enabled": 1},
            "cpu over 100": {"cpu_some_avg60_max": 100.5},
            "negative cpu": {"cpu_some_avg60_max": -1},
            "negative memory": {"mem_available_min_gib": -0.5},
            "zero load ratio": {"load_per_cpu_max": 0},
            "negative load ratio": {"load_per_cpu_max": -1.5},
            "string threshold": {"cpu_some_avg60_max": "40"},
            "not an object": ["enabled"],
        }
        for label, block in invalid.items():
            with self.subTest(label):
                with self.assertRaises(config_module.ConfigError):
                    self.validate(_raw_config(self.root, host_load_gate=block))


class ScoutTickCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "state" / "queue.db")
        self.queue.initialize()
        self.config = runtime(self.queue.path)
        self.router_calls: list[list[str]] = []

    def route(self, argv: list[str], **_kwargs: object) -> dict[str, object]:
        self.router_calls.append(list(argv))
        return {"dispatch": {"job_id": f"job-{len(self.router_calls)}", "launched": True}}

    def add_stale_inflight(self) -> None:
        """An abandoned dispatched attempt the router reports as completed."""

        self.queue.add_task(task("stale"))
        attempt = self.queue.claim(
            "stale", ELIGIBILITY_KEY, "alpha", "alpha-account", now_epoch=NOW,
        )
        self.assertIsNotNone(attempt)
        self.queue.record(
            "stale", ELIGIBILITY_KEY, attempt_id=attempt.id, status="dispatched",
            cycle=RESET, provider_id="alpha", account_id="alpha-account",
            router_job_id="job-stale",
        )

    def run_tick(self, reader=None, **kwargs):
        completed = {"rows": [{
            "provider": "alpha", "job_id": "job-stale", "state": "completed",
        }]}
        with mock.patch.object(reconcile, "execute_adapter", return_value=completed):
            with mock.patch.object(scout, "read_all", return_value=snapshots()):
                with mock.patch.object(
                    scout, "reconcile_inflight", wraps=scout.reconcile_inflight,
                ) as spy:
                    report = scout.run_once(
                        self.config, self.queue, now_epoch=NOW,
                        router_call=self.route, host_load_reader=reader, **kwargs,
                    )
        return report, spy


class GatedTickTests(ScoutTickCase):
    def test_unpressured_tick_dispatches_ready_work(self) -> None:
        self.queue.add_task(task("ready"))

        report, _ = self.run_tick(_calm)

        self.assertEqual(report.errors, ())
        self.assertEqual([item.task_id for item in report.dispatched], ["ready"])
        self.assertEqual(len(self.router_calls), 1)
        self.assertNotIn("host_pressure", [b.get("kind") for b in report.blockers])
        self.assertIsNone(report.to_dict()["skipped"])

    def test_no_reader_means_no_host_verdict(self) -> None:
        self.queue.add_task(task("ready"))

        report, _ = self.run_tick(None)

        self.assertEqual([item.task_id for item in report.dispatched], ["ready"])

    def test_pressured_tick_launches_nothing_but_still_reconciles(self) -> None:
        self.add_stale_inflight()
        self.queue.add_task(task("ready"))

        report, spy = self.run_tick(_pressured)

        self.assertEqual(self.router_calls, [])
        self.assertEqual(report.dispatched, ())
        self.assertEqual(report.previews, ())
        self.assertEqual(report.errors, ())
        self.assertEqual(report.plan.batches, ())
        host = [b for b in report.blockers if b.get("kind") == "host_pressure"]
        self.assertEqual(len(host), 1)
        self.assertAlmostEqual(host[0]["cpu_some_avg60"], 80.0)
        # Reconciliation still ran and repaired the abandoned attempt.
        spy.assert_called_once()
        self.assertEqual(report.reconciliation[0]["action"], "failed")
        self.assertEqual(self.queue.inflight(), [])
        self.assertEqual(
            {(item.task_id, item.state) for item in self.queue.attempts()},
            {("stale", "failed")},
        )
        payload = report.to_dict()
        self.assertIn("host_pressure", [b["kind"] for b in payload["blockers"]])
        self.assertEqual(payload["dispatched"], [])

    def test_disabled_gate_dispatches_under_pressure(self) -> None:
        from dataclasses import replace

        self.config = replace(
            self.config, host_load_gate=config_module.HostLoadGateConfig(enabled=False),
        )
        self.queue.add_task(task("ready"))

        report, _ = self.run_tick(_pressured)

        self.assertEqual([item.task_id for item in report.dispatched], ["ready"])
        self.assertNotIn("host_pressure", [b.get("kind") for b in report.blockers])

    def test_plan_tick_closes_every_gate_under_pressure(self) -> None:
        self.queue.add_task(task("ready"))
        with mock.patch.object(scout, "read_all", return_value=snapshots()):
            calm = scout.plan_tick(
                self.config, self.queue, now_epoch=NOW, host_load_reader=_calm,
            )
            pressured = scout.plan_tick(
                self.config, self.queue, now_epoch=NOW, host_load_reader=_pressured,
            )

        self.assertIsNone(calm.host_pressure)
        self.assertTrue(calm.plan.batches)
        self.assertTrue(any(gate.open for gate in calm.plan.gates))

        self.assertIsNotNone(pressured.host_pressure)
        self.assertEqual(pressured.host_pressure["kind"], "host_pressure")
        self.assertEqual(pressured.plan.batches, ())
        self.assertFalse(pressured.allocations)
        self.assertTrue(pressured.plan.gates)
        for gate in pressured.plan.gates:
            with self.subTest(gate=(gate.provider_id, gate.account_id)):
                self.assertFalse(gate.open)
                self.assertTrue(str(gate.reason).startswith("host pressure"))
                self.assertEqual(gate.batch_size, 0)


class TickLockTests(ScoutTickCase):
    @contextmanager
    def held_lock(self) -> Iterator[Path]:
        lock_path = self.queue.path.parent / "scout.lock"
        self.assertEqual(scout.TICK_LOCK_NAME, "scout.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield lock_path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_concurrent_tick_is_skipped_without_dispatching(self) -> None:
        self.add_stale_inflight()
        self.queue.add_task(task("ready"))

        with self.held_lock():
            report, spy = self.run_tick(_calm)

        self.assertIsNotNone(report.skipped)
        self.assertEqual(report.skipped["reason"], "tick_in_progress")
        self.assertEqual(report.errors, ())
        self.assertEqual(report.dispatched, ())
        self.assertEqual(self.router_calls, [])
        spy.assert_not_called()
        payload = report.to_dict()
        self.assertEqual(payload["skipped"]["reason"], "tick_in_progress")
        self.assertEqual(payload["errors"], [])
        # Nothing was reconciled or launched while another tick owned the lock.
        self.assertEqual(
            {(item.task_id, item.state) for item in self.queue.attempts()},
            {("stale", "dispatched")},
        )

    def test_lock_is_released_after_a_normal_tick(self) -> None:
        self.queue.add_task(task("ready"))

        with self.held_lock():
            skipped, _ = self.run_tick(_calm)
        self.assertEqual(skipped.skipped["reason"], "tick_in_progress")

        first, _ = self.run_tick(_calm)
        self.assertIsNone(first.skipped)
        self.assertEqual([item.task_id for item in first.dispatched], ["ready"])

        # The previous tick released the lock: a fresh holder can take it immediately.
        fd = os.open(self.queue.path.parent / "scout.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

        second, _ = self.run_tick(_calm)
        self.assertIsNone(second.skipped)
        self.assertIsNone(second.to_dict()["skipped"])

    def test_cli_scout_exits_zero_when_skipped(self) -> None:
        config_path = self.root / "config.json"
        raw = _raw_config(self.root)
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        state = self.root / "state"
        self.assertEqual(Path(raw["database"]).parent, state)
        lock_path = state / "scout.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with _capture_cli_json() as payloads:
                code = cli.main(["scout", "--config", str(config_path), "--json"])
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        self.assertEqual(code, 0)
        self.assertEqual(payloads[-1]["skipped"]["reason"], "tick_in_progress")
        self.assertEqual(payloads[-1]["errors"], [])


class SlotFreedMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "state" / "queue.db")
        self.queue.initialize()
        self.marker = self.queue.path.parent / "slot-freed"

    def claim(self, task_id: str):
        self.queue.add_task(task(task_id))
        attempt = self.queue.claim(
            task_id, ELIGIBILITY_KEY, "alpha", "alpha-account", now_epoch=NOW,
        )
        self.assertIsNotNone(attempt)
        return attempt

    def dispatch(self, task_id: str):
        attempt = self.claim(task_id)
        self.queue.record(
            task_id, ELIGIBILITY_KEY, attempt_id=attempt.id, status="dispatched",
            cycle=RESET, provider_id="alpha", account_id="alpha-account",
            router_job_id=f"job-{task_id}",
        )
        return attempt

    def terminal(self, task_id: str, attempt, status: str, outcome: dict[str, object]):
        return self.queue.record(
            task_id, ELIGIBILITY_KEY, attempt_id=attempt.id, status=status,
            outcome=outcome, provider_id="alpha", account_id="alpha-account",
            summary=f"{task_id} {status}",
        )

    def test_marker_location_is_exposed(self) -> None:
        self.assertEqual(db.SLOT_FREED_MARKER, "slot-freed")
        self.assertEqual(self.queue.slot_freed_marker, self.marker)

    def test_dispatched_record_does_not_touch_marker(self) -> None:
        self.dispatch("running")
        self.assertFalse(self.marker.exists())

    def test_terminal_records_touch_marker(self) -> None:
        outcomes = {
            "done": verified_outcome(),
            "failed": {"reason": {
                "code": "retryable", "detail": "fixture failure",
                "signature": "retryable:fixture",
            }},
            "awaiting_human": {"reason": {
                "code": "authority_required", "detail": "Approve the fixture diff",
                "signature": "authority_required:fixture",
            }},
        }
        for status, outcome in outcomes.items():
            with self.subTest(status=status):
                task_id = f"job-{status}"
                attempt = self.dispatch(task_id)
                self.marker.unlink(missing_ok=True)
                self.assertFalse(self.marker.exists())

                self.terminal(task_id, attempt, status, outcome)

                self.assertTrue(self.marker.is_file())
                self.assertEqual(self.queue.attempts(task_id=task_id)[0].state, status)

    def test_terminal_record_updates_an_existing_marker(self) -> None:
        attempt = self.dispatch("finisher")
        self.marker.write_text("stale-sentinel", encoding="utf-8")
        os.utime(self.marker, (1_000_000, 1_000_000))

        self.terminal("finisher", attempt, "done", verified_outcome())

        self.assertNotEqual(self.marker.read_text(encoding="utf-8"), "stale-sentinel")
        self.assertGreater(self.marker.stat().st_mtime, 1_000_000)

    def test_abort_unlaunched_attempt_touches_marker(self) -> None:
        attempt = self.claim("never-launched")
        self.assertFalse(self.marker.exists())

        self.queue.abort_unlaunched_attempt(
            "never-launched", ELIGIBILITY_KEY, attempt.id, "router refused",
        )

        self.assertTrue(self.marker.is_file())
        self.assertEqual(self.queue.attempts(task_id="never-launched")[0].state, "aborted")

    def test_rejected_terminal_does_not_touch_marker(self) -> None:
        attempt = self.dispatch("rejected")
        with self.assertRaises(db.QueueError):
            self.terminal("rejected", attempt, "awaiting_human", {"reason": {
                "code": "authority_required", "detail": "   ",
                "signature": "authority_required:blank",
            }})
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.queue.attempts(task_id="rejected")[0].state, "dispatched")

    def attempt_state(self, attempt_id: str) -> str:
        independent = sqlite3.connect(self.queue.path)
        try:
            return independent.execute(
                "SELECT state FROM task_attempts WHERE id = ?", (attempt_id,),
            ).fetchone()[0]
        finally:
            independent.close()

    def test_rolled_back_terminal_write_does_not_touch_marker(self) -> None:
        class Sentinel(Exception):
            pass

        attempt = self.dispatch("rolled-back")
        self.assertFalse(self.marker.exists())

        with self.assertRaises(Sentinel):
            with self.queue._transaction() as connection:
                connection.execute(
                    "UPDATE task_attempts SET state = 'failed', terminal_at = ? WHERE id = ?",
                    ("2026-01-01T00:00:00Z", attempt.id),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT state FROM task_attempts WHERE id = ?", (attempt.id,),
                    ).fetchone()[0],
                    "failed",
                )
                raise Sentinel()

        self.assertFalse(self.marker.exists())
        self.assertEqual(self.attempt_state(attempt.id), "dispatched")

    def test_marker_is_written_only_after_commit_is_visible(self) -> None:
        attempt = self.dispatch("commit-order")
        self.assertFalse(self.marker.exists())
        observed: list[str] = []

        def observe(_queue) -> None:
            observed.append(self.attempt_state(attempt.id))

        with mock.patch.object(
            db.QueueDB, "_touch_slot_freed", autospec=True, side_effect=observe,
        ) as touch:
            self.terminal("commit-order", attempt, "done", verified_outcome())

        self.assertEqual(touch.call_count, 1)
        self.assertEqual(observed, ["done"])

    def test_unwritable_marker_does_not_fail_terminal_record(self) -> None:
        attempt = self.dispatch("finisher")
        self.marker.mkdir()

        event = self.terminal("finisher", attempt, "done", verified_outcome())

        self.assertEqual(event.status, "done")
        self.assertTrue(self.marker.is_dir())
        self.assertEqual(self.queue.attempts(task_id="finisher")[0].state, "done")
        self.assertEqual(self.queue.claims(), [])
        self.assertEqual(
            [run.status for run in self.queue.runs(task_id="finisher")][0], "done",
        )


class ScoutPathUnitPackagingTests(unittest.TestCase):
    SYSTEMD = SKILL_ROOT / "systemd"

    def unit_lines(self, name: str) -> list[str]:
        path = self.SYSTEMD / name
        self.assertTrue(path.is_file(), f"missing unit: {path}")
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def values(lines: list[str], key: str) -> list[str]:
        prefix = f"{key}="
        return [line[len(prefix):] for line in lines if line.startswith(prefix)]

    def test_path_unit_watches_slot_freed_marker(self) -> None:
        lines = self.unit_lines("bonus-drain-scout.path")
        watched = self.values(lines, "PathChanged")
        self.assertEqual(len(watched), 1)
        self.assertTrue(watched[0].endswith("/slot-freed"), watched[0])
        self.assertEqual(self.values(lines, "Unit"), ["bonus-drain-scout.service"])
        self.assertEqual(len(self.values(lines, "TriggerLimitIntervalSec")), 1)
        self.assertTrue(self.values(lines, "TriggerLimitIntervalSec")[0])
        self.assertEqual(len(self.values(lines, "TriggerLimitBurst")), 1)
        self.assertTrue(self.values(lines, "TriggerLimitBurst")[0])
        self.assertIn("paths.target", self.values(lines, "WantedBy"))

    def test_path_unit_is_shipped_and_owned(self) -> None:
        manifest = json.loads(
            (SKILL_ROOT / "distribution-manifest.json").read_text(encoding="utf-8"),
        )
        self.assertIn("systemd/bonus-drain-scout.path", manifest["include"])
        self.assertIn("bonus-drain-scout.path", lifecycle.UNIT_NAMES)

    def test_scout_timer_runs_every_ten_minutes(self) -> None:
        lines = self.unit_lines("bonus-drain-scout.timer")
        self.assertEqual(self.values(lines, "OnUnitActiveSec"), ["10min"])
        text = (self.SYSTEMD / "bonus-drain-scout.timer").read_text(encoding="utf-8")
        self.assertNotIn("hourly", text.lower())

    def test_scout_service_still_orders_after_refresh(self) -> None:
        lines = self.unit_lines("bonus-drain-scout.service")
        after = " ".join(self.values(lines, "After")).split()
        self.assertIn("bonus-drain-refresh.service", after)


if __name__ == "__main__":
    unittest.main()
