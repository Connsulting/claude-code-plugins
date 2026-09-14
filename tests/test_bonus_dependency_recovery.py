"""Behavioral contracts for retained-history dependency recovery."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
CLI = SKILL_ROOT / "bin" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import cli, db, dispatcher, goals, scout, usage
from bonus_drain import config as config_module

from tests.test_bonus_drain_scout_inflight import _open_snapshots, _two_provider_config

NOW = 2_000_000_000
KEY = "alpha-account/alpha-weekly/2000001000"


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def task(task_id: str, cwd: Path | str, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": task_id,
        "title": task_id,
        "kind": "oneoff",
        "priority": 2,
        "cwd": str(cwd),
        "goal": f"complete {task_id}",
        "done_when": f"proof for {task_id} is retained",
        "created_at": iso(NOW - 60),
        "active": True,
        "size": "small",
    }
    value.update(changes)
    return value


def reason(code: str = "retryable", signature: str = "retryable:fixture") -> dict[str, object]:
    return {
        "reason": {
            "code": code,
            "detail": f"fixture {code} outcome",
            "signature": signature,
        }
    }


def verified(repository: dict[str, object] | None = None, evidence: str = "fixture://proof") -> dict[str, object]:
    value: dict[str, object] = {
        "reason": {
            "code": "done_when_verified",
            "detail": "the done-when was checked",
            "signature": "done_when_verified:fixture",
        },
        "completion": {
            "verified": True,
            "mechanism": "command",
            "evidence": [evidence],
        },
    }
    if repository is not None:
        value["repository"] = repository
    return value


def prompt_json_tokens(prompt: str) -> set[str]:
    """Collect keys and string values from every valid JSON object in a prompt."""

    tokens: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                tokens.add(str(key))
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            tokens.add(value)

    decoder = json.JSONDecoder()
    for index, character in enumerate(prompt):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(prompt[index:])
        except json.JSONDecodeError:
            continue
        collect(value)
    return tokens


def as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    method = getattr(value, "to_dict", None)
    if callable(method):
        return method()
    return vars(value)


def rows(queue: db.QueueDB, sql: str, parameters: tuple[object, ...] = ()) -> list[dict[str, object]]:
    with sqlite3.connect(queue.path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]


def runtime(
    database: Path,
    router: Path | str = "/bin/true",
    *,
    source_path: Path | None = None,
    activation_scope: str = "run",
    activation: bool = False,
) -> config_module.RuntimeConfig:
    adapters = [config_module.AdapterConfig("router", "agent-router", (str(router),))]
    activation_id = None
    if activation:
        activation_id = "activation"
        adapters.append(config_module.AdapterConfig("activation", "activation", ("/bin/true",)))
    return config_module.RuntimeConfig(
        schema_version=1,
        source_path=source_path,
        database=database,
        record_command=(str(CLI), "record"),
        secret_refs=(),
        adapters=tuple(adapters),
        providers=(
            config_module.ProviderConfig(
                "alpha", config_module.DispatchBinding("router", "alpha"), frozenset(), "single",
            ),
        ),
        plans=(config_module.PlanConfig("alpha-plan", "alpha"),),
        accounts=(
            config_module.AccountConfig(
                "alpha-account", "alpha", "alpha-plan",
                activation_adapter_id=activation_id,
                activation_scope=activation_scope,
            ),
        ),
        limits=(
            config_module.LimitConfig(
                "alpha-weekly", "alpha-plan", 604_800, 95, 20_000, 6,
            ),
        ),
        viewer={},
        pr_exceptions=(),
        usage_max_age_seconds=3_600,
        cache_dir=database.parent / "cache",
    )


@contextlib.contextmanager
def captured_json():
    payloads: list[object] = []
    with mock.patch.object(cli, "_json", side_effect=lambda value, **_kwargs: payloads.append(value)):
        yield payloads


class RecoveryCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "queue.db")
        self.queue.initialize()

    def add(self, task_id: str, **changes: object):
        return self.queue.add_task(task(task_id, self.root, **changes))

    def claim(
        self,
        task_id: str,
        *,
        key: str | None = None,
        now: int = NOW,
        automatic: bool = False,
    ):
        attempt = self.queue.claim(
            task_id,
            key or KEY,
            "alpha",
            "alpha-account",
            automatic=automatic,
            now_epoch=now,
        )
        self.assertIsNotNone(attempt)
        return attempt

    def terminal(
        self,
        task_id: str,
        attempt: object,
        status: str,
        outcome: dict[str, object],
        *,
        key: str | None = None,
        now: int = NOW,
    ):
        return self.queue.record(
            task_id,
            key or KEY,
            attempt_id=attempt.id,
            status=status,
            outcome=outcome,
            provider_id="alpha",
            account_id="alpha-account",
            timestamp=iso(now),
            now_epoch=now,
            summary=f"{task_id} {status}",
        )

    def fail_task(self, task_id: str, *, signature: str = "retryable:fixture", now: int = NOW):
        attempt = self.claim(task_id, now=now)
        self.terminal(task_id, attempt, "failed", reason(signature=signature), now=now)
        return attempt


class AttemptAndLegacyContracts(RecoveryCase):
    def test_cli_kind_cannot_turn_a_new_oneoff_null_done_into_recurring_history(self) -> None:
        self.add("kind-parent")
        self.add("kind-child", depends_on=["kind-parent"])

        with captured_json() as payloads:
            code = cli.main([
                "record", "--database", str(self.queue.path), "--task", "kind-parent",
                "--kind", "recurring", "--cycle", "0", "--status", "done", "--json",
            ])

        self.assertEqual(code, 2)
        self.assertIn("kind", str(payloads[0]).lower())
        self.assertEqual(self.queue.runs(task_id="kind-parent"), [])
        self.assertNotEqual(self.queue.readiness("kind-parent", now_epoch=NOW)["state"], "done")
        self.assertFalse(self.queue.readiness("kind-child", now_epoch=NOW)["ready"])

        # A row retained from the pre-fix injection path is still not verified
        # completion for the canonical one-off task.
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute(
                "INSERT INTO runs(task,kind,cycle,eligibility_key,status,ts) "
                "VALUES('kind-parent','recurring',0,NULL,'done',?)",
                (iso(NOW),),
            )
        self.assertNotEqual(self.queue.readiness("kind-parent", now_epoch=NOW)["state"], "done")
        self.assertFalse(self.queue.readiness("kind-child", now_epoch=NOW)["ready"])

    def test_public_release_claim_refuses_a_dispatched_attempt_and_preserves_ownership(self) -> None:
        self.add("dispatched-owner")
        attempt = self.claim("dispatched-owner")
        self.queue.record(
            "dispatched-owner", KEY, attempt_id=attempt.id, status="dispatched",
            provider_id="alpha", account_id="alpha-account", router_job_id="job-owner",
            timestamp=iso(NOW), now_epoch=NOW,
        )

        with self.assertRaisesRegex(db.QueueError, "dispatch|launch|abort"):
            self.queue.release_claim("dispatched-owner", KEY, reason="unsafe public release")

        claim = self.queue.claim_for("dispatched-owner", KEY)
        self.assertEqual(claim.attempt_id, attempt.id)
        self.assertEqual(self.queue.attempts(task_id="dispatched-owner")[0].state, "dispatched")
        self.assertEqual(self.queue.inflight()[0].attempt_id, attempt.id)
        self.assertEqual(
            [event.status for event in self.queue.runs(task_id="dispatched-owner")],
            ["dispatched"],
        )

    def test_exact_worker_terminal_resolves_its_ambiguous_attempt_and_lease(self) -> None:
        self.add("ambiguous-worker")
        attempt = self.claim("ambiguous-worker")
        self.queue.acquire_activation(
            "ambiguous-worker", KEY, "alpha", "alpha-account", lambda: None,
            attempt_id=attempt.id,
        )
        self.queue.record(
            "ambiguous-worker", KEY, attempt_id=attempt.id, status="dispatched",
            provider_id="alpha", account_id="alpha-account", router_job_id="job-unknown",
            timestamp=iso(NOW), now_epoch=NOW,
        )
        self.queue.mark_attempt_ambiguous(
            "ambiguous-worker", KEY, attempt.id, "router response was lost",
        )
        release = mock.Mock()

        completed = self.queue.record(
            "ambiguous-worker", KEY, attempt_id=attempt.id, status="done",
            outcome=verified(), provider_id="alpha", account_id="alpha-account",
            release_activation=release, timestamp=iso(NOW + 1), now_epoch=NOW + 1,
        )

        self.assertEqual(completed.attempt_id, attempt.id)
        release.assert_called_once_with()
        self.assertEqual(self.queue.claims(), [])
        self.assertEqual(self.queue.activation_leases(), [])
        self.assertEqual(self.queue.inflight(), [])
        self.assertEqual(self.queue.attempts(task_id="ambiguous-worker")[0].state, "done")

    def test_late_terminal_replay_cannot_release_or_close_the_successor_attempt(self) -> None:
        self.add("parent")
        first = self.fail_task("parent", signature="retryable:first")
        decision = self.queue.request_recovery(
            "parent", expected_attempt_id=first.id, mode="retry",
            source="operator", now_epoch=NOW,
        )
        self.assertEqual(as_dict(decision)["state"], "scheduled")
        second = self.claim("parent", key=KEY, now=NOW)
        self.queue.acquire_activation(
            "parent", KEY, "alpha", "alpha-account", lambda: None,
        )
        self.queue.record(
            "parent", KEY, attempt_id=second.id, status="dispatched",
            provider_id="alpha", account_id="alpha-account", router_job_id="job-2",
            timestamp=iso(NOW),
        )

        replay = self.queue.record(
            "parent", KEY, attempt_id=first.id, status="failed",
            outcome=reason(signature="retryable:first"), provider_id="alpha",
            timestamp=iso(NOW), summary="parent failed",
        )

        self.assertEqual(replay.attempt_id, first.id)
        self.assertEqual(self.queue.claim_for("parent", KEY).attempt_id, second.id)
        self.assertEqual(self.queue.activation_leases()[0].attempt_id, second.id)
        self.assertEqual(self.queue.inflight()[0].attempt_id, second.id)
        with self.assertRaisesRegex(db.QueueError, "conflict|already recorded"):
            self.queue.record(
                "parent", KEY, attempt_id=first.id, status="skipped",
                outcome=reason("verification_needed", "different"),
            )
        self.assertEqual(len(rows(self.queue, "SELECT * FROM runs WHERE task='parent'")), 2)

    def test_every_new_claim_requires_its_attempt_but_narrow_null_compatibility_remains(self) -> None:
        self.add("oneoff")
        attempt = self.claim("oneoff")
        with self.assertRaisesRegex(db.QueueError, "attempt"):
            self.queue.record("oneoff", KEY, status="failed", outcome=reason())
        self.assertEqual(self.queue.claim_for("oneoff").attempt_id, attempt.id)

        self.add("weekly", kind="recurring", cadence="weekly")
        recurring = self.claim("weekly", key="alpha-account/alpha-weekly/2000002000")
        with self.assertRaisesRegex(db.QueueError, "attempt"):
            self.queue.record(
                "weekly", "alpha-account/alpha-weekly/2000002000",
                status="skipped", outcome=reason("verification_needed", "weekly:skip"),
            )
        self.assertEqual(self.queue.claim_for("weekly").attempt_id, recurring.id)

        self.add("legacy-recurring", kind="recurring", cadence="weekly")
        event = self.queue.record(
            "legacy-recurring", None, cycle=12, status="done",
            summary="supported unclaimed recurring row",
        )
        self.assertIsNone(event.attempt_id)

        self.add("legacy-failed")
        failed = self.queue.record(
            "legacy-failed", None, cycle=0, status="failed", summary="old worker",
        )
        self.add("legacy-skipped")
        skipped = self.queue.record(
            "legacy-skipped", None, cycle=0, status="skipped", summary="old worker",
        )
        self.assertIsNone(failed.attempt_id)
        self.assertIsNone(skipped.attempt_id)
        self.add("new-null-done")
        with self.assertRaisesRegex(db.QueueError, "attempt|verified"):
            self.queue.record("new-null-done", None, cycle=0, status="done")

    def test_only_verified_completion_satisfies_children_and_pr_presence_is_only_evidence(self) -> None:
        self.add("parent")
        self.add("child", depends_on=["parent"])
        attempt = self.claim("parent")
        with self.assertRaisesRegex(db.QueueError, "verified|completion"):
            self.queue.record(
                "parent", KEY, attempt_id=attempt.id, status="done",
                outcome={"repository": {"pull_request": {"url": "https://example.test/1", "state": "merged"}}},
            )
        self.assertEqual(self.queue.claim_for("parent").attempt_id, attempt.id)
        self.assertFalse(self.queue.readiness("child", now_epoch=NOW)["ready"])
        self.terminal("parent", attempt, "done", verified())
        self.assertTrue(self.queue.readiness("child", now_epoch=NOW)["ready"])

    def test_legacy_done_and_failed_rows_migrate_additively_without_invented_attempts(self) -> None:
        legacy = self.root / "legacy.db"
        with sqlite3.connect(legacy) as connection:
            connection.executescript(
                """
                CREATE TABLE tasks (
                  id TEXT PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL,priority INTEGER NOT NULL,
                  cadence TEXT,cwd TEXT NOT NULL,goal TEXT NOT NULL,context TEXT,constraints TEXT,
                  precondition TEXT,done_when TEXT,created_at TEXT NOT NULL,active INTEGER NOT NULL,
                  claude_only INTEGER NOT NULL DEFAULT 0,model TEXT,mcp TEXT,
                  use_implement INTEGER NOT NULL DEFAULT 0,allowed_providers_json TEXT,
                  required_capabilities_json TEXT,size TEXT,source_ref TEXT,work_group TEXT,
                  depends_on_json TEXT
                );
                CREATE TABLE runs (
                  rowid_pk INTEGER PRIMARY KEY AUTOINCREMENT,task TEXT NOT NULL,kind TEXT NOT NULL,
                  cycle INTEGER NOT NULL,eligibility_key TEXT,status TEXT NOT NULL,ts TEXT NOT NULL,
                  branch TEXT,summary TEXT,engine TEXT,provider_id TEXT,account_id TEXT,
                  router_job_id TEXT,trigger TEXT
                );
                """
            )
            parents = (("done", "done"), ("failed", "failed"), ("skipped", "skipped"))
            for task_id, status in parents:
                connection.execute(
                    "INSERT INTO tasks(id,title,kind,priority,cwd,goal,created_at,active,depends_on_json) "
                    "VALUES(?,?, 'oneoff',2,'/tmp','proof',?,1,'[]')",
                    (task_id, task_id, iso(NOW - 60)),
                )
                connection.execute(
                    "INSERT INTO runs(task,kind,cycle,eligibility_key,status,ts) "
                    "VALUES(?, 'oneoff',0,NULL,?,?)",
                    (task_id, status, iso(NOW)),
                )
            for index, parent in enumerate(("failed", "failed", "skipped", "skipped")):
                child = f"child-{index}"
                connection.execute(
                    "INSERT INTO tasks(id,title,kind,priority,cwd,goal,created_at,active,depends_on_json) "
                    "VALUES(?,?, 'oneoff',2,'/tmp','proof',?,1,?)",
                    (child, child, iso(NOW - 30), json.dumps([parent])),
                )
            connection.execute(
                "INSERT INTO tasks(id,title,kind,priority,cwd,goal,created_at,active,depends_on_json) "
                "VALUES('done-child','done-child','oneoff',2,'/tmp','proof',?,1,'[\"done\"]')",
                (iso(NOW - 30),),
            )

        migrated = db.QueueDB(legacy)
        migrated.initialize()
        historical = rows(migrated, "SELECT task,status,attempt_id FROM runs ORDER BY rowid_pk")
        self.assertEqual([(row["task"], row["status"]) for row in historical], list(parents))
        self.assertTrue(all(row["attempt_id"] is None for row in historical))
        self.assertTrue(migrated.readiness("done-child", now_epoch=NOW)["ready"])
        decisions = migrated.reconcile_recoveries(now_epoch=NOW, dry_run=False)
        self.assertEqual({as_dict(item)["task_id"] for item in decisions}, {"failed", "skipped"})
        self.assertTrue(all(as_dict(item)["mode"] == "verification" for item in decisions))
        self.assertTrue(all(not migrated.readiness(f"child-{i}", now_epoch=NOW)["ready"] for i in range(4)))
        self.assertEqual(len(rows(migrated, "SELECT * FROM runs")), 3)


class AutomaticRecoveryContracts(RecoveryCase):
    def test_readiness_skips_descendant_graph_when_task_has_no_recovery(self) -> None:
        self.add("ordinary")

        with mock.patch.object(
            self.queue, "_blocked_descendant_counts",
            side_effect=AssertionError("descendant graph should not be rebuilt"),
        ):
            status = self.queue.readiness("ordinary", now_epoch=NOW)

        self.assertEqual(status["state"], "ready")
        self.assertIsNone(status["recovery"])

    def test_readiness_keeps_exact_descendant_count_when_recovery_exists(self) -> None:
        self.add("parent")
        self.add("child", depends_on=["parent"])
        self.add("grandchild", depends_on=["child"])
        self.fail_task("parent", signature="retryable:counted")
        self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)

        with mock.patch.object(
            self.queue, "_blocked_descendant_counts",
            wraps=self.queue._blocked_descendant_counts,
        ) as counts:
            status = self.queue.readiness("parent", now_epoch=NOW)

        counts.assert_called_once()
        self.assertEqual(status["recovery"]["blocked_descendants"], 2)

    def test_cli_queue_reuses_snapshot_readiness_once_per_task(self) -> None:
        self.add("ready")
        self.add("waiting", depends_on=["ready"])
        cfg = runtime(self.queue.path)

        with (
            mock.patch.object(cli, "_queue", return_value=(cfg, self.queue)),
            mock.patch.object(
                self.queue, "readiness", wraps=self.queue.readiness,
            ) as readiness,
            captured_json() as payloads,
        ):
            code = cli.main(["queue", "0", "--json", "--now", str(NOW)])

        self.assertEqual(code, 0)
        self.assertEqual(readiness.call_count, 2)
        self.assertEqual(set(payloads[0]["readiness"]), {"ready", "waiting"})

    def test_failed_parent_with_only_a_historically_done_child_gets_no_automatic_recovery(self) -> None:
        self.add("historical-parent")
        self.add("historical-child", depends_on=["historical-parent"])
        self.fail_task("historical-parent", signature="retryable:historical-parent")
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute(
                "INSERT INTO runs(task,kind,cycle,eligibility_key,status,ts) "
                "VALUES('historical-child','oneoff',0,NULL,'done',?)",
                (iso(NOW),),
            )

        self.assertEqual(
            self.queue.readiness("historical-child", now_epoch=NOW)["state"], "done",
        )
        self.assertEqual(self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False), ())
        self.assertIsNone(self.queue.recovery_for("historical-parent"))

    def test_recovery_is_dependent_only_uses_fixed_backoff_and_two_immutable_attempts(self) -> None:
        self.add("unused")
        self.fail_task("unused", signature="retryable:unused")
        self.assertEqual(self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False), ())
        self.assertEqual(rows(self.queue, "SELECT * FROM task_recovery WHERE task_id='unused'"), [])

        self.add("unlocker")
        self.add("child", depends_on=["unlocker"])
        self.fail_task("unlocker", signature="retryable:zero", now=NOW)
        first_decision = self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)[0]
        self.assertEqual(as_dict(first_decision)["state"], "backoff")
        self.assertEqual(as_dict(first_decision)["not_before"], iso(NOW + 300))
        self.assertIsNone(self.queue.claim(
            "unlocker", KEY, "alpha", "alpha-account", automatic=True, now_epoch=NOW + 299,
        ))
        first = self.claim("unlocker", automatic=True, now=NOW + 300)
        self.assertEqual((first.ordinal, first.mode, first.origin), (2, "retry", "automatic"))
        projection = rows(self.queue, "SELECT * FROM task_recovery WHERE task_id='unlocker'")[0]
        self.assertEqual((projection["state"], projection["consumed_by_attempt_id"]), ("consumed", first.id))
        self.terminal(
            "unlocker", first, "failed", reason(signature="retryable:one"), now=NOW + 300,
        )
        second_decision = self.queue.reconcile_recoveries(now_epoch=NOW + 300, dry_run=False)[0]
        self.assertEqual(as_dict(second_decision)["not_before"], iso(NOW + 2_100))
        second = self.claim("unlocker", automatic=True, now=NOW + 2_100)
        self.assertEqual((second.ordinal, second.origin), (3, "automatic"))
        self.terminal(
            "unlocker", second, "failed", reason(signature="retryable:two"), now=NOW + 2_100,
        )
        exhausted = self.queue.reconcile_recoveries(now_epoch=NOW + 2_100, dry_run=False)[0]
        self.assertEqual(as_dict(exhausted)["state"], "exhausted")
        self.assertEqual(len(rows(
            self.queue,
            "SELECT * FROM task_attempts WHERE task_id='unlocker' AND origin='automatic' AND state!='aborted'",
        )), 2)
        reopened = db.QueueDB(self.queue.path)
        self.assertEqual(
            as_dict(reopened.reconcile_recoveries(now_epoch=NOW + 99_999, dry_run=False)[0])["state"],
            "exhausted",
        )
        self.assertIsNone(reopened.claim(
            "unlocker", KEY, "alpha", "alpha-account", automatic=True, now_epoch=NOW + 99_999,
        ))
        readiness = reopened.readiness("unlocker", now_epoch=NOW + 99_999)
        latest_run = reopened.runs(task_id="unlocker")[0].to_dict()
        self.assertEqual(readiness["recovery"]["state"], "exhausted")
        self.assertTrue(readiness["requeue"]["allowed"])
        self.assertEqual(latest_run["attempt_id"], second.id)
        self.assertTrue(latest_run["requeue"]["allowed"])
        operator = reopened.requeue(
            "unlocker", attempt_id=second.id, now_epoch=NOW + 99_999,
        )
        self.assertEqual((operator.origin, operator.state), ("operator", "scheduled"))

    def test_backdated_worker_timestamp_cannot_make_automatic_recovery_due_early(self) -> None:
        self.add("backdated")
        self.add("backdated-child", depends_on=["backdated"])
        attempt = self.claim("backdated")
        cfg = runtime(self.queue.path)
        outcome_path = dispatcher.materialize_outcome_file(cfg, attempt.id)
        outcome_path.write_text(json.dumps(reason(signature="retryable:backdated")), encoding="utf-8")

        with mock.patch.object(db.time, "time", return_value=NOW), captured_json() as payloads:
            code = cli.main([
                "record", "--database", str(self.queue.path), "--task", "backdated",
                "--eligibility-key", KEY, "--attempt-id", attempt.id,
                "--kind", "oneoff", "--cycle", "0", "--status", "failed",
                "--ts", iso(NOW - 86_400), "--outcome-file", str(outcome_path), "--json",
            ])

        self.assertEqual(code, 0, payloads)
        stored_attempt = rows(
            self.queue, "SELECT terminal_at FROM task_attempts WHERE id=?", (attempt.id,),
        )[0]
        self.assertEqual(stored_attempt["terminal_at"], iso(NOW))
        run = rows(self.queue, "SELECT ts,received_at FROM runs WHERE attempt_id=?", (attempt.id,))[0]
        self.assertEqual(run["ts"], iso(NOW - 86_400))
        self.assertEqual(run["received_at"], iso(NOW))

        recovery = self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)[0]
        self.assertEqual(as_dict(recovery)["not_before"], iso(NOW + 300))
        self.assertFalse(self.queue.readiness("backdated", now_epoch=NOW + 299)["ready"])
        self.assertTrue(self.queue.readiness("backdated", now_epoch=NOW + 300)["ready"])

    def test_scout_preserves_operator_recovery_after_budget_exhaustion_and_edit(self) -> None:
        self.add("operator-owned")
        self.add("operator-child", depends_on=["operator-owned"])
        self.fail_task("operator-owned", signature="retryable:initial", now=NOW)
        self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)
        first = self.claim("operator-owned", automatic=True, now=NOW + 300)
        self.terminal(
            "operator-owned", first, "failed",
            reason(signature="retryable:first-automatic"), now=NOW + 300,
        )
        self.queue.reconcile_recoveries(now_epoch=NOW + 300, dry_run=False)
        second = self.claim("operator-owned", automatic=True, now=NOW + 2_100)
        self.terminal(
            "operator-owned", second, "failed",
            reason(signature="retryable:second-automatic"), now=NOW + 2_100,
        )
        exhausted = self.queue.reconcile_recoveries(now_epoch=NOW + 2_100, dry_run=False)[0]
        self.assertEqual(as_dict(exhausted)["state"], "exhausted")

        self.queue.requeue("operator-owned", attempt_id=second.id, now_epoch=NOW + 2_101)
        self.queue.edit_task("operator-owned", {"goal": "operator-approved corrected contract"})
        before = as_dict(self.queue.recovery_for("operator-owned"))
        self.assertEqual((before["origin"], before["state"]), ("operator", "scheduled"))
        snapshots = {
            ("alpha", "alpha-account"): usage.UsageSnapshot(
                "alpha", "alpha-account", NOW + 2_101,
                {"alpha-weekly": {"used_percent": 95, "resets_at": NOW + 10_000}},
            ),
        }
        with (
            mock.patch.object(scout, "read_all", return_value=snapshots),
            mock.patch.object(scout, "dispatch") as dispatch_mock,
        ):
            scout.run_once(runtime(self.queue.path), self.queue, now_epoch=NOW + 2_101)

        dispatch_mock.assert_not_called()
        after = as_dict(self.queue.recovery_for("operator-owned"))
        self.assertEqual(
            (after["origin"], after["state"], after["after_attempt_id"],
             after["consumed_by_attempt_id"], after["contract_hash"]),
            ("operator", "scheduled", second.id, None, before["contract_hash"]),
        )

    def test_terminal_operator_recovery_without_descendants_exposes_the_next_action(self) -> None:
        self.add("standalone-retry")
        source = self.fail_task(
            "standalone-retry", signature="retryable:initial-action", now=NOW,
        )
        self.queue.requeue("standalone-retry", attempt_id=source.id, now_epoch=NOW)
        retry = self.claim("standalone-retry", now=NOW)
        self.terminal(
            "standalone-retry", retry, "failed",
            reason(signature="retryable:changed-action"), now=NOW + 1,
        )

        readiness = self.queue.readiness("standalone-retry", now_epoch=NOW + 1)
        self.assertEqual(readiness["state"], "failed")
        self.assertNotEqual((readiness.get("recovery") or {}).get("state"), "consumed")
        self.assertTrue(readiness["requeue"]["allowed"])
        next_action = self.queue.requeue(
            "standalone-retry", attempt_id=retry.id, now_epoch=NOW + 1,
        )
        self.assertEqual((next_action.origin, next_action.state), ("operator", "scheduled"))

    def test_real_snapshot_readiness_exposes_the_latest_attempt_to_the_viewer(self) -> None:
        self.add("snapshot-attempt")
        self.fail_task("snapshot-attempt", signature="retryable:snapshot", now=NOW)
        direct = self.queue.readiness("snapshot-attempt", now_epoch=NOW)
        from_snapshot = self.queue.snapshot(cycle=0, now_epoch=NOW)["readiness"]["snapshot-attempt"]

        for status in (direct, from_snapshot):
            attempt = status["attempt"]
            self.assertEqual(
                (attempt["ordinal"], attempt["mode"], attempt["origin"], attempt["state"]),
                (1, "normal", "normal", "failed"),
            )
        self.assertEqual(from_snapshot["attempt"], direct["attempt"])

    def test_same_normalized_reason_holds_immediately_without_spending_the_remaining_budget(self) -> None:
        self.add("unlocker")
        self.add("child", depends_on=["unlocker"])
        self.fail_task("unlocker", signature="network:stable")
        self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)
        retry = self.claim("unlocker", automatic=True, now=NOW + 300)
        self.terminal(
            "unlocker", retry, "failed", reason(signature="network:stable"), now=NOW + 300,
        )

        held = self.queue.reconcile_recoveries(now_epoch=NOW + 300, dry_run=False)[0]
        self.assertEqual((as_dict(held)["state"], as_dict(held)["reason_code"]), ("held", "no_progress"))
        self.assertEqual(len(rows(
            self.queue,
            "SELECT * FROM task_attempts WHERE task_id='unlocker' AND origin='automatic'",
        )), 1)

    def test_one_injected_clock_controls_dry_run_backoff_readiness_and_claim(self) -> None:
        self.add("unlocker")
        self.add("child", depends_on=["unlocker"])
        self.fail_task("unlocker", now=NOW)
        before = self.queue.snapshot(cycle=0, now_epoch=NOW)
        with mock.patch.object(db.time, "time", side_effect=AssertionError("wall clock used")):
            preview = self.queue.reconcile_recoveries(now_epoch=NOW + 299, dry_run=True)
            self.assertEqual(as_dict(preview[0])["state"], "would_schedule")
            self.assertFalse(self.queue.readiness("unlocker", now_epoch=NOW + 299)["ready"])
            self.assertEqual(self.queue.eligible_tasks(0, task_id="unlocker", now_epoch=NOW + 299), [])
            self.assertIsNone(self.queue.claim(
                "unlocker", KEY, "alpha", "alpha-account",
                automatic=True, now_epoch=NOW + 299,
            ))
        self.assertEqual(self.queue.snapshot(cycle=0, now_epoch=NOW), before)

    def test_manual_run_now_cannot_bypass_recovery_backoff(self) -> None:
        self.add("unlocker")
        self.add("child", depends_on=["unlocker"])
        self.fail_task("unlocker", now=NOW)
        self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)
        router = mock.Mock()
        with self.assertRaises(dispatcher.AlreadyClaimed):
            dispatcher.dispatch(
                runtime(self.queue.path), self.queue, task_id="unlocker",
                eligibility_key="manual/too-early", requested_provider="alpha",
                router_call=router,
            )
        router.assert_not_called()

    def test_recovery_unlocker_tiebreak_preserves_priority_and_legacy_exclusive_first(self) -> None:
        self.add("p0", priority=0)
        self.add("wide", priority=1)
        self.add("narrow", priority=1)
        self.add("ordinary", priority=1)
        self.add("exclusive", priority=1, claude_only=True)
        for index in range(3):
            self.add(f"wide-child-{index}", depends_on=["wide"])
        self.add("narrow-child", depends_on=["narrow"])
        for task_id in ("wide", "narrow"):
            failed = self.fail_task(task_id, signature=f"retryable:{task_id}")
            self.queue.request_recovery(
                task_id, expected_attempt_id=failed.id, mode="retry",
                source="operator", now_epoch=NOW,
            )
        normal = self.queue.eligible_tasks(0, now_epoch=NOW)
        self.assertLess(normal.index(self.queue.task("p0")), normal.index(self.queue.task("wide")))
        self.assertLess(normal.index(self.queue.task("wide")), normal.index(self.queue.task("narrow")))
        self.assertLess(normal.index(self.queue.task("narrow")), normal.index(self.queue.task("ordinary")))
        claude = self.queue.eligible_tasks(
            0, now_epoch=NOW, claude_priority=True,
            capabilities=(db.LEGACY_EXCLUSIVE_CAPABILITY,),
        )
        self.assertEqual(claude[0].id, "exclusive")


class RecoverCompleteContracts(RecoveryCase):
    def prepare(self, task_id: str = "parent"):
        self.add(task_id)
        self.add(f"{task_id}-child", depends_on=[task_id])
        source = self.fail_task(task_id)
        self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)
        return source

    def test_same_thread_completion_succeeds_without_an_automatic_projection(self) -> None:
        self.add("standalone")
        source = self.fail_task("standalone")
        self.assertEqual(self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False), ())
        self.assertIsNone(self.queue.recovery_for("standalone"))

        completed = self.queue.recover_complete(
            "standalone", expected_attempt_id=source.id,
            outcome=verified(evidence="fixture://standalone-correction"),
            summary="same thread supplied the missing proof", now_epoch=NOW + 1,
        )

        self.assertEqual(completed.status, "done")
        self.assertNotEqual(completed.attempt_id, source.id)
        self.assertEqual(self.queue.claims(), [])
        self.assertIsNone(self.queue.recovery_for("standalone"))
        self.assertEqual(self.queue.readiness("standalone", now_epoch=NOW + 1)["state"], "done")

    def test_recover_complete_appends_verified_attempt_without_claim_router_or_history_loss(self) -> None:
        source = self.prepare()
        before_runs = list(self.queue.runs(task_id="parent"))
        with (
            mock.patch.object(dispatcher, "_call_router") as router,
            mock.patch("bonus_drain.reconcile.execute_adapter") as status_probe,
        ):
            completed = self.queue.recover_complete(
                "parent", expected_attempt_id=source.id,
                outcome=verified(evidence="fixture://continued-work"),
                summary="continued work proved the contract", now_epoch=NOW + 10,
            )
        router.assert_not_called()
        status_probe.assert_not_called()
        self.assertNotEqual(completed.attempt_id, source.id)
        attempts = rows(self.queue, "SELECT * FROM task_attempts WHERE task_id='parent' ORDER BY ordinal")
        self.assertEqual([(row["state"], row["mode"], row["origin"]) for row in attempts], [
            ("failed", "normal", "normal"),
            ("done", "verification", "continuation"),
        ])
        self.assertEqual(len(self.queue.runs(task_id="parent")), len(before_runs) + 1)
        self.assertEqual(self.queue.claims(), [])
        self.assertEqual(self.queue.activation_leases(), [])
        self.assertEqual(self.queue.inflight(), [])
        self.assertEqual(rows(self.queue, "SELECT * FROM task_recovery WHERE task_id='parent'"), [])
        self.assertTrue(self.queue.readiness("parent-child", now_epoch=NOW + 10)["ready"])

        replay = self.queue.recover_complete(
            "parent", expected_attempt_id=source.id,
            outcome=verified(evidence="fixture://continued-work"),
            summary="continued work proved the contract", now_epoch=NOW + 11,
        )
        self.assertEqual(replay.attempt_id, completed.attempt_id)
        with self.assertRaisesRegex(db.QueueError, "conflict|already completed"):
            self.queue.recover_complete(
                "parent", expected_attempt_id=source.id,
                outcome=verified(evidence="fixture://different"),
                summary="different proof", now_epoch=NOW + 11,
            )

    def test_recover_complete_refuses_unverified_wrong_stale_held_and_successor_sources_atomically(self) -> None:
        source = self.prepare()
        before = rows(self.queue, "SELECT * FROM task_attempts WHERE task_id='parent'")
        refusals = [
            {"expected_attempt_id": "not-the-source", "outcome": verified()},
            {"expected_attempt_id": source.id, "outcome": {"completion": {"verified": False}}},
        ]
        for kwargs in refusals:
            with self.subTest(kwargs=kwargs), self.assertRaises(db.QueueError):
                self.queue.recover_complete(
                    "parent", summary="must refuse", now_epoch=NOW + 1, **kwargs,
                )
            self.assertEqual(rows(self.queue, "SELECT * FROM task_attempts WHERE task_id='parent'"), before)

        successor = self.claim("parent", automatic=True, now=NOW + 300)
        with self.assertRaisesRegex(db.QueueError, successor.id):
            self.queue.recover_complete(
                "parent", expected_attempt_id=source.id, outcome=verified(),
                summary="source lost its CAS", now_epoch=NOW + 300,
            )
        self.assertEqual(self.queue.claim_for("parent").attempt_id, successor.id)
        self.assertEqual(rows(
            self.queue,
            "SELECT COUNT(*) AS n FROM task_attempts WHERE task_id='parent' AND mode='verification'",
        )[0]["n"], 0)

        self.add("authority")
        self.add("authority-child", depends_on=["authority"])
        held_source = self.claim("authority")
        self.terminal(
            "authority", held_source, "failed",
            reason("authority_required", "authority:missing"),
        )
        self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)
        with self.assertRaisesRegex(db.QueueError, "authority|held"):
            self.queue.recover_complete(
                "authority", expected_attempt_id=held_source.id, outcome=verified(),
                summary="cannot invent authority", now_epoch=NOW + 1,
            )

    def test_recover_complete_rolls_back_if_the_verification_attempt_insert_is_interrupted(self) -> None:
        source = self.prepare()
        before_runs = rows(self.queue, "SELECT * FROM runs WHERE task='parent'")
        before_recovery = rows(self.queue, "SELECT * FROM task_recovery WHERE task_id='parent'")
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute(
                "CREATE TRIGGER interrupt_verification BEFORE INSERT ON task_attempts "
                "WHEN NEW.mode='verification' BEGIN SELECT RAISE(ABORT, 'fixture interruption'); END"
            )
        with self.assertRaises((db.QueueError, sqlite3.DatabaseError)):
            self.queue.recover_complete(
                "parent", expected_attempt_id=source.id, outcome=verified(),
                summary="would have completed", now_epoch=NOW + 1,
            )
        self.assertEqual(rows(self.queue, "SELECT * FROM runs WHERE task='parent'"), before_runs)
        self.assertEqual(rows(self.queue, "SELECT * FROM task_recovery WHERE task_id='parent'"), before_recovery)

    def test_requeue_then_edit_updates_task_and_projection_hash_together(self) -> None:
        self.add("failed")
        source = self.fail_task("failed")
        self.queue.requeue("failed", attempt_id=source.id, mode="retry", now_epoch=NOW)
        old_hash = rows(self.queue, "SELECT contract_hash FROM task_recovery WHERE task_id='failed'")[0]["contract_hash"]
        updated = self.queue.edit_task("failed", {"goal": "new exact contract"})
        recovery = rows(self.queue, "SELECT * FROM task_recovery WHERE task_id='failed'")[0]
        self.assertEqual(updated.goal, "new exact contract")
        self.assertNotEqual(recovery["contract_hash"], old_hash)
        claimed = self.claim("failed", now=NOW)
        claimed_row = rows(
            self.queue, "SELECT contract_hash FROM task_attempts WHERE id=?", (claimed.id,),
        )[0]
        self.assertEqual(recovery["contract_hash"], claimed_row["contract_hash"])
        with self.assertRaises(db.QueueError):
            self.queue.request_recovery(
                "failed", expected_attempt_id="stale", mode="retry",
                source="operator", now_epoch=NOW,
            )
        self.assertEqual(self.queue.task("failed").goal, "new exact contract")

        self.add("automatic")
        self.add("automatic-child", depends_on=["automatic"])
        self.fail_task("automatic")
        self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)
        with self.assertRaisesRegex(db.QueueError, "operator|automatic"):
            self.queue.edit_task("automatic", {"goal": "must not change"})


class DependencyPreflightTransactionContracts(RecoveryCase):
    def prepare_parent(self, parent_id: str) -> dict[str, object]:
        self.add(parent_id)
        attempt = self.claim(parent_id, key=f"manual/{parent_id}")
        repository = {
            "remote": "https://example.test/repository.git",
            "target_ref": "refs/heads/main",
            "target_base_oid": "a" * 40,
            "branch_ref": f"refs/heads/{parent_id}",
            "head_oid": "b" * 40,
            "integration_state": "unmerged",
        }
        self.terminal(
            parent_id, attempt, "done", verified(repository),
            key=f"manual/{parent_id}",
        )
        return {
            "base_oid": repository["head_oid"],
            "branch_ref": repository["branch_ref"],
            "target_ref": repository["target_ref"],
            "parent_ids": [parent_id],
        }

    def test_claim_releases_queue_lock_during_preflight_and_rejects_a_stale_contract(self) -> None:
        parent_id = "claim-parent"
        dependency_base = self.prepare_parent(parent_id)
        self.add("claim-child", depends_on=[parent_id])
        writer_opened = False

        def resolve(_cwd: str, _outcomes: object) -> dict[str, object]:
            nonlocal writer_opened
            with sqlite3.connect(self.queue.path, timeout=0) as independent:
                independent.execute("BEGIN IMMEDIATE")
                writer_opened = True
                independent.rollback()
            concurrent_queue = db.QueueDB(self.queue.path)
            concurrent_queue.edit_task(
                "claim-child", {"goal": "contract changed during network preflight"},
            )
            return dependency_base

        with mock.patch(
            "bonus_drain.handoff.resolve_dependency_base", side_effect=resolve,
        ) as resolver:
            claimed = self.queue.claim(
                "claim-child", "manual/claim-child", "alpha", "alpha-account",
                now_epoch=NOW,
            )

        self.assertTrue(writer_opened)
        resolver.assert_called_once()
        self.assertIsNone(claimed)
        self.assertEqual(
            self.queue.task("claim-child").goal,
            "contract changed during network preflight",
        )
        self.assertEqual(self.queue.claims(), [])
        self.assertEqual(self.queue.inflight(), [])
        self.assertEqual(self.queue.activation_leases(), [])
        self.assertEqual(rows(
            self.queue, "SELECT * FROM task_attempts WHERE task_id='claim-child'",
        ), [])

    def test_recover_complete_releases_queue_lock_during_dependency_preflight(self) -> None:
        parent_id = "completion-parent"
        dependency_base = self.prepare_parent(parent_id)
        self.add("completion-child", depends_on=[parent_id])
        with mock.patch(
            "bonus_drain.handoff.resolve_dependency_base", return_value=dependency_base,
        ):
            source = self.claim("completion-child", key="manual/completion-child")
        self.terminal(
            "completion-child", source, "failed", reason(),
            key="manual/completion-child",
        )
        writer_opened = False

        def resolve(_cwd: str, _outcomes: object) -> dict[str, object]:
            nonlocal writer_opened
            with sqlite3.connect(self.queue.path, timeout=0) as independent:
                independent.execute("BEGIN IMMEDIATE")
                writer_opened = True
                independent.rollback()
            return dependency_base

        with mock.patch(
            "bonus_drain.handoff.resolve_dependency_base", side_effect=resolve,
        ) as resolver:
            completed = self.queue.recover_complete(
                "completion-child", expected_attempt_id=source.id,
                outcome=verified(evidence="fixture://continued-child-proof"),
                summary="continued work verified the child",
                now_epoch=NOW + 1,
            )

        self.assertTrue(writer_opened)
        resolver.assert_called_once()
        self.assertEqual(completed.status, "done")
        self.assertEqual(self.queue.claims(), [])
        self.assertFalse(self.queue.readiness("completion-child", now_epoch=NOW + 1)["ready"])
        self.assertEqual(
            self.queue.readiness("completion-child", now_epoch=NOW + 1)["state"], "done",
        )


class DispatcherOwnershipAndOutcomeContracts(RecoveryCase):
    def claimed_prompt(self, task_id: str, *, kind: str) -> str:
        changes: dict[str, object] = {
            "kind": kind,
            "done_when": "a committed branch and its verification evidence are retained",
            "use_implement": True,
        }
        key = KEY
        if kind == "recurring":
            changes["cadence"] = "weekly"
            key = "alpha-account/alpha-weekly/2000002000"
        queued = self.add(task_id, **changes)
        attempt = self.claim(task_id, key=key)
        return dispatcher.render_prompt(
            runtime(self.queue.path), queued, key, "alpha", "alpha-account",
            attempt=attempt, outcome_path=self.root / f"{task_id}-outcome.json",
        )

    def assert_prompt_defines_structured_outcome(self, prompt: str) -> None:
        tokens = prompt_json_tokens(prompt)
        required_fields = {
            "reason", "code", "detail", "signature",
            "completion", "verified", "mechanism", "evidence",
            "repository", "remote", "target_ref", "target_base_oid",
            "branch_ref", "head_oid", "integration_state",
        }
        self.assertEqual(required_fields - tokens, set(), prompt)
        valid_mechanisms = {
            "command", "artifact", "operator_receipt", "goal_acceptance",
        }
        self.assertEqual(valid_mechanisms - tokens, set(), prompt)

    def test_claimed_oneoff_prompt_defines_machine_readable_terminal_and_handoff_outcome(self) -> None:
        prompt = self.claimed_prompt("oneoff-prompt", kind="oneoff")
        self.assert_prompt_defines_structured_outcome(prompt)
        self.assertIn("recover-complete", prompt)

    def test_claimed_recurring_prompt_defines_terminal_outcome_without_continuation_command(self) -> None:
        prompt = self.claimed_prompt("recurring-prompt", kind="recurring")
        self.assert_prompt_defines_structured_outcome(prompt)
        self.assertNotIn("recover-complete", prompt)

    def _dispatch(self, task_id: str, response: object, *, cfg=None, activation_call=None):
        cfg = cfg or runtime(self.queue.path)
        self.add(task_id)
        if isinstance(response, BaseException):
            callback = mock.Mock(side_effect=response)
        else:
            callback = mock.Mock(return_value=response)
        with self.assertRaises(dispatcher.DispatchError):
            dispatcher.dispatch(
                cfg, self.queue, task_id=task_id, eligibility_key=f"manual/{task_id}",
                requested_provider="alpha", router_call=callback,
                activation_call=activation_call,
            )
        return callback

    def test_known_not_launched_aborts_exact_attempt_restores_recovery_and_spends_no_budget(self) -> None:
        self.add("retry")
        self.add("retry-child", depends_on=["retry"])
        self.fail_task("retry")
        self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)
        with (
            mock.patch.object(db.time, "time", return_value=NOW + 300),
            mock.patch.object(self.queue, "requeue") as public_requeue,
            self.assertRaises(dispatcher.KnownDispatchFailure),
        ):
            dispatcher.dispatch(
                runtime(self.queue.path), self.queue, task_id="retry", eligibility_key=KEY,
                requested_provider="alpha",
                trigger="bonus",
                router_call=lambda *_args, **_kwargs: {
                    "dispatch": {"launched": False}, "error": "admission refused",
                },
            )
        public_requeue.assert_not_called()
        attempts = rows(self.queue, "SELECT * FROM task_attempts WHERE task_id='retry' ORDER BY ordinal")
        self.assertEqual([row["state"] for row in attempts], ["failed", "aborted"])
        self.assertEqual(rows(
            self.queue,
            "SELECT COUNT(*) AS n FROM task_attempts WHERE task_id='retry' "
            "AND origin='automatic' AND state!='aborted'",
        )[0]["n"], 0)
        recovery = rows(self.queue, "SELECT * FROM task_recovery WHERE task_id='retry'")[0]
        self.assertEqual((recovery["state"], recovery["consumed_by_attempt_id"]), ("scheduled", None))
        self.assertEqual(self.queue.claims(), [])
        self.assertEqual(self.queue.activation_leases(), [])
        self.assertEqual([run.status for run in self.queue.runs(task_id="retry")], ["failed"])
        self.assertTrue(self.queue.readiness("retry", now_epoch=NOW + 300)["ready"])

    def test_prelaunch_activation_failure_and_known_router_rejection_are_immediately_eligible(self) -> None:
        cfg = runtime(self.queue.path, activation=True)
        self.add("activation-failed")
        router = mock.Mock()
        with self.assertRaises(dispatcher.DispatchError):
            dispatcher.dispatch(
                cfg, self.queue, task_id="activation-failed", eligibility_key="manual/activation",
                requested_provider="alpha", router_call=router,
                activation_call=mock.Mock(side_effect=dispatcher.ActivationUnavailable(
                    "requested account did not become active", known_not_switched=True,
                )),
            )
        router.assert_not_called()
        self.assertEqual(rows(
            self.queue, "SELECT state FROM task_attempts WHERE task_id='activation-failed'",
        ), [{"state": "aborted"}])
        self.assertTrue(self.queue.readiness("activation-failed", now_epoch=NOW)["ready"])

        self._dispatch(
            "router-rejected", {"dispatch": {"launched": False}, "error": "no slot"},
        )
        self.assertEqual(rows(
            self.queue, "SELECT state FROM task_attempts WHERE task_id='router-rejected'",
        ), [{"state": "aborted"}])
        self.assertTrue(self.queue.readiness("router-rejected", now_epoch=NOW)["ready"])

    def test_unknown_not_switched_back_text_keeps_exact_ambiguous_ownership(self) -> None:
        cfg = runtime(self.queue.path, activation=True)
        self.add("activation-unknown")
        router = mock.Mock()
        activation = mock.Mock(
            side_effect=RuntimeError("account not switched back; state unknown"),
        )

        with self.assertRaises(dispatcher.AmbiguousDispatch):
            dispatcher.dispatch(
                cfg, self.queue, task_id="activation-unknown",
                eligibility_key="manual/activation-unknown", requested_provider="alpha",
                router_call=router, activation_call=activation,
            )

        router.assert_not_called()
        activation.assert_called_once_with("activate", "alpha-account")
        attempt = self.queue.attempts(task_id="activation-unknown")[0]
        self.assertEqual(attempt.state, "ambiguous")
        claim = self.queue.claim_for("activation-unknown", "manual/activation-unknown")
        self.assertEqual((claim.state, claim.attempt_id), ("ambiguous", attempt.id))
        self.assertFalse(self.queue.readiness("activation-unknown", now_epoch=NOW)["ready"])

    def test_invalid_done_evidence_keeps_activation_owned_until_valid_correction(self) -> None:
        self.add("evidence-owner")
        attempt = self.claim("evidence-owner")
        self.queue.acquire_activation(
            "evidence-owner", KEY, "alpha", "alpha-account", lambda: None,
            attempt_id=attempt.id,
        )
        self.queue.record(
            "evidence-owner", KEY, attempt_id=attempt.id, status="dispatched",
            provider_id="alpha", account_id="alpha-account", router_job_id="job-evidence",
            timestamp=iso(NOW), now_epoch=NOW,
        )
        release = mock.Mock()
        invalid = verified()
        invalid["completion"] = {
            "verified": False, "mechanism": "command", "evidence": ["fixture://invalid"],
        }

        with self.assertRaisesRegex(db.QueueError, "verified"):
            self.queue.record(
                "evidence-owner", KEY, attempt_id=attempt.id, status="done",
                outcome=invalid, release_activation=release,
                timestamp=iso(NOW + 1), now_epoch=NOW + 1,
            )

        release.assert_not_called()
        self.assertEqual(self.queue.claim_for("evidence-owner", KEY).attempt_id, attempt.id)
        self.assertEqual(self.queue.activation_leases()[0].state, "active")
        self.assertEqual(self.queue.inflight()[0].attempt_id, attempt.id)
        self.assertEqual(
            [event.status for event in self.queue.runs(task_id="evidence-owner")],
            ["dispatched"],
        )

        corrected = self.queue.record(
            "evidence-owner", KEY, attempt_id=attempt.id, status="done",
            outcome=verified(evidence="fixture://valid-correction"),
            release_activation=release, timestamp=iso(NOW + 2), now_epoch=NOW + 2,
        )
        self.assertEqual(corrected.attempt_id, attempt.id)
        release.assert_called_once_with()
        self.assertEqual(self.queue.claims(), [])
        self.assertEqual(self.queue.activation_leases(), [])
        self.assertEqual(self.queue.inflight(), [])

    def test_incomplete_and_orphan_leases_close_only_the_owning_provider(self) -> None:
        for lease_state in ("activating", "active", "releasing", "orphan"):
            with self.subTest(lease_state=lease_state):
                queue = db.QueueDB(self.root / f"lease-{lease_state}.db")
                queue.initialize()
                queue.add_task(task(
                    "lease-owner", self.root, allowed_providers=["alpha"],
                ))
                queue.add_task(task(
                    "alpha-ready", self.root, allowed_providers=["alpha"],
                ))
                queue.add_task(task(
                    "beta-ready", self.root, allowed_providers=["beta"],
                ))
                key = f"alpha-account/manual/{lease_state}"
                attempt_id = None
                stored_state = "active" if lease_state == "orphan" else lease_state
                if lease_state != "orphan":
                    attempt = queue.claim(
                        "lease-owner", key, "alpha", "alpha-account", now_epoch=NOW,
                    )
                    self.assertIsNotNone(attempt)
                    attempt_id = attempt.id
                with sqlite3.connect(queue.path) as connection:
                    connection.execute(
                        "INSERT INTO activation_leases("
                        "task_id,eligibility_key,provider_id,account_id,state,acquired_at,attempt_id"
                        ") VALUES(?,?,?,?,?,?,?)",
                        (
                            "lease-owner", key, "alpha", "alpha-account", stored_state,
                            iso(NOW), attempt_id,
                        ),
                    )

                health = db.doctor(queue)
                self.assertFalse(health.ok)
                self.assertIn("alpha", health.provider_holds)
                self.assertNotIn("beta", health.provider_holds)
                self.assertEqual(queue.runs(task_id="lease-owner"), [])
                with (
                    mock.patch.object(scout, "read_all", return_value=_open_snapshots()),
                    mock.patch.object(scout, "dispatch") as dispatch_mock,
                ):
                    report = scout.run_once(
                        _two_provider_config(queue, self.root / f"cache-{lease_state}"),
                        queue, now_epoch=NOW,
                    )

                launched = [call.kwargs["task_id"] for call in dispatch_mock.call_args_list]
                self.assertEqual(launched, ["beta-ready"])
                self.assertIn(("alpha", "alpha-account"), report.plan.closed)

    def test_every_uncertain_router_identity_retains_one_ambiguous_owner_and_capacity_hold(self) -> None:
        cases: dict[str, object] = {
            "timeout": subprocess.TimeoutExpired(["agent-router"], 1),
            "malformed": subprocess.CompletedProcess([], 0, b"not-json", b""),
            "missing": {},
            "duplicate": subprocess.CompletedProcess(
                [], 0,
                b'{"dispatch":{"job_id":"one"},"dispatch":{"job_id":"two"}}', b"",
            ),
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                callback = self._dispatch(name, response)
                attempt = rows(self.queue, "SELECT * FROM task_attempts WHERE task_id=?", (name,))[0]
                self.assertEqual(attempt["state"], "ambiguous")
                claim = self.queue.claim_for(name)
                self.assertEqual((claim.state, claim.attempt_id), ("ambiguous", attempt["id"]))
                self.assertFalse(self.queue.readiness(name, now_epoch=NOW)["ready"])
                with self.assertRaises(dispatcher.AlreadyClaimed):
                    dispatcher.dispatch(
                        runtime(self.queue.path), self.queue, task_id=name,
                        eligibility_key=f"manual/{name}", requested_provider="alpha",
                        router_call=callback,
                    )
                self.assertEqual(callback.call_count, 1)

    def test_launch_scope_cleanup_uncertainty_keeps_dispatched_row_and_exact_owner(self) -> None:
        cfg = runtime(self.queue.path, activation=True, activation_scope="launch")
        self.add("cleanup")

        def activation(_cfg, _account, action, _callback):
            if action == "release":
                raise RuntimeError("release state unknown")

        with (
            mock.patch.object(dispatcher, "_activation", side_effect=activation),
            self.assertRaises(dispatcher.AmbiguousDispatch),
        ):
            dispatcher.dispatch(
                cfg, self.queue, task_id="cleanup", eligibility_key="manual/cleanup",
                requested_provider="alpha",
                router_call=lambda *_args, **_kwargs: {
                    "dispatch": {"job_id": "job-cleanup", "launched": True},
                },
            )
        attempt = rows(self.queue, "SELECT * FROM task_attempts WHERE task_id='cleanup'")[0]
        self.assertEqual(attempt["state"], "ambiguous")
        self.assertEqual(self.queue.claim_for("cleanup").attempt_id, attempt["id"])
        self.assertEqual(self.queue.inflight()[0].attempt_id, attempt["id"])
        self.assertEqual(len(self.queue.activation_leases()), 1)

    def test_known_nonlaunch_with_uncertain_injected_activation_cleanup_is_ambiguous(self) -> None:
        cfg = runtime(self.queue.path, activation=True)
        self.add("cleanup-unknown")

        def activation(action: str, _account: str) -> None:
            if action == "release":
                raise RuntimeError("cannot prove credentials were restored")

        with self.assertRaises(dispatcher.AmbiguousDispatch):
            dispatcher.dispatch(
                cfg, self.queue, task_id="cleanup-unknown",
                eligibility_key="manual/cleanup-unknown", requested_provider="alpha",
                activation_call=activation,
                router_call=lambda *_args, **_kwargs: {
                    "dispatch": {"launched": False}, "error": "rejected before spawn",
                },
            )
        attempt = rows(
            self.queue, "SELECT * FROM task_attempts WHERE task_id='cleanup-unknown'",
        )[0]
        self.assertEqual(attempt["state"], "ambiguous")
        self.assertEqual(self.queue.claim_for("cleanup-unknown").attempt_id, attempt["id"])
        self.assertFalse(self.queue.readiness("cleanup-unknown", now_epoch=NOW)["ready"])

    def test_outcome_file_is_private_validated_bounded_and_removed_only_after_commit(self) -> None:
        self.add("outcome")
        result = dispatcher.dispatch(
            runtime(self.queue.path), self.queue, task_id="outcome",
            eligibility_key="manual/outcome", requested_provider="alpha",
            router_call=lambda *_args, **_kwargs: {
                "dispatch": {"job_id": "job-outcome", "launched": True},
            },
        )
        command_line = next(
            line.strip() for line in result.prompt.splitlines()
            if "recover-complete" in line and "--outcome-file" in line
        )
        argv = shlex.split(command_line)
        self.assertNotIn("--config", argv)
        outcome_path = Path(argv[argv.index("--outcome-file") + 1])
        self.assertTrue(outcome_path.is_relative_to(self.queue.path.parent / "outcomes"))
        self.assertTrue(outcome_path.is_file())
        self.assertEqual(stat.S_IMODE(outcome_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(outcome_path.stat().st_mode), 0o600)

        attempt_id = self.queue.claim_for("outcome").attempt_id
        invalid_values: list[tuple[str, object, int, str]] = [
            ("wrong-mode", verified(), 0o644, "mode 0600"),
            ("oversize", "x" * 65_537, 0o600, "exceeds"),
            ("non-object", [], 0o600, "one JSON object"),
        ]
        for name, value, mode, expected_error in invalid_values:
            path = outcome_path.parent / f"{name}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            path.chmod(mode)
            with captured_json() as failures:
                code = cli.main([
                    "record", "--database", str(self.queue.path), "--task", "outcome",
                    "--eligibility-key", "manual/outcome", "--attempt-id", attempt_id,
                    "--status", "done", "--outcome-file", str(path), "--json",
                ])
            self.assertEqual(code, 2, name)
            self.assertIn(expected_error, failures[0]["error"])
            self.assertTrue(path.exists(), name)
        target = outcome_path.parent / "target.json"
        target.write_text(json.dumps(verified()), encoding="utf-8")
        target.chmod(0o600)
        symlink = outcome_path.parent / "symlink.json"
        symlink.symlink_to(target)
        with captured_json() as failures:
            self.assertEqual(cli.main([
                "record", "--database", str(self.queue.path), "--task", "outcome",
                "--eligibility-key", "manual/outcome", "--attempt-id", attempt_id,
                "--status", "done", "--outcome-file", str(symlink), "--json",
            ]), 2)
        self.assertIn("regular file", failures[0]["error"])
        self.assertTrue(symlink.is_symlink())

        wrong_owner = outcome_path.parent / "wrong-owner.json"
        wrong_owner.write_text(json.dumps(verified()), encoding="utf-8")
        wrong_owner.chmod(0o600)
        real_lstat = Path.lstat
        metadata = wrong_owner.lstat()

        def lstat_with_foreign_file(path: Path):
            if path == wrong_owner:
                return mock.Mock(st_mode=metadata.st_mode, st_uid=os.getuid() + 1)
            return real_lstat(path)

        with (
            mock.patch.object(Path, "lstat", autospec=True, side_effect=lstat_with_foreign_file),
            captured_json() as failures,
        ):
            self.assertEqual(cli.main([
                "record", "--database", str(self.queue.path), "--task", "outcome",
                "--eligibility-key", "manual/outcome", "--attempt-id", attempt_id,
                "--status", "done", "--outcome-file", str(wrong_owner), "--json",
            ]), 2)
        self.assertIn("owned by the current user", failures[0]["error"])
        self.assertTrue(wrong_owner.exists())

        outcome_path.write_text(json.dumps(verified()), encoding="utf-8")
        outcome_path.chmod(0o600)
        with captured_json() as payloads:
            self.assertEqual(cli.main([
                "record", "--database", str(self.queue.path), "--task", "outcome",
                "--eligibility-key", "manual/outcome", "--attempt-id", attempt_id,
                "--status", "done", "--outcome-file", str(outcome_path), "--json",
            ]), 0)
        self.assertFalse(outcome_path.exists())
        self.assertTrue(payloads[0]["run"]["outcome"]["completion"]["verified"])


class GoalRecoveryContracts(RecoveryCase):
    def goal_contract(self, goal_id: str = "release") -> dict[str, object]:
        return {
            "id": goal_id,
            "title": goal_id,
            "cwd": str(self.root),
            "outcome": "combined proof",
            "authority": "local fixture only",
            "acceptance": [{"id": "journey", "proof": "fixture evidence"}],
            "merge_policy": "stack",
            "max_turns": 5,
            "deadline": NOW + 3_600,
            "max_inflight": 1,
            "coordinator": {"model": "gpt-6-astra"},
            "task_ids": [],
        }

    def finish_claimed(self, task_id: str, status: str = "done") -> object:
        claim = self.queue.claim_for(task_id)
        if claim is None:
            attempt = self.claim(task_id, key=f"manual/{task_id}")
            key = f"manual/{task_id}"
        else:
            attempt = type("AttemptRef", (), {"id": claim.attempt_id})()
            key = claim.eligibility_key
        return self.terminal(
            task_id, attempt, status,
            verified() if status == "done" else reason(signature=f"retryable:{task_id}"),
            key=key,
        )

    def create_members(self):
        store = goals.GoalStore(self.queue)
        store.create(self.goal_contract(), now=NOW)
        store.tick(now=NOW)
        goal = store.show("release")
        turn = goal["coordinator_task"]
        attempt = self.claim(turn, key=f"manual/{turn}")
        self.queue.record(
            turn, f"manual/{turn}", attempt_id=attempt.id, status="dispatched",
            provider_id="alpha", router_job_id="coordinator-job",
        )
        candidate = {"commits": {"fixture": "a" * 40}, "runtime": {}}
        store.advance(
            "release", turn,
            {
                "expected_revision": goal["revision"],
                "action": "wait",
                "summary": "wait for members",
                "tasks": [
                    {
                        "role": "implementation",
                        "task": task("implementation", self.root, use_implement=True),
                    },
                    {
                        "role": "integration",
                        "task": task("integration", self.root, use_implement=True),
                    },
                    {
                        "role": "acceptance",
                        "candidate": candidate,
                        "task": task("acceptance", self.root, use_implement=False),
                    },
                ],
                "wait_for": ["implementation", "integration", "acceptance"],
                "candidate": candidate,
            },
            now=NOW,
        )
        self.finish_claimed(turn)
        return store, candidate

    def test_cli_recover_complete_rejects_worker_time_and_uses_server_clock(self) -> None:
        self.create_members()
        source = self.fail_task("implementation", now=NOW)
        server_now = NOW + 1
        worker_now = NOW + 7_200
        cfg = runtime(self.queue.path)
        outcome_path = dispatcher.materialize_outcome_file(cfg, source.id)
        outcome_path.write_text(
            json.dumps(verified(evidence="fixture://server-clock")), encoding="utf-8",
        )
        arguments = [
            "recover-complete", "--database", str(self.queue.path),
            "--task", "implementation", "--from-attempt", source.id,
            "--outcome-file", str(outcome_path), "--summary", "server-timed proof", "--json",
        ]

        with captured_json() as rejected:
            rejected_code = cli.main([*arguments, "--now", str(worker_now)])
        self.assertEqual(rejected_code, 2)
        self.assertIn("--now", rejected[0]["error"])
        self.assertTrue(outcome_path.exists())

        with (
            mock.patch.dict(
                os.environ,
                {"BONUS_DRAIN_NOW": str(worker_now), "BONUS_DRAIN_CONFIG": ""},
            ),
            mock.patch.object(cli, "_now", side_effect=AssertionError("worker clock used")),
            mock.patch.object(db.time, "time", return_value=server_now),
            captured_json() as payloads,
        ):
            code = cli.main(arguments)

        self.assertEqual(code, 0, payloads)
        self.assertFalse(outcome_path.exists())
        completion_id = payloads[0]["run"]["attempt_id"]
        attempt = rows(
            self.queue,
            "SELECT created_at,terminal_at FROM task_attempts WHERE id=?",
            (completion_id,),
        )[0]
        receipt = rows(
            self.queue,
            "SELECT ts,received_at FROM runs WHERE attempt_id=?",
            (completion_id,),
        )[0]
        self.assertEqual(attempt, {
            "created_at": iso(server_now), "terminal_at": iso(server_now),
        })
        self.assertEqual(receipt, {
            "ts": iso(server_now), "received_at": iso(server_now),
        })

    def test_only_admitted_implementation_and_integration_get_same_id_recovery(self) -> None:
        store, candidate = self.create_members()
        self.finish_claimed("implementation", "failed")
        self.finish_claimed("acceptance", "failed")
        before_goal = store.show("release")
        decisions = self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)
        by_task = {as_dict(item)["task_id"]: as_dict(item) for item in decisions}
        self.assertEqual(by_task["implementation"]["mode"], "retry")
        self.assertEqual(by_task["acceptance"]["reason_code"], "fresh_goal_followup_required")
        self.assertEqual(rows(
            self.queue, "SELECT * FROM task_recovery WHERE task_id='acceptance'",
        ), [])
        with self.assertRaisesRegex(
            db.QueueError,
            "goal-owned run history must be retained; create a fresh follow-up job",
        ):
            self.queue.requeue("implementation")
        shown = store.show("release")
        self.assertEqual(shown["candidate"], candidate)
        self.assertEqual(shown["revision"], before_goal["revision"])
        self.assertEqual(shown["decisions"][0]["decision"].get("observations", []), [])
        self.assertEqual(shown["operations"], [])

    def test_paused_deadline_contract_concurrency_and_coordinator_guards_hold_recovery(self) -> None:
        store, _candidate = self.create_members()
        implementation = self.fail_task("implementation")
        current = store.show("release")
        store.steer("release", current["revision"], "pause fixture", pause=True, now=NOW)
        decision = self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)[0]
        self.assertEqual(as_dict(decision)["state"], "held")
        self.assertRegex(as_dict(decision)["reason_code"], "goal|paused|admission")
        self.assertEqual(rows(
            self.queue, "SELECT COUNT(*) AS n FROM task_attempts WHERE task_id='implementation'",
        )[0]["n"], 1)
        self.assertEqual(implementation.id, rows(
            self.queue, "SELECT id FROM task_attempts WHERE task_id='implementation'",
        )[0]["id"])

        coordinator = store.show("release")["coordinator_task"]
        if coordinator:
            with self.assertRaises(db.QueueError):
                self.queue.request_recovery(
                    coordinator, source="operator", mode="retry", now_epoch=NOW,
                )

    def test_paused_goal_recovery_reschedules_when_the_goal_resumes(self) -> None:
        store, _candidate = self.create_members()
        source = self.fail_task("implementation", signature="retryable:paused", now=NOW)
        current = store.show("release")
        paused = store.steer(
            "release", current["revision"], "pause the fixture", pause=True, now=NOW,
        )
        held = self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)[0]
        self.assertEqual((held.state, held.reason_code), ("held", "goal_paused"))

        store.resume("release", paused["revision"], "resume the fixture", now=NOW + 1)
        rescheduled = self.queue.reconcile_recoveries(now_epoch=NOW + 1, dry_run=False)[0]

        self.assertEqual(rescheduled.after_attempt_id, source.id)
        self.assertEqual((rescheduled.origin, rescheduled.state), ("automatic", "backoff"))
        self.assertEqual(self.queue.recovery_for("implementation").state, "backoff")

    def test_concurrency_held_recovery_reschedules_after_the_peer_finishes(self) -> None:
        self.create_members()
        source = self.fail_task("implementation", signature="retryable:concurrency", now=NOW)
        peer = self.claim("integration", key="manual/integration", now=NOW)
        self.queue.record(
            "integration", "manual/integration", attempt_id=peer.id, status="dispatched",
            provider_id="alpha", account_id="alpha-account", router_job_id="peer-job",
            timestamp=iso(NOW), now_epoch=NOW,
        )
        held = self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)[0]
        self.assertEqual((held.state, held.reason_code), ("held", "goal_concurrency_held"))

        self.terminal(
            "integration", peer, "done", verified(), key="manual/integration", now=NOW + 1,
        )
        rescheduled = self.queue.reconcile_recoveries(now_epoch=NOW + 1, dry_run=False)[0]

        self.assertEqual(rescheduled.after_attempt_id, source.id)
        self.assertEqual((rescheduled.origin, rescheduled.state), ("automatic", "backoff"))
        self.assertEqual(self.queue.recovery_for("implementation").state, "backoff")




if __name__ == "__main__":
    unittest.main()
