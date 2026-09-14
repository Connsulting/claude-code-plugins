"""A terminal ledger record closes the dispatch-written factory runs row, filling only NULLs."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import cli, db, dispatcher, factory_terminal  # noqa: E402

REAL_TELEMETRY = Path.home() / ".claude" / "skills" / "implement" / "factory-telemetry.py"
EVENT_TS = "2026-09-14T15:00:00Z"
PR_2524 = "https://github.com/example/repo/pull/2524"


class HelperTests(unittest.TestCase):
    def test_first_sentence(self) -> None:
        self.assertEqual(
            factory_terminal.first_sentence("Opened #2524 for the fix. Tests green."),
            "Opened #2524 for the fix.",
        )
        self.assertEqual(factory_terminal.first_sentence("  no terminal\n punctuation "), "no terminal punctuation")
        self.assertIsNone(factory_terminal.first_sentence("   "))
        self.assertIsNone(factory_terminal.first_sentence(None))

    def test_pr_reference_takes_the_earliest_number_or_url(self) -> None:
        self.assertEqual(factory_terminal.pr_reference("merged #2524 then #2525"), "2524")
        self.assertEqual(
            factory_terminal.pr_reference(f"see {PR_2524} (was #12)"), PR_2524,
        )
        self.assertEqual(factory_terminal.pr_reference(f"#7 supersedes {PR_2524}"), "7")
        self.assertIsNone(factory_terminal.pr_reference("no pull request here, issue&#39;s fine"))
        self.assertIsNone(factory_terminal.pr_reference(None))

    def test_resolver_without_gh_keeps_urls_and_drops_bare_numbers(self) -> None:
        with mock.patch.object(factory_terminal.shutil, "which", return_value=None):
            self.assertEqual(factory_terminal.resolve_pr_url(PR_2524, "/tmp"), PR_2524)
            self.assertIsNone(factory_terminal.resolve_pr_url("2524", "/tmp"))

    def test_skipped_maps_to_skipped_and_failed_to_failed(self) -> None:
        row = {"factory_version": "v1", "repo": "r", "tier": "quick", "status": "dispatched",
               "completed_at": None, "outcome": None, "pr_url": None}
        for ledger, expected in (("skipped", "skipped"), ("failed", "failed"), ("done", "complete")):
            payload = factory_terminal.fill_only_null(row, factory_terminal.candidate_fills(
                row, ledger, EVENT_TS, "x.", "/tmp", lambda ref, cwd: None,
            ))
            assert payload is not None
            self.assertEqual(payload["status"], expected)


@unittest.skipUnless(REAL_TELEMETRY.is_file(), "factory-telemetry.py is not installed")
class TerminalUpsertTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.factory_db = self.root / "implement-factory.db"
        self.env = {
            key: value for key, value in os.environ.items()
            if key not in dispatcher.FACTORY_SESSION_ENV_KEYS
        }
        self.env["IMPLEMENT_FACTORY_DB"] = str(self.factory_db)
        legacy = self.root / "legacy.jsonl"
        legacy.write_text("", encoding="utf-8")
        self._telemetry("db", "init")
        self._telemetry("cutover", "initial-import", "--source", str(legacy))
        patched = {
            "IMPLEMENT_FACTORY_DB": str(self.factory_db),
            dispatcher.FACTORY_TELEMETRY_ENV: str(REAL_TELEMETRY),
        }
        environ = mock.patch.dict(os.environ, patched)
        environ.start()
        self.addCleanup(environ.stop)
        self.queue = db.QueueDB(self.root / "queue.db")
        self.queue.initialize()

    def _telemetry(self, *argv: str) -> None:
        step = subprocess.run(
            [sys.executable, str(REAL_TELEMETRY), *argv],
            capture_output=True, text=True, env=self.env, check=False,
        )
        self.assertEqual(step.returncode, 0, step.stderr or step.stdout)

    def _write_run(self, run_id: str, payload: dict[str, Any]) -> None:
        path = self.root / f"{run_id}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self._telemetry("record", "run", "--run-id", run_id, "--json-file", str(path))

    def _placeholder(self, task_id: str, attempt_id: str) -> str:
        run_id = dispatcher.new_factory_run_id(task_id, attempt_id)
        self._write_run(run_id, {
            "factory_version": "v1", "repo": "repo", "tier": "quick",
            "launch_mode": "background", "status": "dispatched", "drain_task_id": task_id,
        })
        return run_id

    def _row(self, run_id: str) -> sqlite3.Row:
        with contextlib.closing(sqlite3.connect(self.factory_db)) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def _fake_gh(self, url: str) -> None:
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            f"echo \"$PWD $*\" >> {self.root / 'gh-calls'}\n"
            f"echo {url}\n",
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        path = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
        path.start()
        self.addCleanup(path.stop)

    def test_placeholder_and_done_summary_naming_a_pr_closes_complete_with_url(self) -> None:
        run_id = self._placeholder("closer", "a1b2c3d4e5f60718293a4b5c6d7e8f90")
        self._fake_gh(PR_2524)
        written = factory_terminal.record_factory_terminal(
            "closer", "a1b2c3d4e5f60718293a4b5c6d7e8f90", "done", EVENT_TS,
            "Opened #2524 with the fix. Everything green.", str(self.root),
        )
        self.assertTrue(written)
        row = self._row(run_id)
        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["completed_at"], EVENT_TS)
        self.assertEqual(row["outcome"], "Opened #2524 with the fix.")
        self.assertEqual(row["pr_url"], PR_2524)
        self.assertEqual(row["repo"], "repo")
        self.assertEqual(row["tier"], "quick")
        self.assertEqual(row["drain_task_id"], "closer")
        calls = (self.root / "gh-calls").read_text(encoding="utf-8")
        self.assertIn(f"{self.root} pr view 2524 --json url", calls)

    def test_driver_written_row_is_not_overwritten(self) -> None:
        attempt = "0f0e0d0c0b0a09080706050403020100"
        run_id = self._placeholder("driven", attempt)
        self._write_run(run_id, {
            "factory_version": "v1", "repo": "driver-repo", "tier": "build",
            "status": "blocked", "completed_at": "2026-09-14T14:00:00Z",
            "outcome": "driver outcome", "pr_url": "https://github.com/example/repo/pull/1",
        })
        self._fake_gh(PR_2524)
        written = factory_terminal.record_factory_terminal(
            "driven", attempt, "done", EVENT_TS, "Merged #2524.", str(self.root),
        )
        self.assertFalse(written)
        row = self._row(run_id)
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["completed_at"], "2026-09-14T14:00:00Z")
        self.assertEqual(row["outcome"], "driver outcome")
        self.assertEqual(row["pr_url"], "https://github.com/example/repo/pull/1")
        self.assertFalse((self.root / "gh-calls").exists())

    def test_driver_status_wins_while_null_columns_still_fill(self) -> None:
        attempt = "11112222333344445555666677778888"
        run_id = self._placeholder("half-driven", attempt)
        self._write_run(run_id, {
            "factory_version": "v1", "repo": "driver-repo", "tier": "build", "status": "running",
        })
        self.assertTrue(factory_terminal.record_factory_terminal(
            "half-driven", attempt, "failed", EVENT_TS, "Gave up. Retry later.", str(self.root),
            pr_resolver=lambda ref, cwd: None,
        ))
        row = self._row(run_id)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["completed_at"], EVENT_TS)
        self.assertEqual(row["outcome"], "Gave up.")
        self.assertEqual(row["repo"], "driver-repo")

    def test_placeholder_session_survives_the_status_transition(self) -> None:
        attempt = "abababababababababababababababab"
        run_id = dispatcher.new_factory_run_id("sessioned", attempt)
        self._write_run(run_id, {
            "factory_version": "v1", "repo": "repo", "tier": "quick", "status": "dispatched",
            "drain_task_id": "sessioned", "session_id": "known-session",
        })
        self.assertTrue(factory_terminal.record_factory_terminal(
            "sessioned", attempt, "done", EVENT_TS, "Done.", str(self.root),
            pr_resolver=lambda ref, cwd: None,
        ))
        row = self._row(run_id)
        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["session_id"], "known-session")

    def test_driver_write_during_the_pr_lookup_is_kept(self) -> None:
        attempt = "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
        run_id = self._placeholder("racing", attempt)

        def resolver(reference: str, cwd: str) -> str:
            self._write_run(run_id, {
                "factory_version": "v1", "repo": "repo", "tier": "quick", "status": "blocked",
                "outcome": "driver outcome",
            })
            return PR_2524

        self.assertTrue(factory_terminal.record_factory_terminal(
            "racing", attempt, "done", EVENT_TS, "Opened #2524.", str(self.root),
            pr_resolver=resolver,
        ))
        row = self._row(run_id)
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["outcome"], "driver outcome")
        self.assertEqual(row["completed_at"], EVENT_TS)
        self.assertEqual(row["pr_url"], PR_2524)

    def test_summary_without_a_pr_leaves_pr_url_null(self) -> None:
        attempt = "99998888777766665555444433332222"
        run_id = self._placeholder("no-pr", attempt)
        self._fake_gh(PR_2524)
        self.assertTrue(factory_terminal.record_factory_terminal(
            "no-pr", attempt, "skipped", EVENT_TS, "Precondition unmet", str(self.root),
        ))
        row = self._row(run_id)
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["outcome"], "Precondition unmet")
        self.assertIsNone(row["pr_url"])
        self.assertFalse((self.root / "gh-calls").exists())

    def test_row_for_another_task_or_missing_row_is_left_alone(self) -> None:
        self.assertFalse(factory_terminal.record_factory_terminal(
            "absent", "aaaabbbbccccddddeeeeffff00001111", "done", EVENT_TS, "x", str(self.root),
        ))
        self.assertFalse(factory_terminal.record_factory_terminal(
            "absent", None, "done", EVENT_TS, "x", str(self.root),
        ))

    def test_cli_record_closes_the_row_the_dispatch_wrote(self) -> None:
        self.queue.add_task({
            "id": "via-cli", "title": "via-cli", "kind": "oneoff", "priority": 2,
            "cwd": str(self.root), "goal": "run via-cli", "active": True, "use_implement": True,
        })
        config = _config(self.root, self.queue.path)
        result = dispatcher.dispatch(
            config, self.queue, task_id="via-cli", eligibility_key="manual/via-cli",
            requested_provider="claude",
            router_call=lambda argv, **_kwargs: {
                "dispatch": {"job_id": "job-1", "launched": True}, "log_id": 7,
            },
        )
        run_id = result.factory_run_id
        assert run_id is not None and result.attempt_id is not None
        self.assertEqual(run_id, dispatcher.new_factory_run_id("via-cli", result.attempt_id))
        self.assertEqual(self._row(run_id)["status"], "dispatched")

        with mock.patch.object(cli, "_json"):
            code = cli.main([
                "record", "--database", str(self.queue.path), "--task", "via-cli",
                "--eligibility-key", "manual/via-cli", "--status", "failed",
                "--attempt-id", result.attempt_id, "--summary", "Router lost the worker. No PR.",
                "--json",
            ])
        self.assertEqual(code, 0)
        row = self._row(run_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["outcome"], "Router lost the worker.")
        self.assertIsNotNone(row["completed_at"])
        self.assertIsNone(row["pr_url"])


def _config(root: Path, database: Path):
    from bonus_drain import config as config_module

    router = config_module.AdapterConfig(
        "router", "agent-router", (str(root / "bin" / "agent-router"),),
        timeout_seconds=0.2, max_output_bytes=1024,
    )
    return config_module.RuntimeConfig(
        schema_version=1,
        source_path=root / "config.json",
        database=database,
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
        accounts=(config_module.AccountConfig("claude-account", "claude", "claude-plan"),),
        limits=(
            config_module.LimitConfig("claude-weekly", "claude-plan", 604800, 95, 20000, 1),
        ),
        viewer={},
        pr_exceptions=(),
        usage_max_age_seconds=3600,
        cache_dir=root / "cache",
    )


if __name__ == "__main__":
    unittest.main()
