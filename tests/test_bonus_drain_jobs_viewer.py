"""Compatibility and security contracts for the established jobs viewer UI."""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
SERVER_PATH = SKILL_ROOT / "services" / "jobs-viewer" / "server.py"
sys.path.insert(0, str(SKILL_ROOT))


def _load_server():
    spec = importlib.util.spec_from_file_location("bonus_jobs_viewer_test", SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class JobsViewerContractTests(unittest.TestCase):
    HOST = "viewer.example.test:9443"
    ORIGIN = "https://viewer.example.test:9443"

    def setUp(self) -> None:
        self.viewer = _load_server()

    def _server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.viewer.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def _request(server, method: str, path: str, *, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_existing_ui_is_preserved_without_login_or_generic_viewer_copy(self) -> None:
        expected_frontend_hashes = {
            "CSS": "1aa4a685d0645366bcf5e0a82733abd7c6d3940466e9cb23219d57d5b888c002",
            "ICON_SPRITE": "7c5f372d21654d5980b50830bb48a53a6aa94ec9440dc34e88301d5d44f72199",
            "SCRIPT": "c3012ef3517b75f04b0b3b928194cbb9c77765dba7ffa8960f0f1f438af7e324",
            "PAGE": "d736c4f06db4fe65e6d15f1bcdf661dccee8971694d7be352226fdf83797bbda",
        }
        self.assertEqual(
            {
                name: hashlib.sha256(getattr(self.viewer, name).encode()).hexdigest()
                for name in expected_frontend_hashes
            },
            expected_frontend_hashes,
            "the established frontend is a byte-for-byte compatibility contract",
        )
        with (
            mock.patch.object(self.viewer, "render_bonus_body", return_value='<button class="task-run" data-engine="claude">force</button>'),
            mock.patch.object(self.viewer, "render_schedule_body", return_value="scheduled body"),
        ):
            page = self.viewer.render_page().decode()
        for marker in (
            "background jobs", "01 bonus-drain", "02 scheduled", "jobsViewerTab",
            'class="task-run"', 'data-engine="claude"', "scheduled body",
        ):
            self.assertIn(marker, page)
        for marker in ("authentication required", "sign in", "access secret", "bonus_drain_session"):
            self.assertNotIn(marker, page.lower())
        self.assertNotIn("independent provider capacity console", page)

    def test_secretless_force_requires_exact_browser_boundary_and_json(self) -> None:
        self.viewer.ALLOWED_HOSTS = (self.HOST,)
        self.viewer.ALLOWED_ORIGINS = (self.ORIGIN,)
        self.viewer.MUTATIONS_ENABLED = True
        server = self._server()
        with mock.patch.object(self.viewer, "render_page", return_value=b"old ui"):
            status, _headers, _body = self._request(
                server, "GET", "/", headers={"Host": "evil.example"},
            )
            self.assertEqual(status, 403)
            status, headers, body = self._request(server, "GET", "/", headers={"Host": self.HOST})
        self.assertEqual((status, body), (200, b"old ui"))
        self.assertNotIn("Set-Cookie", headers)

        payload = json.dumps({"id": "portable-a", "engine": "claude"}).encode()
        base = {
            "Host": self.HOST, "Origin": self.ORIGIN,
            "Content-Type": "application/json", "Content-Length": str(len(payload)),
        }
        with mock.patch.object(self.viewer, "run_task_now", return_value=(True, "launched")) as kick:
            status, _headers, _body = self._request(
                server, "POST", "/api/bonus/task/run",
                headers={**base, "Origin": "https://evil.example"}, body=payload,
            )
            self.assertEqual(status, 403)
            status, _headers, _body = self._request(
                server, "POST", "/api/bonus/task/run",
                headers={**base, "Content-Type": "text/plain"}, body=payload,
            )
            self.assertEqual(status, 400)
            oversized = b"{" + (b" " * 4096)
            status, _headers, _body = self._request(
                server, "POST", "/api/bonus/task/run",
                headers={**base, "Content-Length": str(len(oversized))}, body=oversized,
            )
            self.assertEqual(status, 400)
            kick.assert_not_called()
            status, _headers, body = self._request(
                server, "POST", "/api/bonus/task/run",
                headers=base, body=payload,
            )
        self.assertEqual(status, 200, body)
        kick.assert_called_once_with("portable-a", "claude")

    def test_force_delegates_once_to_shared_router_kick_service(self) -> None:
        result = mock.Mock(provider_id="claude", job_id="job-1")
        cfg = mock.Mock(database=Path("/tmp/queue.db"))
        with (
            mock.patch.object(self.viewer.graph_config, "load_config", return_value=cfg),
            mock.patch.object(self.viewer, "QueueDB") as queue_type,
            mock.patch.object(self.viewer, "kick_task", return_value=result) as kick,
            mock.patch.dict(os.environ, {"BONUS_DRAIN_CONFIG": "/tmp/config.json"}),
        ):
            ok, message = self.viewer.run_task_now("portable-a", "claude")
        self.assertTrue(ok, message)
        kick.assert_called_once_with(cfg, queue_type.return_value, "portable-a", "claude")

    def test_configured_graph_owns_database_and_runtime_bind(self) -> None:
        database = Path("/tmp/authoritative-queue.db")
        cfg = mock.Mock(
            database=database,
            viewer={
                "bind": "127.0.0.1", "mutations_enabled": True,
                "remote": {
                    "trusted_loopback_proxy": True,
                    "allowed_hosts": [self.HOST], "allowed_origins": [self.ORIGIN],
                },
            },
        )
        with mock.patch.object(self.viewer.graph_config, "load_config", return_value=cfg):
            self.viewer.configure_request_boundary()
        self.assertEqual(self.viewer.DB_PATH, database)
        self.assertEqual(self.viewer.VIEWER_BIND, "127.0.0.1")

        with (
            mock.patch.object(self.viewer.graph_config, "load_config", return_value=cfg),
            mock.patch.object(sys, "argv", [str(SERVER_PATH), "--host", "0.0.0.0"]),
            self.assertRaisesRegex(SystemExit, "must match configured loopback bind"),
        ):
            self.viewer.main()

    def test_expected_router_rejections_return_bounded_errors(self) -> None:
        cfg = mock.Mock(database=Path("/tmp/queue.db"))
        for failure in (
            self.viewer.graph_dispatcher.ActivationUnavailable("account is busy"),
            self.viewer.graph_dispatcher.ClassificationFailure("classification unavailable"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(self.viewer.graph_config, "load_config", return_value=cfg),
                mock.patch.object(self.viewer, "QueueDB"),
                mock.patch.object(self.viewer, "kick_task", side_effect=failure),
            ):
                ok, message = self.viewer.run_task_now("portable-a", "auto")
                self.assertFalse(ok)
                self.assertIn(str(failure), message)
                self.assertLessEqual(len(message), 500)

    def test_headless_chrome_renders_the_frozen_frontend(self) -> None:
        chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if chrome is None:
            self.skipTest("Chrome is unavailable")
        server = self._server()
        host = f"viewer.example.test:{server.server_port}"
        self.viewer.ALLOWED_HOSTS = (host,)
        with (
            mock.patch.object(
                self.viewer, "render_bonus_body",
                return_value='<button class="task-run" data-engine="claude">force</button>',
            ),
            mock.patch.object(self.viewer, "render_schedule_body", return_value="scheduled body"),
            tempfile.TemporaryDirectory() as profile,
        ):
            completed = subprocess.run(
                [
                    chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                    "--disable-dev-shm-usage", "--no-proxy-server",
                    f"--user-data-dir={profile}",
                    "--host-resolver-rules=MAP viewer.example.test 127.0.0.1",
                    "--dump-dom", f"http://{host}/",
                ],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=30, check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("01 bonus-drain", completed.stdout)
        self.assertIn("02 scheduled", completed.stdout)
        self.assertIn('class="task-run"', completed.stdout)
        self.assertNotRegex(completed.stdout.lower(), r"sign in|access secret|authentication required")

    def test_upcoming_rows_render_size_or_unknown_without_annotating_history(self) -> None:
        unexpected = '<img src=x onerror=alert(1)>'
        remaining = [
            {
                "id": "medium-job", "title": "Medium upcoming", "kind": "recurring",
                "priority": 2, "cadence": "weekly", "cwd": "/tmp/medium", "goal": "medium goal",
                "engine_class": "codex-ok", "last_ts": "2026-08-23T12:00:00Z", "size": "medium",
            },
            {
                "id": "legacy-job", "title": "Legacy upcoming", "kind": "oneoff",
                "priority": 2, "cadence": None, "cwd": "/tmp/legacy", "goal": "legacy goal",
                "engine_class": "codex-ok", "last_ts": "2026-08-24T12:00:00Z", "size": None,
            },
            {
                "id": "new-recurring", "title": "New recurring", "kind": "recurring",
                "priority": 2, "cadence": "weekly", "cwd": "/tmp/scheduled", "goal": "new goal",
                "engine_class": "claude-only", "last_ts": None, "size": "small",
            },
            {
                "id": "unexpected-job", "title": "Unexpected upcoming", "kind": "oneoff",
                "priority": 2, "cadence": None, "cwd": "/tmp/unexpected", "goal": "unexpected goal",
                "engine_class": "codex-ok", "last_ts": None, "size": unexpected,
            },
        ]
        history = [{
            "ts": "2026-08-25T12:00:00Z", "task": "historical-job",
            "title": "Historical run", "kind": "oneoff", "status": "done",
            "engine": "codex", "cycle": 123, "summary": "already complete",
            "branch": None, "size": "tiny",
        }]
        disabled = [{
            "id": "disabled-job", "title": "Disabled task", "kind": "oneoff",
            "priority": 3, "cadence": None, "cwd": "/tmp/disabled",
            "goal": "disabled goal", "size": "huge",
        }]
        with (
            mock.patch.object(self.viewer, "get_usage", return_value=None),
            mock.patch.object(self.viewer, "get_codex_usage", return_value=None),
            mock.patch.object(self.viewer, "get_grok_usage", return_value=None),
            mock.patch.object(self.viewer, "current_cycle", return_value=123),
            mock.patch.object(self.viewer, "get_remaining", return_value=remaining),
            mock.patch.object(self.viewer, "get_recent_runs", return_value=history),
            mock.patch.object(self.viewer, "get_counts", return_value={"active": 3, "oneoff_done": 1}),
            mock.patch.object(self.viewer, "get_disabled", return_value=disabled),
            mock.patch.object(self.viewer, "get_inflight", return_value=[]),
            mock.patch.object(self.viewer, "get_gates", return_value={"coordinator": "none"}),
            mock.patch.object(self.viewer, "_claude_cards", return_value=[]),
            mock.patch.object(self.viewer, "_codex_cards", return_value=[]),
            mock.patch.object(self.viewer, "_grok_cards", return_value=[]),
            mock.patch.object(
                self.viewer, "_verdict",
                return_value=("idle", "nothing draining", "idle", ""),
            ),
            mock.patch.object(self.viewer, "_rotation", return_value=""),
            mock.patch.object(self.viewer, "get_dispatch_times", return_value=[]),
            mock.patch.object(
                self.viewer.time, "time",
                return_value=self.viewer._iso_epoch("2026-08-26T12:00:00Z"),
            ),
        ):
            body = self.viewer.render_bonus_body()

        def upcoming_row(title: str) -> str:
            marker = f'<div class="qtitle">{title}</div>'
            position = body.index(marker)
            start = body.rfind('<div class="qrow">', 0, position)
            end = body.find('<div class="qrow">', position)
            if end < 0:
                end = body.find('<div class="sec">', position)
            self.assertGreaterEqual(start, 0)
            self.assertGreater(end, position)
            return body[start:end]

        def metadata(row: str) -> str:
            start = row.index('<div class="qmeta">')
            return row[start:row.index("</div>", start)]

        medium = upcoming_row("Medium upcoming")
        self.assertLess(medium.index('class="qmeta"'), medium.index('class="qsub"'))
        self.assertIn('aria-label="medium size estimate"', medium)
        self.assertEqual(medium.count('class="on"'), 3)
        medium_meta = metadata(medium)
        self.assertNotIn('href="#i-auto"', medium_meta)
        self.assertNotIn('href="#i-claude"', medium_meta)
        self.assertIn('class="qrepo" title="/tmp/medium">medium</span>', medium)
        self.assertIn('href="#i-calendar"', medium)
        self.assertIn('class="qmeta-label">3d</span>', medium)
        self.assertNotIn(">ran 3d ago<", medium)
        self.assertLess(medium_meta.index('href="#i-calendar"'), medium_meta.index('class="qrepo"'))

        legacy = upcoming_row("Legacy upcoming")
        self.assertIn('aria-label="unknown size estimate"', legacy)
        self.assertIn('class="qmeta-label">unknown</span>', legacy)
        self.assertNotIn('href="#i-calendar"', legacy)
        self.assertNotIn("never run", legacy)

        new_recurring = upcoming_row("New recurring")
        self.assertIn('href="#i-calendar"', new_recurring)
        self.assertIn('class="qmeta-label">N/A</span>', new_recurring)
        self.assertIn('aria-label="Recurring · never run"', new_recurring)

        self.assertIn('aria-label="unknown size estimate"', upcoming_row("Unexpected upcoming"))
        self.assertNotIn(unexpected, body)

        run_start = body.index("<span>run log</span>")
        disabled_start = body.index("<span><i class=\"caret\"></i>disabled")
        history_section = body[run_start:disabled_start]
        disabled_section = body[disabled_start:body.index("<footer>", disabled_start)]
        self.assertNotIn("size estimate", history_section)
        self.assertNotIn("size estimate", disabled_section)


if __name__ == "__main__":
    unittest.main()
