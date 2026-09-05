"""Dispatch writes the factory runs row for /implement tasks and never fails on it."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import config as config_module, db, dispatcher  # noqa: E402

REAL_TELEMETRY = Path.home() / ".claude" / "skills" / "implement" / "factory-telemetry.py"


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


def _config(root: Path) -> config_module.RuntimeConfig:
    router = config_module.AdapterConfig(
        "router", "agent-router", (str(root / "bin" / "agent-router"),),
        timeout_seconds=0.2, max_output_bytes=1024,
    )
    return config_module.RuntimeConfig(
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
        accounts=(config_module.AccountConfig("claude-account", "claude", "claude-plan"),),
        limits=(
            config_module.LimitConfig("claude-weekly", "claude-plan", 604800, 95, 20000, 1),
        ),
        viewer={},
        pr_exceptions=(),
        usage_max_age_seconds=3600,
        cache_dir=root / "cache",
    )


def _router_response(job_id: str = "job-1", log_id: Any = 4242) -> dict[str, Any]:
    return {
        "provider": "claude",
        "dispatch": {"job_id": job_id, "launched": True, "job_name": "Bonus: x"},
        "log_id": log_id,
    }


class FactoryRunRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.config = _config(self.root)
        self.queue = db.QueueDB(self.config.database)
        self.queue.initialize()
        self._saved_env = {
            key: os.environ.get(key)
            for key in (dispatcher.FACTORY_TELEMETRY_ENV, "IMPLEMENT_FACTORY_DB")
        }

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._temporary.cleanup()

    def _dispatch(self, task_id: str, **kwargs: Any) -> tuple[dispatcher.DispatchResult, list[list[str]]]:
        seen: list[list[str]] = []

        def router_call(argv: list[str], **_kwargs: object) -> dict[str, object]:
            seen.append(list(argv))
            return _router_response()

        result = dispatcher.dispatch(
            self.config, self.queue, task_id=task_id,
            eligibility_key=f"manual/{task_id}", requested_provider="claude",
            router_call=router_call, **kwargs,
        )
        return result, seen

    def test_implement_task_writes_run_row_with_drain_and_router_ids(self) -> None:
        self.queue.add_task(_task("impl-task", use_implement=True, cwd=str(self.root)))
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def telemetry_call(argv: list[str], payload: dict[str, Any]) -> None:
            calls.append((argv, payload))

        result, seen = self._dispatch("impl-task", telemetry_call=telemetry_call)

        self.assertEqual(len(calls), 1)
        argv, payload = calls[0]
        self.assertEqual(argv[:2], ["record", "run"])
        run_id = argv[argv.index("--run-id") + 1]
        self.assertTrue(run_id.startswith("drain-impl-task-"), run_id)
        self.assertEqual(result.factory_run_id, run_id)
        self.assertEqual(payload["drain_task_id"], "impl-task")
        self.assertEqual(payload["router_decision_id"], 4242)
        self.assertEqual(payload["tier"], "quick")
        self.assertEqual(payload["launch_mode"], "background")
        self.assertEqual(payload["status"], "dispatched")
        self.assertEqual(payload["repo"], self.root.name)
        self.assertEqual(payload["factory_version"], "v1")

        launch = next(argv for argv in seen if "--dry-run" not in argv)
        prompt = launch[-1]
        lines = prompt.splitlines()
        self.assertTrue(prompt.startswith("/implement "))
        self.assertIn("BACKGROUND_RUN=1", lines)
        self.assertEqual(lines[-1], f"FACTORY_RUN_ID={run_id}")
        self.assertEqual(lines[-2], "BACKGROUND_RUN=1")
        self.assertEqual(result.to_dict()["factory_run_id"], run_id)

    def test_plain_task_writes_no_row_and_no_prompt_line(self) -> None:
        self.queue.add_task(_task("plain-task"))
        calls: list[Any] = []
        result, seen = self._dispatch("plain-task", telemetry_call=lambda a, p: calls.append(a))
        self.assertEqual(calls, [])
        self.assertIsNone(result.factory_run_id)
        launch = next(argv for argv in seen if "--dry-run" not in argv)
        self.assertNotIn("FACTORY_RUN_ID", launch[-1])

    def test_router_without_log_id_omits_decision_id(self) -> None:
        self.queue.add_task(_task("no-log", use_implement=True, cwd=str(self.root)))
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def router_call(argv: list[str], **_kwargs: object) -> dict[str, object]:
            return {"dispatch": {"job_id": "job-2", "launched": True}, "log_id": None}

        dispatcher.dispatch(
            self.config, self.queue, task_id="no-log", eligibility_key="manual/no-log",
            requested_provider="claude", router_call=router_call,
            telemetry_call=lambda a, p: calls.append((a, p)),
        )
        self.assertEqual(len(calls), 1)
        self.assertNotIn("router_decision_id", calls[0][1])
        self.assertEqual(calls[0][1]["drain_task_id"], "no-log")

    def test_telemetry_failure_never_fails_the_dispatch(self) -> None:
        self.queue.add_task(_task("boom", use_implement=True, cwd=str(self.root)))

        def telemetry_call(argv: list[str], payload: dict[str, Any]) -> None:
            raise RuntimeError("telemetry down")

        result, _seen = self._dispatch("boom", telemetry_call=telemetry_call)
        self.assertEqual(result.job_id, "job-1")
        events = self.queue.runs(task_id="boom")
        self.assertEqual([event.status for event in events], ["dispatched"])
        self.assertEqual(events[0].router_job_id, "job-1")

    def test_failing_script_is_swallowed_and_reported(self) -> None:
        script = self.root / "telemetry.py"
        script.write_text("import sys; sys.stderr.write('nope\\n'); sys.exit(1)\n", encoding="utf-8")
        os.environ[dispatcher.FACTORY_TELEMETRY_ENV] = str(script)
        self.queue.add_task(_task("script-fail", use_implement=True, cwd=str(self.root)))
        result, _seen = self._dispatch("script-fail")
        self.assertEqual(result.job_id, "job-1")
        self.assertIsNotNone(result.factory_run_id)

    def test_missing_script_is_skipped(self) -> None:
        os.environ[dispatcher.FACTORY_TELEMETRY_ENV] = str(self.root / "absent.py")
        self.queue.add_task(_task("no-script", use_implement=True, cwd=str(self.root)))
        result, _seen = self._dispatch("no-script")
        self.assertEqual(result.job_id, "job-1")

    def test_script_receives_payload_without_dispatcher_session_env(self) -> None:
        script = self.root / "telemetry.py"
        capture = self.root / "capture.json"
        script.write_text(
            "import json, os, sys\n"
            "argv = sys.argv[1:]\n"
            "payload = json.load(open(argv[argv.index('--json-file') + 1]))\n"
            "json.dump({'argv': argv, 'payload': payload,\n"
            "  'session': os.environ.get('CLAUDE_CODE_SESSION_ID'),\n"
            "  'thread': os.environ.get('CODEX_THREAD_ID')},\n"
            f"  open({str(capture)!r}, 'w'))\n",
            encoding="utf-8",
        )
        os.environ[dispatcher.FACTORY_TELEMETRY_ENV] = str(script)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "dispatcher-session"
        os.environ["CODEX_THREAD_ID"] = "dispatcher-thread"
        try:
            self.queue.add_task(_task("captured", use_implement=True, cwd=str(self.root)))
            result, _seen = self._dispatch("captured")
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            os.environ.pop("CODEX_THREAD_ID", None)
        recorded = json.loads(capture.read_text(encoding="utf-8"))
        self.assertEqual(recorded["argv"][:4], ["record", "run", "--run-id", result.factory_run_id])
        self.assertEqual(recorded["payload"]["drain_task_id"], "captured")
        self.assertEqual(recorded["payload"]["router_decision_id"], 4242)
        self.assertIsNone(recorded["session"])
        self.assertIsNone(recorded["thread"])
        self.assertFalse(list(self.root.glob("bonus-drain-factory-run-*.json")))

    def test_repo_name_uses_git_toplevel_when_cwd_is_a_repo(self) -> None:
        repo = self.root / "some-repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        nested = repo / "sub"
        nested.mkdir()
        self.assertEqual(dispatcher.factory_repo_name(str(nested)), "some-repo")
        self.assertEqual(dispatcher.factory_repo_name(str(self.root / "plain")), "plain")

    @unittest.skipUnless(REAL_TELEMETRY.is_file(), "factory-telemetry.py is not installed")
    def test_real_writer_creates_row_and_driver_event_upserts_onto_it(self) -> None:
        factory_db = self.root / "implement-factory.db"
        env = {
            key: value for key, value in os.environ.items()
            if key not in dispatcher.FACTORY_SESSION_ENV_KEYS
        }
        env["IMPLEMENT_FACTORY_DB"] = str(factory_db)
        legacy = self.root / "legacy.jsonl"
        legacy.write_text("", encoding="utf-8")
        for argv in (
            ["db", "init"],
            ["cutover", "initial-import", "--source", str(legacy)],
        ):
            step = subprocess.run(
                [sys.executable, str(REAL_TELEMETRY), *argv],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(step.returncode, 0, step.stderr or step.stdout)
        os.environ["IMPLEMENT_FACTORY_DB"] = str(factory_db)
        os.environ[dispatcher.FACTORY_TELEMETRY_ENV] = str(REAL_TELEMETRY)
        self.queue.add_task(_task("real-writer", use_implement=True, cwd=str(self.root)))
        result, _seen = self._dispatch("real-writer")
        run_id = result.factory_run_id
        assert run_id is not None

        def rows() -> list[sqlite3.Row]:
            with sqlite3.connect(factory_db) as connection:
                connection.row_factory = sqlite3.Row
                return connection.execute(
                    "SELECT * FROM runs WHERE drain_task_id='real-writer'"
                ).fetchall()

        before = rows()
        self.assertEqual(len(before), 1)
        self.assertEqual(before[0]["run_id"], run_id)
        self.assertEqual(before[0]["status"], "dispatched")
        self.assertEqual(before[0]["tier"], "quick")
        self.assertEqual(before[0]["router_decision_id"], 4242)

        payload = self.root / "driver-run.json"
        payload.write_text(json.dumps({
            "factory_version": "v1", "repo": "driver-repo", "branch": "feat",
            "tier": "build", "launch_mode": "background", "status": "running",
            "session_id": "driver-session",
        }), encoding="utf-8")
        driver = subprocess.run(
            [
                sys.executable, str(REAL_TELEMETRY), "record", "run",
                "--run-id", run_id, "--json-file", str(payload),
            ],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(driver.returncode, 0, driver.stderr or driver.stdout)
        after = rows()
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["run_id"], run_id)
        self.assertEqual(after[0]["status"], "running")
        self.assertEqual(after[0]["tier"], "build")
        self.assertEqual(after[0]["repo"], "driver-repo")
        self.assertEqual(after[0]["session_id"], "driver-session")
        self.assertEqual(after[0]["drain_task_id"], "real-writer")
        self.assertEqual(after[0]["router_decision_id"], 4242)


if __name__ == "__main__":
    unittest.main()
