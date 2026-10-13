"""Tests for T3 feedback routing and delivery failures."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("big_plan_dispatch", Path(__file__).with_name("dispatch.py"))
assert SPEC and SPEC.loader
dispatch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch)


class T3DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        self.plan = self.root / "plan with spaces.md"
        self.plan.write_text("# Plan\n")
        self.thread_id = "mcp:ab612b3a-acb9-4038-ada0-474c4324a817"
        outbox = patch.object(dispatch, "OUTBOX", self.root / "outbox")
        outbox.start()
        self.addCleanup(outbox.stop)

    def register(self, thread_id=None):
        return dispatch.save_provenance(self.plan, thread_id, "Author", str(self.root))

    def test_registration_preserves_t3_id_and_null(self) -> None:
        record = self.register(self.thread_id)
        self.assertEqual(dispatch.load_provenance(self.plan), record)
        self.assertEqual(self.register()["threadId"], None)

    def test_registration_rejects_bad_thread_id_and_cwd(self) -> None:
        for value in ["", "two ids", "bad\n", 42, "x" * 257]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.register(value)
        for cwd in ["", "relative", str(self.root / "missing")]:
            with self.subTest(cwd=cwd), self.assertRaises(ValueError):
                dispatch.save_provenance(self.plan, None, "", cwd)

    def test_routes_usable_t3_thread(self) -> None:
        self.register(self.thread_id)
        with patch.object(dispatch, "_t3_api", return_value={"usable": True, "title": "Live author"}) as api:
            route = dispatch.route(self.plan, self.root)
        self.assertEqual((route["mode"], route["sessionId"], route["target"]),
                         ("thread", self.thread_id, "Live author"))
        api.assert_called_once_with("inspect", self.thread_id)

    def test_no_provenance_starts_in_git_ancestor(self) -> None:
        self.assertEqual(dispatch.route(self.plan, self.root)["mode"], "new_thread")
        self.assertEqual(dispatch.route(self.plan, self.root)["cwd"], str(self.root))

    def test_provider_sidecars_use_new_thread_and_keep_repo(self) -> None:
        for engine in ["claude", "codex", "grok"]:
            dispatch.provenance_path(self.plan).write_text(json.dumps({
                "engine": engine, "sessionId": "ab612b3a-acb9-4038-ada0-474c4324a817",
                "cwd": str(self.root),
            }))
            with patch.object(dispatch, "_t3_api") as api:
                route = dispatch.route(self.plan, self.root)
            self.assertEqual((route["mode"], route["sessionId"], route["cwd"]),
                             ("new_thread", None, str(self.root)))
            api.assert_not_called()

    def test_unusable_t3_thread_starts_new(self) -> None:
        self.register(self.thread_id)
        with patch.object(dispatch, "_t3_api", return_value={"usable": False}):
            self.assertEqual(dispatch.route(self.plan, self.root)["mode"], "new_thread")

    def test_t3_outage_does_not_change_destination(self) -> None:
        self.register(self.thread_id)
        with patch.object(dispatch, "_t3_api", side_effect=dispatch.DispatchFailed("offline")), self.assertRaisesRegex(dispatch.DispatchFailed, "offline"):
            dispatch.route(self.plan, self.root)

    def test_external_plan_needs_repo_registration(self) -> None:
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            plan = self.root / "big-plan/plans/project/review.md"
            plan.parent.mkdir(parents=True)
            plan.write_text("# Review\n")
            with self.assertRaisesRegex(ValueError, "repository cwd"):
                dispatch.route(plan, self.root)
            dispatch.save_provenance(plan, None, "", str(self.root))
            self.assertEqual(dispatch.route(plan, self.root)["cwd"], str(self.root))

    def test_malformed_sidecar_routes_new(self) -> None:
        for content in ["not json", "[]", '{"threadId":"bad id"}']:
            dispatch.provenance_path(self.plan).write_text(content)
            self.assertEqual(dispatch.route(self.plan, self.root)["mode"], "new_thread")

    def test_existing_thread_gets_full_payload_on_stdin(self) -> None:
        prompt = "Keep this literal: `id`; $(touch injected); 'quoted'\nUnicode: café"
        decision = {"mode": "thread", "sessionId": self.thread_id, "cwd": str(self.root)}
        with patch.object(dispatch, "_node_bin", return_value="/node"), patch.object(
            dispatch, "_run", return_value={"threadId": self.thread_id, "runId": "run:1"}
        ) as run, patch.object(dispatch, "route") as route:
            result = dispatch.submit(self.plan, self.plan.name, self.root, prompt, decision)
        route.assert_not_called()
        argv, stdin_text = run.call_args.args
        self.assertFalse(any(prompt in arg for arg in argv))
        request = json.loads(stdin_text)
        self.assertEqual(request["action"], "send")
        self.assertEqual(request["threadId"], self.thread_id)
        self.assertIn(prompt, request["payload"])
        self.assertIn(str(self.plan) + ".comments.json", request["payload"])
        self.assertIn("'" + str(self.plan) + "'", request["payload"])
        self.assertEqual(Path(result["payload"]).read_text(), request["payload"])

    def test_new_thread_uses_supported_launcher_and_checks_startup(self) -> None:
        prompt = "please process all feedback"
        with patch.object(dispatch, "_run", return_value={"threadId": self.thread_id}) as run, patch.object(
            dispatch, "_t3_api", return_value={"threadId": self.thread_id, "runId": "run:1"}
        ) as api:
            result = dispatch.submit(self.plan, self.plan.name, self.root, prompt)
        argv, payload = run.call_args.args
        self.assertEqual(argv, [str(dispatch.T3_SKILL / "t3-thread"), "--project", str(self.root),
                                "--prompt", "-", "--title", "Review feedback: plan with spaces", "--json"])
        self.assertIn(prompt, payload)
        api.assert_called_once_with("check", self.thread_id)
        self.assertEqual(result["mode"], "new_thread")

    def test_missing_launch_identity_is_failure(self) -> None:
        with patch.object(dispatch, "_run", return_value={}), self.assertRaisesRegex(dispatch.DispatchFailed, "no thread ID"):
            dispatch.submit(self.plan, self.plan.name, self.root, "feedback")

    def test_delivery_refusal_does_not_launch_another_thread(self) -> None:
        decision = {"mode": "thread", "sessionId": self.thread_id, "cwd": str(self.root)}
        with patch.object(dispatch, "_t3_api", side_effect=dispatch.DispatchFailed("refused")), patch.object(dispatch, "_run") as run, self.assertRaisesRegex(dispatch.DispatchFailed, "refused"):
            dispatch.submit(self.plan, self.plan.name, self.root, "feedback", decision)
        run.assert_not_called()

    def test_rejects_empty_and_oversized_prompts_before_delivery(self) -> None:
        for prompt in [None, " ", "x" * (dispatch.MAX_PROMPT_BYTES + 1)]:
            with self.subTest(prompt_type=type(prompt).__name__), self.assertRaises(ValueError):
                dispatch.submit(self.plan, self.plan.name, self.root, prompt)
        self.assertFalse(dispatch.OUTBOX.exists())

    def test_process_refusal_and_invalid_json_surface(self) -> None:
        with self.assertRaisesRegex(dispatch.DispatchFailed, "refused"):
            dispatch._run([sys.executable, "-c", "import sys; sys.stderr.write('refused'); sys.exit(1)"], "")
        with self.assertRaisesRegex(dispatch.DispatchFailed, "invalid JSON"):
            dispatch._run([sys.executable, "-c", "print('bad response')"], "")
        with self.assertRaisesRegex(dispatch.DispatchFailed, "Could not start"):
            dispatch._run([str(self.root / "missing")], "")

    def test_process_stdin_keeps_metacharacters_inert(self) -> None:
        text = json.dumps({"text": "`id`; $(touch injected)\ncafé"})
        result = dispatch._run([sys.executable, "-c", "import sys; print(sys.stdin.read())"], text)
        self.assertEqual(result, json.loads(text))
        self.assertFalse((self.root / "injected").exists())


if __name__ == "__main__":
    unittest.main()
