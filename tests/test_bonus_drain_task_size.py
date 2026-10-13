"""Red-on-revert contracts for Bonus Drain task-size metadata."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.readiness_fixture import isolated_cli, proxy_fixture, review_args, reviewed

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import cli, db, dispatcher, scout, usage
from bonus_drain import config as config_module

SIZES = ("tiny", "small", "medium", "large", "huge")
NOW = 2_000_000_000
_MISSING = object()


def _task_values(task_id: str, *, size: object = _MISSING) -> dict[str, object]:
    values: dict[str, object] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "kind": "oneoff",
        "priority": 2,
        "cwd": "/tmp/bonus-size",
        "goal": f"Complete {task_id}",
        "created_at": "2026-08-26T00:00:00Z",
        "active": True,
        "allowed_providers": ("alpha",),
        "required_capabilities": ("cpu",),
    }
    if size is not _MISSING:
        values["size"] = size
    return values


def _cli_add(database: Path, task_id: str, size: object = _MISSING) -> list[str]:
    argv = [
        "add", "--database", str(database),
        "--id", task_id, "--title", f"Task {task_id}",
        "--kind", "oneoff", "--cwd", "/tmp/bonus-size",
        "--goal", f"Complete {task_id}",
    ]
    if size is not _MISSING:
        argv.extend(("--size", str(size)))
    argv.extend(review_args(argv))
    argv.append("--json")
    return argv


@contextlib.contextmanager
def _capture_cli_json() -> Iterator[list[object]]:
    payloads: list[object] = []
    with mock.patch.object(
        cli, "_json", side_effect=lambda value, **_kwargs: payloads.append(value),
    ):
        yield payloads


def _runtime_config(database: Path) -> config_module.RuntimeConfig:
    router = config_module.AdapterConfig(
        "router", "t3-thread", ("/bin/true",),
    )
    return proxy_fixture(config_module.RuntimeConfig(schema_version=1, source_path=None, database=database, record_command=("/bin/true",), secret_refs=(), adapters=(router,), providers=(
            config_module.ProviderConfig("alpha", config_module.DispatchBinding("router", "alpha"), "huge", frozenset({"cpu"})),
        ), limits=(
            config_module.LimitConfig(
                "alpha-weekly", "alpha", 604_800, 95, 20_000, 3,
            ),
        ), viewer={}, pr_exceptions=(), usage_max_age_seconds=3_600, cache_dir=database.parent / "cache", proxy_secret_ref=None), (("alpha", "alpha-account"),))


class TaskSizeContractTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "queue.db"
        self.queue = db.QueueDB(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_queue_json_exposes_graph_backed_provider_eligibility(self) -> None:
        self.queue.add_task(reviewed(_task_values("provider-filtered", size="small")))
        args = type("Args", (), {
            "command": "queue", "cycle": NOW, "run_limit": 10, "json": True,
        })()
        with (
            mock.patch.object(cli, "_queue", return_value=(_runtime_config(self.database), self.queue)),
            _capture_cli_json() as payloads,
        ):
            self.assertEqual(cli._command(args), 0)
        self.assertEqual(
            payloads[0]["eligible_provider_ids"]["provider-filtered"],
            ["alpha"],
        )
        self.assertEqual(
            payloads[0]["compatible_provider_ids"]["provider-filtered"],
            ["alpha"],
        )

    def test_queue_json_keeps_compatible_providers_for_waiting_tasks(self) -> None:
        self.queue.add_task(reviewed(_task_values("parent", size="small")))
        child = _task_values("child", size="small")
        child["depends_on"] = ["parent"]
        self.queue.add_task(reviewed(child))
        args = type("Args", (), {
            "command": "queue", "cycle": NOW, "run_limit": 10, "json": True,
        })()
        with (
            mock.patch.object(cli, "_queue", return_value=(_runtime_config(self.database), self.queue)),
            _capture_cli_json() as payloads,
        ):
            self.assertEqual(cli._command(args), 0)
        self.assertEqual(payloads[0]["readiness"]["child"]["state"], "waiting")
        self.assertNotIn("child", payloads[0]["eligible_task_ids"])
        self.assertNotIn("child", payloads[0]["eligible_provider_ids"])
        self.assertEqual(payloads[0]["compatible_provider_ids"]["child"], ["alpha"])
        self.assertEqual(payloads[0]["eligible_provider_ids"]["parent"], ["alpha"])

    def assert_plain_nullable_size_column(self, database: Path) -> None:
        with sqlite3.connect(database) as connection:
            columns = connection.execute("PRAGMA table_info(tasks)").fetchall()
            matches = [column for column in columns if column[1] == "size"]
            self.assertEqual(len(matches), 1)
            column = matches[0]
            self.assertEqual(str(column[2]).upper(), "TEXT")
            self.assertEqual(column[3], 0, "size must remain nullable")
            self.assertIsNone(column[4], "size must not acquire a default")
            create_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'",
            ).fetchone()[0]
        self.assertNotRegex(
            create_sql,
            r"(?i)\bsize\s+TEXT\s+(?:NOT\s+NULL|DEFAULT|CHECK)",
            "size is an app-validated plain nullable TEXT column",
        )

    def test_pre_size_database_migrates_additively_without_rewriting_tasks_or_runs(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE tasks (
                  id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  priority INTEGER NOT NULL DEFAULT 2,
                  cadence TEXT,
                  cwd TEXT NOT NULL,
                  goal TEXT NOT NULL,
                  context TEXT,
                  constraints TEXT,
                  precondition TEXT,
                  done_when TEXT,
                  created_at TEXT NOT NULL,
                  active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE runs (
                  rowid_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                  task TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  cycle INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  ts TEXT NOT NULL,
                  branch TEXT,
                  summary TEXT
                );
                """,
            )
            connection.executemany(
                """INSERT INTO tasks(
                     id,title,kind,priority,cadence,cwd,goal,context,constraints,
                     precondition,done_when,created_at,active
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        "completed", "Completed historical task", "oneoff", 1, None,
                        "/tmp/completed", "was completed", "old context", "old constraint",
                        "old precondition", "old proof", "2026-08-01T00:00:00Z", 1,
                    ),
                    (
                        "eligible", "Eligible historical task", "oneoff", 2, None,
                        "/tmp/eligible", "still upcoming", None, None, None, None,
                        "2026-08-02T00:00:00Z", 1,
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO runs(
                     task,kind,cycle,status,ts,branch,summary
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    "completed", "oneoff", 1_999_999_999, "done",
                    "2026-08-20T12:00:00Z", "work/completed", "historical proof",
                ),
            )
            task_rows_before = connection.execute(
                "SELECT id,title,kind,priority,cadence,cwd,goal,context,constraints,"
                "precondition,done_when,created_at,active FROM tasks ORDER BY id",
            ).fetchall()
            run_rows_before = connection.execute(
                "SELECT rowid_pk,task,kind,cycle,status,ts,branch,summary FROM runs",
            ).fetchall()

        self.queue.initialize()
        self.assert_plain_nullable_size_column(self.database)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT id,title,kind,priority,cadence,cwd,goal,context,constraints,"
                    "precondition,done_when,created_at,active FROM tasks ORDER BY id",
                ).fetchall(),
                task_rows_before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT rowid_pk,task,kind,cycle,status,ts,branch,summary FROM runs",
                ).fetchall(),
                run_rows_before,
            )
            self.assertEqual(
                connection.execute("SELECT id,size FROM tasks ORDER BY id").fetchall(),
                [("completed", None), ("eligible", None)],
            )
            counts_after_first_init = (
                connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            )
            columns_after_first_init = connection.execute(
                "PRAGMA table_info(tasks)",
            ).fetchall()

        self.queue.initialize()
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                (
                    connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                    connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                ),
                counts_after_first_init,
            )
            self.assertEqual(
                connection.execute("PRAGMA table_info(tasks)").fetchall(),
                columns_after_first_init,
            )
            # Initialization records v1, may record v2 (status CHECK relaxation), and records
            # v3 (preflight schema) and v4 (readiness review); a second initialize adds nothing.
            versions = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            self.assertTrue({1, 3, 4} <= versions <= {1, 2, 3, 4, 5}, versions)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                len(versions),
            )

    def test_all_sizes_round_trip_through_lower_level_and_required_cli_add(self) -> None:
        self.queue.initialize()
        self.assert_plain_nullable_size_column(self.database)

        for size in SIZES:
            with self.subTest(surface="lower-level", size=size):
                task = self.queue.add_task(reviewed(_task_values(f"lower-{size}", size=size)))
                self.assertEqual(task.size, size)
                self.assertEqual(self.queue.task(task.id).size, size)

        for size in SIZES:
            with self.subTest(surface="cli", size=size), _capture_cli_json() as payloads:
                code = isolated_cli(_cli_add(self.database, f"cli-{size}", size))
                self.assertEqual(code, 0)
                self.assertEqual(len(payloads), 1)
                self.assertEqual(payloads[0]["task"], self.queue.task(f"cli-{size}").to_dict())
                self.assertEqual(payloads[0]["checks"], [])
                self.assertEqual(payloads[0]["task"]["size"], size)

        legacy = self.queue.add_task(reviewed(_task_values("legacy-missing-size")))
        self.assertIsNone(legacy.size)
        self.assertIsNone(self.queue.task(legacy.id).size)
        with sqlite3.connect(self.database) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT size FROM tasks WHERE id='legacy-missing-size'",
                ).fetchone()[0],
            )

        count_before_rejections = len(self.queue.tasks())
        for index, invalid in enumerate(("", "unknown", "MEDIUM", "extra-large")):
            with self.subTest(surface="lower-level-invalid", size=invalid):
                with self.assertRaises(db.QueueError):
                    self.queue.add_task(reviewed(
                        _task_values(f"bad-lower-{index}", size=invalid)),
                    )
                self.assertIsNone(self.queue.task(f"bad-lower-{index}"))

            with self.subTest(surface="cli-invalid", size=invalid), _capture_cli_json():
                code = isolated_cli(_cli_add(self.database, f"bad-cli-{index}", invalid))
                self.assertEqual(code, 2)
                self.assertIsNone(self.queue.task(f"bad-cli-{index}"))

        with _capture_cli_json():
            self.assertEqual(
                isolated_cli(_cli_add(self.database, "missing-cli-size")),
                2,
            )
        self.assertIsNone(self.queue.task("missing-cli-size"))
        self.assertEqual(len(self.queue.tasks()), count_before_rejections)

    def test_set_size_requires_an_authoritative_cycle_and_only_sizes_that_cycles_upcoming_tasks(self) -> None:
        cycle = NOW
        self.queue.add_task(reviewed(_task_values("target")))
        self.queue.add_task(reviewed(_task_values("untouched", size="small")))
        self.queue.add_task(reviewed(_task_values("spent")))
        self.queue.add_task(reviewed(_task_values("weekly-current") | {
            "kind": "recurring", "cadence": "weekly",
        }))
        self.queue.add_task(reviewed(_task_values("weekly-prior") | {
            "kind": "recurring", "cadence": "weekly",
        }))
        self.queue.add_task(reviewed(_task_values("inactive") | {"active": False}))
        self.queue.add_task(reviewed(_task_values("claimed")))
        spent_attempt = self.queue.claim(
            "spent", f"alpha-account/alpha-weekly/{cycle}", "alpha", "alpha-account",
            provider_max_size="huge", provider_capabilities=("cpu",), now_epoch=cycle,
        )
        self.assertIsNotNone(spent_attempt)
        self.queue.record(
            "spent", f"alpha-account/alpha-weekly/{cycle}",
            attempt_id=spent_attempt.id, status="done", cycle=cycle,
            outcome={
                "reason": {
                    "code": "done_when_verified", "detail": "fixture proof",
                    "signature": "done_when_verified:task-size-spent",
                },
                "completion": {
                    "verified": True,
                    "mechanism": "command",
                    "evidence": ["fixture://task-size-spent"],
                },
            },
        )
        self.queue.record("weekly-current", status="done", cycle=cycle)
        self.queue.record(
            "weekly-prior", status="done", cycle=cycle - 604_800,
            timestamp=(datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
        )
        self.assertTrue(self.queue.claim(
            "claimed", f"alpha-account/alpha-weekly/{cycle}", "alpha", "alpha-account",
            provider_max_size="huge", provider_capabilities=("cpu",),
        ))

        for size in SIZES:
            with self.subTest(surface="lower-level", size=size):
                updated = self.queue.set_size("target", size, cycle)
                self.assertEqual(updated.id, "target")
                self.assertEqual(updated.size, size)
                self.assertEqual(self.queue.task("target").size, size)
                self.assertEqual(self.queue.task("untouched").size, "small")

        recurring = self.queue.set_size("weekly-prior", "huge", cycle)
        self.assertEqual(recurring.size, "huge")

        with self.assertRaises(TypeError):
            self.queue.set_size("target", "medium")
        self.assertEqual(self.queue.task("target").size, "huge")

        for task_id in ("spent", "weekly-current", "inactive", "claimed"):
            with self.subTest(surface="lower-level-ineligible", task=task_id):
                with self.assertRaises(db.QueueError):
                    self.queue.set_size(task_id, "large", cycle)
                self.assertIsNone(self.queue.task(task_id).size)

            with self.subTest(surface="cli-ineligible", task=task_id), _capture_cli_json():
                self.assertEqual(
                    isolated_cli([
                        "set-size", task_id, "large", "--cycle", str(cycle),
                        "--database", str(self.database), "--json",
                    ]),
                    2,
                )
                self.assertIsNone(self.queue.task(task_id).size)

        with _capture_cli_json():
            self.assertEqual(
                isolated_cli([
                    "set-size", "target", "medium",
                    "--database", str(self.database), "--json",
                ]),
                2,
                "CLI must require an explicit frozen authoritative pick cycle",
            )
        self.assertEqual(self.queue.task("target").size, "huge")

        with _capture_cli_json() as payloads:
            code = isolated_cli([
                "set-size", "target", "medium", "--cycle", str(cycle),
                "--database", str(self.database), "--json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(payloads[0]["task"]["id"], "target")
        self.assertEqual(payloads[0]["task"]["size"], "medium")
        self.assertEqual(self.queue.task("target").size, "medium")

        for invalid in ("", "unknown", "MEDIUM", "extra-large"):
            with self.subTest(surface="lower-level-invalid", size=invalid):
                with self.assertRaises(db.QueueError):
                    self.queue.set_size("target", invalid, cycle)
                self.assertEqual(self.queue.task("target").size, "medium")

            with self.subTest(surface="cli-invalid", size=invalid), _capture_cli_json():
                code = isolated_cli([
                    "set-size", "target", invalid,
                    "--cycle", str(cycle), "--database", str(self.database), "--json",
                ])
                self.assertEqual(code, 2)
                self.assertEqual(self.queue.task("target").size, "medium")

        with self.assertRaises(db.QueueError):
            self.queue.set_size("no-such-task", "large", cycle)
        with _capture_cli_json():
            self.assertEqual(
                isolated_cli([
                    "set-size", "no-such-task", "large",
                    "--cycle", str(cycle), "--database", str(self.database), "--json",
                ]),
                2,
            )
        self.assertEqual(
            {task.id for task in self.queue.tasks()},
            {"target", "untouched", "spent", "weekly-current", "weekly-prior", "inactive", "claimed"},
        )
        self.assertEqual(self.queue.task("untouched").size, "small")

    def test_monthly_cooldown_uses_the_last_run_time_not_the_dispatch_cycle(self) -> None:
        self.queue.add_task(reviewed(_task_values("monthly") | {"kind": "recurring", "cadence": "monthly"}))
        now = NOW

        def timestamp(seconds_ago: int) -> str:
            return datetime.fromtimestamp(now - seconds_ago, timezone.utc).isoformat()

        # A later provider-reset cycle must not make the task eligible before its
        # elapsed monthly cooldown has ended.
        self.queue.record("monthly", status="skipped", cycle=456, timestamp=timestamp(27 * 24 * 60 * 60))
        with mock.patch.object(db.time, "time", return_value=now):
            self.assertEqual(
                self.queue.eligible_tasks(now + 604_800, max_size="huge", provider_id="alpha", capabilities=("cpu",)),
                [],
            )

        self.queue.record("monthly", status="done", cycle=987, timestamp=timestamp(28 * 24 * 60 * 60))
        with mock.patch.object(db.time, "time", return_value=now):
            self.assertEqual(
                {
                    task.id for task in self.queue.eligible_tasks(
                        now + 1_209_600, max_size="huge", provider_id="alpha", capabilities=("cpu",),
                    )
                },
                {"monthly"},
            )

    def test_task_json_surfaces_carry_size_and_null_and_render_json_accepts_both(self) -> None:
        unscoped = {"allowed_providers": (), "required_capabilities": ()}
        sized = self.queue.add_task(reviewed(_task_values("sized", size="medium") | unscoped))
        legacy = self.queue.add_task(reviewed(_task_values("legacy") | unscoped))
        expected = {"sized": "medium", "legacy": None}

        self.assertEqual(sized.to_dict()["size"], "medium")
        self.assertIsNone(legacy.to_dict()["size"])
        with _capture_cli_json() as payloads:
            self.assertEqual(
                isolated_cli(["queue", "--database", str(self.database), "--json"]),
                0,
            )
        self.assertEqual(
            {task["id"]: task["size"] for task in payloads[0]["tasks"]},
            expected,
        )

        with _capture_cli_json() as payloads:
            self.assertEqual(
                isolated_cli(["pick", "--database", str(self.database), "10", str(NOW)]),
                0,
            )
        self.assertEqual(
            {task["id"]: task["size"] for task in payloads[0]},
            expected,
        )

        for task in (sized, legacy):
            with self.subTest(surface="contract-task", task=task.id), _capture_cli_json() as payloads:
                self.assertEqual(
                    isolated_cli([
                        "contract-task", "--database", str(self.database),
                        "--id", task.id, "--title", task.title,
                    ]),
                    0,
                )
                self.assertEqual(len(payloads[0]), 1)
                self.assertEqual(payloads[0][0]["size"], expected[task.id])

        rendered_tasks: list[db.Task] = []

        def render_stub(
            _config: config_module.RuntimeConfig,
            task: db.Task,
            _key: str,
            _provider: str,
            _account: str | None,
            *,
            preflight_checks: object,
        ) -> str:
            # render-prompt-json builds a Task with no stored checks, so it renders none.
            self.assertEqual(preflight_checks, ())
            rendered_tasks.append(task)
            return f"rendered {task.id}"

        raw_sized = sized.to_dict()
        raw_legacy = legacy.to_dict()
        raw_legacy.pop("size")
        with mock.patch.object(cli.dispatcher, "render_prompt", side_effect=render_stub):
            for raw in (raw_sized, raw_legacy):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = isolated_cli([
                        "render-prompt-json", "--database", str(self.database),
                        "--task-json", json.dumps(raw), "--cycle", str(NOW),
                        "--provider", "alpha",
                    ])
                self.assertEqual(code, 0)
                self.assertEqual(output.getvalue().strip(), f"rendered {raw['id']}")
        self.assertEqual(
            [(task.id, task.size) for task in rendered_tasks],
            [("sized", "medium"), ("legacy", None)],
        )

    def _size_config(self, max_size: str = "medium") -> config_module.RuntimeConfig:
        cfg = _runtime_config(self.database)
        cfg = replace(cfg, providers=(replace(cfg.providers[0], max_size=max_size),))
        snapshot = usage.UsageSnapshot(
            "alpha", "alpha-account", NOW,
            {"seven_day": {"used_percent": 20, "resets_at": NOW + 1_000}},
        )
        usage.write_cache(usage.cache_path(cfg.usage_cache_dir, "alpha", "alpha-account"), snapshot.cache_dict())
        return cfg

    def test_provider_size_ceiling_filters_real_plan_and_keeps_queue_visibility(self) -> None:
        cfg = self._size_config()
        for task_id, size in (("smaller", "small"), ("equal", "medium"),
                              ("greater", "large"), ("unsized", None)):
            self.queue.add_task(reviewed(_task_values(task_id, size=size)))
        self.assertCountEqual([task.id for task in self.queue.eligible_tasks(NOW, capabilities={"cpu"})],
                              ["smaller", "equal", "greater", "unsized"])
        eligible = self.queue.eligible_tasks(
            NOW, provider_id="alpha", capabilities=("cpu",), max_size="medium",
            now_epoch=NOW,
        )
        self.assertCountEqual([task.id for task in eligible], ["smaller", "equal"])
        tick = scout.plan_tick(cfg, self.queue, now_epoch=NOW)
        self.assertCountEqual([task.id for tasks in tick.allocations.values() for task in tasks],
                              ["smaller", "equal"])
        holds = {item["task_id"]: item for item in tick.dependency_holds
                 if item.get("kind") == "provider_size"}
        self.assertEqual(set(holds), {"greater", "unsized"})
        self.assertEqual(holds["unsized"]["size"], "huge")
        self.assertEqual(holds["greater"]["max_size"], "medium")
        self.assertIn("task size large exceeds provider alpha max_size medium",
                      holds["greater"]["reason"])

    def test_plan_json_exposes_provider_size_blockers_and_preserves_subscriptions(self) -> None:
        cfg = _runtime_config(self.database)
        cfg = replace(
            cfg,
            providers=tuple(
                config_module.ProviderConfig(
                    provider_id, config_module.DispatchBinding("router", provider_id),
                    max_size, frozenset({"cpu"}),
                )
                for provider_id, max_size in (("grok", "small"), ("muse", "medium"))
            ),
            limits=tuple(
                replace(cfg.limits[0], id=f"{provider_id}-weekly", provider_id=provider_id)
                for provider_id in ("grok", "muse")
            ),
        )
        accounts = (("grok", "grok-account"), ("muse", "muse-account"))
        proxy_fixture(cfg, accounts, now=NOW)
        for provider_id, account_id in accounts:
            snapshot = usage.UsageSnapshot(
                provider_id, account_id, NOW,
                {"seven_day": {"used_percent": 20, "resets_at": NOW + 1_000}},
            )
            usage.write_cache(
                usage.cache_path(cfg.usage_cache_dir, provider_id, account_id),
                snapshot.cache_dict(),
            )
        for task_id, size, providers in (
            ("small-grok", "small", ("grok",)),
            ("medium-muse", "medium", ("muse",)),
            ("large-restricted", "large", ("grok", "muse")),
        ):
            self.queue.add_task(reviewed(
                _task_values(task_id, size=size) | {"allowed_providers": providers},
            ))

        payloads = {}
        with (
            mock.patch.object(cli, "_load_config", return_value=cfg),
            mock.patch.object(cli, "_host_load_reader", return_value=None),
        ):
            for command in ("plan", "gates"):
                output = io.StringIO()
                with mock.patch.dict(cli._json.__kwdefaults__, {"stream": output}):
                    code = isolated_cli([
                        command, "--database", str(self.database), "--now", str(NOW), "--json",
                    ])
                self.assertEqual(code, 0)
                payloads[command] = json.loads(output.getvalue())

        plan = payloads["plan"]
        self.assertCountEqual(
            [(batch["provider_id"], batch["batch_size"]) for batch in plan["batches"]],
            [("grok", 1), ("muse", 1)],
        )
        allocations = payloads["gates"]["allocations"]
        self.assertNotIn("large-restricted", [row["task_id"] for row in allocations])
        self.assertCountEqual(
            [(row["task_id"], row["provider_id"]) for row in allocations],
            [("small-grok", "grok"), ("medium-muse", "muse")],
        )
        blockers = [row for row in plan["blockers"] if row.get("kind") == "provider_size"]
        self.assertCountEqual(
            [(row["task_id"], row["provider_id"], row["size"], row["max_size"])
             for row in blockers],
            [("large-restricted", "grok", "large", "small"),
             ("large-restricted", "muse", "large", "medium")],
        )
        for blocker in blockers:
            self.assertIn(
                f"task size large exceeds provider {blocker['provider_id']} "
                f"max_size {blocker['max_size']}",
                blocker["reason"],
            )
        self.assertEqual(plan["subscriptions"], payloads["gates"]["subscriptions"])
        self.assertCountEqual(
            [(row["provider_id"], row["account_id"]) for row in plan["subscriptions"]],
            accounts,
        )
        for subscription in plan["subscriptions"]:
            self.assertEqual(
                subscription["limits"]["seven_day"],
                {"used_percent": 20, "resets_at": NOW + 1_000},
            )

    def test_atomic_claim_accepts_smaller_and_equal_and_refuses_greater_or_unsized(self) -> None:
        for task_id, size, accepted in (("smaller", "small", True), ("equal", "medium", True),
                                       ("greater", "large", False), ("unsized", None, False)):
            with self.subTest(size=size):
                self.queue.add_task(reviewed(_task_values(task_id, size=size)))
                attempt = self.queue.claim(
                    task_id, f"alpha/manual/{NOW}", "alpha", "alpha",
                    provider_capabilities=("cpu",), provider_max_size="medium", now_epoch=NOW,
                )
                self.assertEqual(attempt is not None, accepted)
                self.assertEqual(self.queue.claim_for(task_id) is not None, accepted)
        self.assertIsNotNone(self.queue.claim(
            "unsized", f"alpha/manual/{NOW + 1}", "alpha", "alpha",
            provider_capabilities=("cpu",), provider_max_size="huge", now_epoch=NOW,
        ))

    def test_explicit_dispatch_applies_size_ceiling_before_any_launch_or_claim(self) -> None:
        cfg = self._size_config()
        for task_id, size, accepted in (("smaller", "small", True), ("equal", "medium", True),
                                       ("greater", "large", False), ("unsized", None, False)):
            with self.subTest(size=size):
                self.queue.add_task(reviewed(_task_values(task_id, size=size)))
                launch = mock.Mock(return_value={"threadId": f"thread-{task_id}"})
                if accepted:
                    result = dispatcher.dispatch(
                        cfg, self.queue, task_id=task_id, eligibility_key=f"alpha/manual/{NOW}",
                        requested_provider="alpha", router_call=launch, now_epoch=NOW,
                    )
                    self.assertEqual(result.account_id, "alpha")
                    launch.assert_called_once()
                else:
                    with self.assertRaises(dispatcher.InvalidRoute):
                        dispatcher.dispatch(
                            cfg, self.queue, task_id=task_id, eligibility_key=f"alpha/manual/{NOW}",
                            requested_provider="alpha", router_call=launch, now_epoch=NOW,
                        )
                    launch.assert_not_called()
                    self.assertIsNone(self.queue.claim_for(task_id))

    def test_auto_dispatch_selects_provider_that_can_fit_task_and_refuses_if_none_fit(self) -> None:
        cfg = self._size_config("small")
        cfg = replace(cfg, providers=(cfg.providers[0], config_module.ProviderConfig(
            "beta", config_module.DispatchBinding("router", "beta"), "medium", frozenset({"cpu"}),
        )), limits=(*cfg.limits, replace(cfg.limits[0], id="beta-weekly", provider_id="beta")))
        proxy_fixture(cfg, (("alpha", "alpha-account"), ("beta", "beta-account")), now=NOW)
        usage.write_cache(usage.cache_path(cfg.usage_cache_dir, "beta", "beta-account"),
                          usage.UsageSnapshot("beta", "beta-account", NOW,
                              {"seven_day": {"used_percent": 20, "resets_at": NOW + 1_000}}).cache_dict())
        for task_id, size, expected in (("small", "small", "alpha"), ("equal", "medium", "beta"),
                                       ("greater", "large", None), ("unsized", None, None)):
            values = _task_values(task_id, size=size)
            values["allowed_providers"] = ("alpha", "beta")
            self.queue.add_task(reviewed(values))
            launch = mock.Mock(return_value={"threadId": f"thread-{task_id}"})
            if expected:
                result = dispatcher.dispatch(
                    cfg, self.queue, task_id=task_id, eligibility_key=f"manual/{task_id}",
                    requested_provider="auto", router_call=launch, now_epoch=NOW,
                )
                self.assertEqual(result.provider_id, expected)
                self.assertEqual(result.account_id, expected)
                launch.assert_called_once()
            else:
                with self.assertRaises(dispatcher.InvalidRoute):
                    dispatcher.dispatch(
                        cfg, self.queue, task_id=task_id, eligibility_key=f"manual/{task_id}",
                        requested_provider="auto", router_call=launch, now_epoch=NOW,
                    )
                launch.assert_not_called()
                self.assertIsNone(self.queue.claim_for(task_id))


if __name__ == "__main__":
    unittest.main()
