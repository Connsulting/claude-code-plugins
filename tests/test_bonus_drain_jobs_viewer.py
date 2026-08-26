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
            "CSS": "ab728f01b90b90bfcdeae4016570cc42f5e794bbfc94bd6b3a9f92301202260c",
            "ICON_SPRITE": "4047ba712de5e325d2254175a247c83237121a30e359c31661efee0bf27e477b",
            "SCRIPT": "c3012ef3517b75f04b0b3b928194cbb9c77765dba7ffa8960f0f1f438af7e324",
            "PAGE": "f59a05ce3b80f6ee47c9d58974777d3076ef7b94e9a10ed32e0dfd81d88d7523",
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


if __name__ == "__main__":
    unittest.main()
