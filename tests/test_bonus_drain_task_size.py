"""Red-on-revert contracts for Bonus Drain task-size metadata."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import cli, config as config_module, db, dispatcher, scout, usage  # noqa: E402


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
        "router", "agent-router", ("/bin/true",),
    )
    return config_module.RuntimeConfig(
        schema_version=1,
        source_path=None,
        database=database,
        record_command=("/bin/true",),
        secret_refs=(),
        adapters=(router,),
        providers=(
            config_module.ProviderConfig(
                "alpha",
                config_module.DispatchBinding("router", "alpha"),
                frozenset({"cpu"}),
                "single",
            ),
        ),
        plans=(config_module.PlanConfig("alpha-plan", "alpha"),),
        accounts=(
            config_module.AccountConfig("alpha-account", "alpha", "alpha-plan"),
        ),
        limits=(
            config_module.LimitConfig(
                "alpha-weekly", "alpha-plan", 604_800, 95, 20_000, 3,
            ),
        ),
        viewer={},
        pr_exceptions=(),
        usage_max_age_seconds=3_600,
        cache_dir=database.parent / "cache",
    )


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
        self.queue.add_task(_task_values("provider-filtered", size="small"))
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
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                1,
            )

    def test_all_sizes_round_trip_through_lower_level_and_required_cli_add(self) -> None:
        self.queue.initialize()
        self.assert_plain_nullable_size_column(self.database)

        for size in SIZES:
            with self.subTest(surface="lower-level", size=size):
                task = self.queue.add_task(_task_values(f"lower-{size}", size=size))
                self.assertEqual(task.size, size)
                self.assertEqual(self.queue.task(task.id).size, size)

        for size in SIZES:
            with self.subTest(surface="cli", size=size), _capture_cli_json() as payloads:
                code = cli.main(_cli_add(self.database, f"cli-{size}", size))
                self.assertEqual(code, 0)
                self.assertEqual(payloads, [{"task": self.queue.task(f"cli-{size}").to_dict()}])
                self.assertEqual(payloads[0]["task"]["size"], size)

        legacy = self.queue.add_task(_task_values("legacy-missing-size"))
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
                    self.queue.add_task(
                        _task_values(f"bad-lower-{index}", size=invalid),
                    )
                self.assertIsNone(self.queue.task(f"bad-lower-{index}"))

            with self.subTest(surface="cli-invalid", size=invalid), _capture_cli_json():
                code = cli.main(_cli_add(self.database, f"bad-cli-{index}", invalid))
                self.assertEqual(code, 2)
                self.assertIsNone(self.queue.task(f"bad-cli-{index}"))

        with _capture_cli_json():
            self.assertEqual(
                cli.main(_cli_add(self.database, "missing-cli-size")),
                2,
            )
        self.assertIsNone(self.queue.task("missing-cli-size"))
        self.assertEqual(len(self.queue.tasks()), count_before_rejections)

    def test_set_size_requires_an_authoritative_cycle_and_only_sizes_that_cycles_upcoming_tasks(self) -> None:
        cycle = NOW
        self.queue.add_task(_task_values("target"))
        self.queue.add_task(_task_values("untouched", size="small"))
        self.queue.add_task(_task_values("spent"))
        self.queue.add_task(_task_values("weekly-current") | {
            "kind": "recurring", "cadence": "weekly",
        })
        self.queue.add_task(_task_values("weekly-prior") | {
            "kind": "recurring", "cadence": "weekly",
        })
        self.queue.add_task(_task_values("inactive") | {"active": False})
        self.queue.add_task(_task_values("claimed"))
        self.queue.record("spent", status="done", cycle=cycle)
        self.queue.record("weekly-current", status="done", cycle=cycle)
        self.queue.record(
            "weekly-prior", status="done", cycle=cycle - 604_800,
            timestamp=(datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),
        )
        self.assertTrue(self.queue.claim(
            "claimed", f"alpha-account/alpha-weekly/{cycle}", "alpha", "alpha-account",
            provider_capabilities=("cpu",),
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
                    cli.main([
                        "set-size", task_id, "large", "--cycle", str(cycle),
                        "--database", str(self.database), "--json",
                    ]),
                    2,
                )
                self.assertIsNone(self.queue.task(task_id).size)

        with _capture_cli_json():
            self.assertEqual(
                cli.main([
                    "set-size", "target", "medium",
                    "--database", str(self.database), "--json",
                ]),
                2,
                "CLI must require an explicit frozen authoritative pick cycle",
            )
        self.assertEqual(self.queue.task("target").size, "huge")

        with _capture_cli_json() as payloads:
            code = cli.main([
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
                code = cli.main([
                    "set-size", "target", invalid,
                    "--cycle", str(cycle), "--database", str(self.database), "--json",
                ])
                self.assertEqual(code, 2)
                self.assertEqual(self.queue.task("target").size, "medium")

        with self.assertRaises(db.QueueError):
            self.queue.set_size("no-such-task", "large", cycle)
        with _capture_cli_json():
            self.assertEqual(
                cli.main([
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

    def test_recurring_cooldowns_use_the_last_run_time_not_the_dispatch_cycle(self) -> None:
        self.queue.add_task(_task_values("weekly") | {"kind": "recurring", "cadence": "weekly"})
        self.queue.add_task(_task_values("monthly") | {"kind": "recurring", "cadence": "monthly"})
        now = NOW

        def timestamp(seconds_ago: int) -> str:
            return datetime.fromtimestamp(now - seconds_ago, timezone.utc).isoformat()

        # The first weekly run came from a manual cycle. A later provider-reset cycle
        # must not make it eligible again before the four-day cooldown has elapsed.
        self.queue.record("weekly", status="done", cycle=123, timestamp=timestamp(3 * 24 * 60 * 60))
        self.queue.record("monthly", status="skipped", cycle=456, timestamp=timestamp(27 * 24 * 60 * 60))
        with mock.patch.object(db.time, "time", return_value=now):
            self.assertEqual(
                self.queue.eligible_tasks(now + 604_800, provider_id="alpha", capabilities=("cpu",)),
                [],
            )

        self.queue.record("weekly", status="done", cycle=789, timestamp=timestamp(4 * 24 * 60 * 60))
        self.queue.record("monthly", status="done", cycle=987, timestamp=timestamp(28 * 24 * 60 * 60))
        with mock.patch.object(db.time, "time", return_value=now):
            self.assertEqual(
                {
                    task.id for task in self.queue.eligible_tasks(
                        now + 1_209_600, provider_id="alpha", capabilities=("cpu",),
                    )
                },
                {"weekly", "monthly"},
            )

    def test_task_json_surfaces_carry_size_and_null_and_render_json_accepts_both(self) -> None:
        unscoped = {"allowed_providers": (), "required_capabilities": ()}
        sized = self.queue.add_task(_task_values("sized", size="medium") | unscoped)
        legacy = self.queue.add_task(_task_values("legacy") | unscoped)
        expected = {"sized": "medium", "legacy": None}

        self.assertEqual(sized.to_dict()["size"], "medium")
        self.assertIsNone(legacy.to_dict()["size"])
        with _capture_cli_json() as payloads:
            self.assertEqual(
                cli.main(["queue", "--database", str(self.database), "--json"]),
                0,
            )
        self.assertEqual(
            {task["id"]: task["size"] for task in payloads[0]["tasks"]},
            expected,
        )

        with _capture_cli_json() as payloads:
            self.assertEqual(
                cli.main(["pick", "--database", str(self.database), "10", str(NOW)]),
                0,
            )
        self.assertEqual(
            {task["id"]: task["size"] for task in payloads[0]},
            expected,
        )

        for task in (sized, legacy):
            with self.subTest(surface="contract-task", task=task.id), _capture_cli_json() as payloads:
                self.assertEqual(
                    cli.main([
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
        ) -> str:
            rendered_tasks.append(task)
            return f"rendered {task.id}"

        raw_sized = sized.to_dict()
        raw_legacy = legacy.to_dict()
        raw_legacy.pop("size")
        with mock.patch.object(cli.dispatcher, "render_prompt", side_effect=render_stub):
            for raw in (raw_sized, raw_legacy):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = cli.main([
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

    def test_size_is_neutral_to_order_eligibility_routing_planning_and_prompts(self) -> None:
        legacy_queue = db.QueueDB(self.root / "legacy.db")
        sized_queue = db.QueueDB(self.root / "sized.db")
        assignments = {"alpha": "huge", "beta": "tiny", "gamma": "medium"}
        for task_id in assignments:
            legacy_queue.add_task(_task_values(task_id))
            sized_queue.add_task(_task_values(task_id, size=assignments[task_id]))

        legacy_eligible = legacy_queue.eligible_tasks(
            NOW, provider_id="alpha", capabilities=("cpu",),
        )
        sized_eligible = sized_queue.eligible_tasks(
            NOW, provider_id="alpha", capabilities=("cpu",),
        )
        self.assertEqual(
            [task.id for task in legacy_eligible],
            ["alpha", "beta", "gamma"],
        )
        self.assertEqual(
            [task.id for task in sized_eligible],
            [task.id for task in legacy_eligible],
            "size must not alter eligibility or equal-priority drain order",
        )

        config = _runtime_config(sized_queue.path)
        provider = config.provider("alpha")
        for legacy_task, sized_task in zip(legacy_eligible, sized_eligible):
            with self.subTest(task=sized_task.id):
                self.assertEqual(
                    db.QueueDB._provider_compatible(
                        legacy_task, "alpha", ("cpu",),
                    ),
                    db.QueueDB._provider_compatible(
                        sized_task, "alpha", ("cpu",),
                    ),
                )
                self.assertEqual(
                    dispatcher.provider_compatible(legacy_task, provider),
                    dispatcher.provider_compatible(sized_task, provider),
                )
                self.assertEqual(
                    dispatcher.render_prompt(
                        config, legacy_task, f"alpha-account/alpha-weekly/{NOW + 1_000}",
                        "alpha", "alpha-account",
                    ),
                    dispatcher.render_prompt(
                        config, sized_task, f"alpha-account/alpha-weekly/{NOW + 1_000}",
                        "alpha", "alpha-account",
                    ),
                )
                self.assertEqual(
                    dispatcher.classification_prompt(legacy_task),
                    dispatcher.classification_prompt(sized_task),
                )

        snapshot = usage.UsageSnapshot(
            "alpha", "alpha-account", NOW,
            {"alpha-weekly": {"used_percent": 20, "resets_at": NOW + 1_000}},
        )
        snapshots = {("alpha", "alpha-account"): snapshot}
        with mock.patch.object(scout, "read_all", return_value=snapshots):
            legacy_tick = scout.plan_tick(
                replace(config, database=legacy_queue.path), legacy_queue, now_epoch=NOW,
            )
            sized_tick = scout.plan_tick(config, sized_queue, now_epoch=NOW)
        self.assertEqual(sized_tick.plan.to_dict(), legacy_tick.plan.to_dict())
        self.assertEqual(
            {
                key: [task.id for task in tasks]
                for key, tasks in sized_tick.allocations.items()
            },
            {
                key: [task.id for task in tasks]
                for key, tasks in legacy_tick.allocations.items()
            },
            "size must not alter planner capacity or scout allocation",
        )


if __name__ == "__main__":
    unittest.main()
