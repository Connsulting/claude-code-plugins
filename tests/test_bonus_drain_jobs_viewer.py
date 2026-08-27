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
from types import SimpleNamespace
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
            "CSS": "612907cebed828f396a0509031694ac47b868262cfe8b321a902358b7e8647d4",
            "ICON_SPRITE": "333ef2163122e3450f95ea008ef6eaaad6ecb8f2562244bfb02e6317a4c06a03",
            "SCRIPT": "b0ced2b4ca8e3b4852a4362ca815da8ba1769566f5522f309cf3cd8691a7b35d",
            "PAGE": "69071b11da43869de11c5211fe5fed4a976f38f6499c4b70ce9205845b4033de",
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

    def test_active_stripes_and_dispatching_bar_shimmer_have_distinct_meanings(self) -> None:
        """A gold stripe is active; a shimmering bar is an actual dispatch."""
        claude = self.viewer._claude_cards(
            {"acct": [{"label": "Business", "u7": 22}], "active": "Business"}, None, 0, "", 0,
        )[0]
        codex = self.viewer._codex_cards(
            {"codex_acct": [{"label": "Personal", "u7": 22}], "codex_active": "Personal"}, None, 0, "", 0,
        )[0]
        grok = self.viewer._grok_cards({}, {"weekly_percent": 22}, 0, "", 0)[0]

        for card in (claude, codex, grok):
            with self.subTest(account=card["name"]):
                self.assertTrue(card["active"])
                row = self.viewer._account_row(card)
                self.assertIn('<i class="stripe on"></i>', row)
                self.assertIn('bar lg idle', row)

        window_draining = self.viewer._grok_cards(
            {}, {"weekly_percent": 22}, 0, "grok", 0,
        )[0]
        self.assertIn('bar lg draining', self.viewer._account_row(window_draining))

        draining = self.viewer._claude_cards(
            {"acct": [{"label": "Business", "u7": 22}], "active": "Business", "selected": "Business"},
            None, 0, "claude", 1,
        )[0]
        self.assertIn('bar lg draining', self.viewer._account_row(draining))
        self.assertIn('@keyframes drainshimmer', self.viewer.CSS)

    def test_live_grok_ledger_batch_shimmers_and_drives_the_verdict(self) -> None:
        grok = self.viewer._grok_cards(
            {}, {"weekly_percent": 50, "weekly_reset": 2_000_000_000}, 4, "grok", 2,
        )[0]
        self.assertIn('bar lg draining', self.viewer._account_row(grok))
        tone, label, text, _sub = self.viewer._verdict([grok], "grok", 30 * 3600)
        self.assertEqual((tone, label), ("live", "draining"))
        self.assertIn("Grok", text)

    def test_remaining_and_force_buttons_use_graph_provider_eligibility(self) -> None:
        pick = [{"id": "claude-codex", "title": "Claude and Codex", "kind": "oneoff",
                 "priority": 2, "cwd": "/tmp", "goal": "test",
                 "eligible_providers": ["claude", "codex"]}]
        with (
            mock.patch.object(self.viewer, "_remaining_snapshot", return_value=pick),
            mock.patch.object(self.viewer, "_last_runs", return_value={}),
        ):
            remaining = self.viewer.get_remaining(123)
        self.assertEqual(remaining[0]["eligible_providers"], ["claude", "codex"])
        buttons = self.viewer._run_buttons(remaining[0])
        self.assertIn('data-engine="claude"', buttons)
        self.assertIn('data-engine="codex"', buttons)
        self.assertIn('data-engine="grok"', buttons)
        grok = buttons[buttons.index('data-engine="grok"'):]
        self.assertIn('disabled title="This task is not eligible for Grok"', grok)

    def test_gates_uses_the_current_json_cli_protocol(self) -> None:
        self.viewer._gates_cache.update(t=0.0, key=None, val=None)
        response = SimpleNamespace(stdout=(
            "lead_hours=30\n"
            '{"gates":[{"provider_id":"grok","open":true,"batch_size":6,'
            '"resets_at":2000000000}]}\n'
        ))
        with mock.patch.object(self.viewer.subprocess, "run", return_value=response) as run:
            gates = self.viewer.get_gates(4, 4, 4, None, None)
        self.assertIn("gates --json", run.call_args.args[0][2])
        self.assertEqual(gates["grok_batch"], 6)
        self.assertEqual(gates["coordinator"], "grok")

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
            start = body.rfind('<div class="qrow"', 0, position)
            end = body.find('<div class="qrow"', position)
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


    QUEUE_FIXTURE = [
        {
            "id": "weekly-claude", "title": "Weekly Claude sweep", "kind": "recurring",
            "priority": 1, "cadence": "weekly", "cwd": "/tmp/one", "goal": "sweep",
            "size": "small", "last_ts": None, "eligible_providers": ["claude"],
        },
        {
            "id": "oneoff-codex", "title": "One-off Codex refactor", "kind": "oneoff",
            "priority": 2, "cadence": None, "cwd": "/tmp/two", "goal": "refactor",
            "size": "large", "last_ts": None, "eligible_providers": ["codex", "grok"],
        },
        {
            "id": "oneoff-claude", "title": "One-off Claude audit", "kind": "oneoff",
            "priority": 2, "cadence": None, "cwd": "/tmp/three", "goal": "audit",
            "size": "small", "last_ts": None, "eligible_providers": ["claude", "codex"],
        },
    ]

    def _bonus_body(self, remaining):
        with (
            mock.patch.object(self.viewer, "get_usage", return_value=None),
            mock.patch.object(self.viewer, "get_codex_usage", return_value=None),
            mock.patch.object(self.viewer, "get_grok_usage", return_value=None),
            mock.patch.object(self.viewer, "current_cycle", return_value=123),
            mock.patch.object(self.viewer, "get_remaining", return_value=remaining),
            mock.patch.object(self.viewer, "get_recent_runs", return_value=[]),
            mock.patch.object(self.viewer, "get_disabled", return_value=[]),
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
        ):
            return self.viewer.render_bonus_body()

    def test_queue_filter_chips_cover_the_facets_present_in_the_queue(self) -> None:
        body = self._bonus_body(self.QUEUE_FIXTURE)
        bar = body[body.index('id="qfilters"'):body.index('id="qlist"')]

        for group, label in (
            ("kind", "type"), ("provider", "supported by"),
            ("priority", "priority"), ("size", "size"),
        ):
            with self.subTest(group=group):
                self.assertIn(f'role="group" aria-label="filter by {label}"', bar)
                self.assertIn(f'class="fchip fall on" data-group="{group}" data-value=""', bar)

        # Every chip but priority is its glyph plus a count, and the words that would have
        # labelled it survive as the title and the accessible name.
        self.assertIn(
            'data-value="oneoff" aria-pressed="false" aria-label="one-off" title="one-off">'
            + self.viewer.ico("arrow") + "<i>2</i>", bar,
        )
        self.assertIn(
            'data-value="recurring" aria-pressed="false" aria-label="recurring" '
            'title="recurring">' + self.viewer.ico("cycle") + "<i>1</i>", bar,
        )
        # A provider row counts once per provider it supports, not once per row.
        for provider, count in (("claude", 2), ("codex", 2), ("grok", 1)):
            with self.subTest(provider=provider):
                self.assertIn(
                    f'data-value="{provider}" aria-pressed="false" '
                    f'aria-label="{provider.title()}" title="{provider.title()}">'
                    + self.viewer.ico(provider) + f"<i>{count}</i>", bar,
                )
        self.assertIn('aria-label="priority 1" title="priority 1">P1<i>1</i>', bar)
        self.assertIn('aria-label="priority 2" title="priority 2">P2<i>2</i>', bar)
        # The size chip is the same five-block gauge the rows carry, so the filter and the row
        # it filters cannot drift apart.
        self.assertIn(
            'data-value="small" aria-pressed="false" aria-label="small size" '
            'title="small size">' + self.viewer._size_blocks("small") + "<i>2</i>", bar,
        )
        self.assertIn(
            'data-value="large" aria-pressed="false" aria-label="large size" '
            'title="large size">' + self.viewer._size_blocks("large") + "<i>1</i>", bar,
        )
        self.assertIn(self.viewer._size_blocks("small"), self.viewer.size_badge("small"))

        # Nothing in the bar spells a facet out, so the row stays one line.
        for word in ("one-off<", "recurring<", "Claude<", "Codex<", "Grok<", "small<", "large<"):
            self.assertNotIn(word, bar)

        # Every row carries the facets the chips filter on.
        self.assertIn('data-kind="oneoff" data-priority="2"', body)
        self.assertIn('data-size="large" data-providers="codex grok"', body)
        self.assertIn('data-size="small" data-providers="claude codex"', body)

    def test_single_value_facets_and_an_empty_queue_render_no_dead_controls(self) -> None:
        uniform = [dict(t, priority=2, kind="oneoff", cadence=None, size="small",
                        eligible_providers=["claude"]) for t in self.QUEUE_FIXTURE]
        # Nothing here can be filtered, so the bar itself is dropped rather than rendered as
        # four controls that cannot change the list.
        uniform_body = self._bonus_body(uniform)
        self.assertNotIn('id="qfilters"', uniform_body)
        self.assertIn('id="qlist"', uniform_body)

        # One facet varying is enough to earn the bar, and only that facet gets a group.
        mixed = [dict(t, size=size) for t, size in zip(uniform, ("small", "large", "large"))]
        bar = self._bonus_body(mixed)
        self.assertIn('id="qfilters"', bar)
        self.assertIn('aria-label="filter by size"', bar)
        for label in ("type", "supported by", "priority"):
            self.assertNotIn(f'aria-label="filter by {label}"', bar)

        drained = self._bonus_body([])
        self.assertNotIn('id="qfilters"', drained)
        self.assertIn("queue drained", drained)

    def test_selecting_filters_hides_rows_and_renumbers_the_queue_in_a_browser(self) -> None:
        chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if chrome is None:
            self.skipTest("Chrome is unavailable")
        with (
            mock.patch.object(self.viewer, "render_bonus_body",
                              return_value=self._bonus_body(self.QUEUE_FIXTURE)),
            mock.patch.object(self.viewer, "render_schedule_body", return_value="scheduled body"),
        ):
            page = self.viewer.render_page().decode()

        def dump(clicks: str) -> str:
            script = "".join(
                f"document.querySelector('.fchip[data-group=\"{g}\"]"
                f"[data-value=\"{v}\"]').click();"
                for g, v in clicks
            )
            with tempfile.TemporaryDirectory() as work:
                target = Path(work) / "page.html"
                target.write_text(page + f"<script>{script}</script>")
                completed = subprocess.run(
                    [
                        chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                        "--disable-dev-shm-usage", "--no-proxy-server",
                        f"--user-data-dir={work}/profile", "--dump-dom", target.as_uri(),
                    ],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=60, check=False,
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return completed.stdout

        def row(dom: str, title: str) -> str:
            position = dom.index(f'<div class="qtitle">{title}</div>')
            return dom[dom.rfind('<div class="qrow"', 0, position):position]

        unfiltered = dump([])
        self.assertNotIn("hidden", row(unfiltered, "One-off Codex refactor"))
        self.assertIn(">3 jobs<", unfiltered)

        # One group: the two one-offs survive, the recurring row goes, and the surviving rows
        # renumber from 1 so the drain order never shows a gap.
        kind_only = dump([("kind", "oneoff")])
        self.assertIn("hidden", row(kind_only, "Weekly Claude sweep"))
        self.assertNotIn("hidden", row(kind_only, "One-off Codex refactor"))
        self.assertIn(">2 of 3 jobs<", kind_only)
        self.assertIn('<span class="qn">1</span>', row(kind_only, "One-off Codex refactor"))
        self.assertIn('<span class="qn">2</span>', row(kind_only, "One-off Claude audit"))
        self.assertIn("2 tasks", kind_only)
        self.assertNotIn("hidden", kind_only[kind_only.index('data-band="2"') - 40:][:60])

        # Across groups the filters intersect: one-off AND Claude-eligible AND small.
        crossed = dump([("kind", "oneoff"), ("provider", "claude"), ("size", "small")])
        self.assertIn("hidden", row(crossed, "One-off Codex refactor"))
        self.assertNotIn("hidden", row(crossed, "One-off Claude audit"))
        self.assertIn(">1 of 3 jobs<", crossed)
        self.assertIn('id="qfreset"', crossed)
        self.assertNotIn('id="qfreset" hidden', crossed)

        # Within a group they union, and an empty band header is dropped with its count.
        union = dump([("provider", "codex"), ("provider", "grok")])
        self.assertIn("hidden", row(union, "Weekly Claude sweep"))
        self.assertIn(">2 of 3 jobs<", union)

        # A selection that matches nothing says so rather than looking like a drained queue.
        nothing = dump([("kind", "recurring"), ("size", "large")])
        self.assertIn("no remaining jobs match these filters", nothing)
        self.assertNotIn('id="qnone" hidden', nothing)
        self.assertIn(">0 of 3 jobs<", nothing)


if __name__ == "__main__":
    unittest.main()
