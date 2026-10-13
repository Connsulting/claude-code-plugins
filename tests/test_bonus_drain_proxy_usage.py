"""Proxy HTTP boundary, sanitized cache, provider pools and forward migration."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.readiness_fixture import reviewed

SKILL = Path(__file__).resolve().parents[1] / "plugins/bonus-drain/skills/bonus-drain"
sys.path.insert(0, str(SKILL))
from bonus_drain import config, db, planner, proxy_usage, scout, usage


class ProxyUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = int(time.time())
        self.requests = []
        self.files = [
            {"provider": "anthropic", "auth_index": "a", "access_token": "private-provider-token"},
            {"provider": "claude", "auth_index": "b"},
            {"provider": "openai", "auth_index": "c"},
            {"provider": "xai", "auth_index": "d"},
            {"provider": "meta", "auth_index": "e"},
            {"provider": "claude", "auth_index": "off", "disabled": True},
        ]
        self.bodies = {
            "a": {"five_hour": {"utilization": 10, "resets_at": self.now + 18000},
                  "seven_day": {"utilization": 20, "resets_at": self.now + 7200},
                  "seven_day_sonnet": {"utilization": 30, "resets_at": self.now + 7200}},
            "b": {"five_hour": {"utilization": 99, "resets_at": self.now + 18000},
                  "seven_day": {"utilization": 99, "resets_at": self.now + 3600}},
            "c": {"rate_limit": {
                "primary_window": {"used_percent": 4, "reset_at": self.now + 18000,
                                   "limit_window_seconds": 18000},
                "secondary_window": {"used_percent": 12, "reset_at": self.now + 10800,
                                     "limit_window_seconds": 604800}}},
        }
        self.failure = set()
        self.redirect = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                owner.requests.append(("GET", self.path, None))
                if self.headers.get("Authorization") != "Bearer fixture-management-credential":
                    self.send_error(401)
                    return
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", "/must-not-follow")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"files": owner.files}).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, body))
                self.send_response(200)
                self.end_headers()
                index = body["auth_index"]
                response = {"status_code": 401 if index in owner.failure else 200,
                            "body": "private-provider-token" if index in owner.failure
                            else json.dumps(owner.bodies[index])}
                self.wfile.write(json.dumps(response).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        dotenv = self.root / "proxy.env"
        dotenv.write_text(f'# fixture\nCPA_BASE_URL="http://127.0.0.1:{self.server.server_port}"\nCPA_MGMT_KEY=fixture-management-credential\n')
        dotenv.chmod(0o600)
        self.raw = {
            "schema_version": 1, "database": str(self.root / "queue.db"),
            "cache_dir": str(self.root / "cache"), "record_command": ["/bin/true"],
            "secret_refs": [{"id": "proxy", "source": "file", "path": str(dotenv)}],
            "proxy_secret_ref": "proxy", "host_load_gate": {"enabled": False},
            "adapters": [{"id": "router", "kind": "t3-thread", "argv": ["/bin/true"]}],
            "providers": [{"id": name, "dispatch": {"adapter_id": "router", "provider": name}}
                          for name in ("claude", "codex", "xai", "meta")],
            "limits": [{"id": name + "-weekly", "provider_id": name,
                        "window_seconds": 604800, "ceiling_percent": 98,
                        "lead_seconds": 604800, "batch_size": 3}
                       for name in ("claude", "codex")],
        }
        self.config = config.validate_config(self.raw)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.raw))

    def cli(self, command, *extra):
        return subprocess.run([str(SKILL / "bin/bonus-drain"), command,
                               "--config", str(self.config_path), "--json", *extra],
                              capture_output=True, text=True, check=False)

    def test_refresh_cli_and_plan_cli_share_proxy_cache(self):
        refresh = self.cli("refresh")
        self.assertEqual(refresh.returncode, 0, refresh.stderr)
        payload = json.loads(refresh.stdout)
        accounts = {a["account_id"]: a for a in payload["accounts"]}
        self.assertEqual(set(accounts), {"claude-a", "claude-b", "codex-c", "xai-d", "meta-e"})
        self.assertEqual(set(accounts["claude-a"]["limits"]), {"five_hour", "seven_day", "seven_day_sonnet"})
        self.assertEqual(accounts["codex-c"]["limits"]["seven_day"]["used_percent"], 12)
        self.assertEqual(set(accounts["codex-c"]["limits"]), {"five_hour", "seven_day"})
        queue = db.QueueDB(self.config.database)
        for index in range(8):
            queue.add_task(reviewed({"id": str(index), "title": "fixture", "kind": "oneoff",
                                    "priority": 2, "cwd": str(self.root), "goal": "fixture"}))
        count = len(self.requests)
        plan = self.cli("plan")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        planned = json.loads(plan.stdout)
        self.assertEqual(len(self.requests), count)
        self.assertEqual([b["provider_id"] for b in planned["batches"]], ["claude", "codex", "meta", "xai"])
        self.assertEqual(planned["batches"][0]["resets_at"], self.now + 7200)
        self.assertEqual(len(planned["accounts"]), 5)
        self.assertIsNone(planned["batches"][-1]["resets_at"])
        self.assertTrue(planned["batches"][-1]["unmetered"])
        self.assertTrue(accounts["xai-d"]["unmetered"])
        for path in self.config.usage_cache_dir.rglob("*.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text()
            self.assertNotIn("fixture-management-credential", text)
            self.assertNotIn("private-provider-token", text)
            self.assertNotIn("Authorization", text)

    def test_only_metered_accounts_call_allowed_endpoints_with_fixed_headers(self):
        usage.refresh_all(self.config, now_epoch=self.now)
        self.assertEqual(self.requests[0], ("GET", "/v0/management/auth-files", None))
        calls = [body for method, path, body in self.requests if method == "POST"]
        self.assertEqual([body["auth_index"] for body in calls], ["a", "b", "c"])
        for call in calls:
            self.assertEqual(call["method"], "GET")
            self.assertEqual(call["header"]["Authorization"], "Bearer $TOKEN$")
        self.assertEqual(calls[0]["url"], proxy_usage.ANTHROPIC_URL)
        self.assertEqual(calls[0]["header"]["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(calls[0]["header"]["User-Agent"], "claude-cli/2.1.280 (external, cli)")
        self.assertEqual(calls[2]["url"], proxy_usage.CODEX_URL)

    def test_api_call_rejects_every_nonexact_target_before_network(self):
        reader = proxy_usage.ProxyReader(self.config)
        for url in ("https://evil.invalid", proxy_usage.CODEX_URL + "?x=1", proxy_usage.ANTHROPIC_URL + "/"):
            with self.assertRaises(usage.UsageError):
                reader.api_call("a", url)
        self.assertEqual(self.requests, [])

    def test_management_redirect_is_not_followed(self):
        self.redirect = True
        with self.assertRaises(usage.UsageError):
            usage.refresh_all(self.config)
        self.assertEqual(len(self.requests), 1)

    def test_failed_account_closes_only_it_and_does_not_echo_response(self):
        self.failure.add("a")
        result = self.cli("refresh")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("private-provider-token", result.stdout + result.stderr)
        self.assertNotIn("fixture-management-credential", result.stdout + result.stderr)
        records = usage.read_all(self.config)
        self.assertFalse(records[("claude", "claude-a")].fresh)
        self.assertTrue(records[("codex", "codex-c")].fresh)

    def test_refresh_removes_disabled_and_deleted_inventory_accounts(self):
        usage.refresh_all(self.config)
        self.files = [dict(row, disabled=True) if row["auth_index"] == "a" else row
                      for row in self.files if row["auth_index"] != "c"]
        usage.refresh_all(self.config)
        ids = {account.id for account in usage.read_accounts(self.config)}
        self.assertNotIn("claude-a", ids)
        self.assertNotIn("codex-c", ids)

    def test_unknown_configured_provider_is_unmetered_without_api_call(self):
        raw = dict(self.raw, providers=[*self.raw["providers"], {"id": "future", "dispatch": {"adapter_id": "router", "provider": "future"}}])
        self.files.append({"provider": "future", "auth_index": "f"})
        records = usage.refresh_all(config.validate_config(raw))
        self.assertTrue(records[("future", "future-f")].unmetered)
        self.assertEqual(len([request for request in self.requests if request[0] == "POST"]), 3)

    def test_unmetered_provider_cap_counts_all_accounts_and_running_claims(self):
        self.files.append({"provider": "xai", "auth_index": "d2"})
        usage.refresh_all(self.config)
        queue = db.QueueDB(self.config.database)
        for index in range(4):
            queue.add_task(reviewed({"id": str(index), "title": "fixture", "kind": "oneoff",
                                    "priority": 2, "cwd": str(self.root), "goal": "fixture",
                                    "allowed_providers": ["xai"]}))
        queue.claim("0", "xai-d2/unmetered/123", "xai", "xai-d2", now_epoch=self.now)
        tick = scout.plan_tick(self.config, queue, now_epoch=self.now)
        self.assertEqual([(b.provider_id, b.batch_size) for b in tick.plan.batches], [("xai", 1)])
        queue.claim("1", "xai-d/unmetered/123", "xai", "xai-d", now_epoch=self.now)
        self.assertEqual(scout.plan_tick(self.config, queue, now_epoch=self.now).plan.batches, ())
        raised_cap = replace(self.config, unmetered_max_inflight=3)
        self.assertEqual(scout.plan_tick(raised_cap, queue, now_epoch=self.now).plan.batches[0].batch_size, 1)

    def test_invalid_and_stale_readings_fail_closed(self):
        self.bodies["a"]["seven_day"]["utilization"] = None
        records = usage.refresh_all(self.config, now_epoch=self.now)
        self.assertFalse(records[("claude", "claude-a")].fresh)
        records = usage.read_all(self.config, now_epoch=self.now + 4000)
        self.assertFalse(any(record.fresh for record in records.values()))
        self.assertEqual(planner.build_plan(self.config, records, eligible_count=5, now_epoch=self.now + 4000).batches, ())

    def test_claude_ignores_null_model_windows(self):
        body = {"five_hour": {"utilization": 1, "resets_at": self.now + 60},
                "seven_day": {"utilization": 2, "resets_at": self.now + 120},
                "seven_day_opus": None, "seven_day_breakdown": {"utilization": None}}
        self.assertEqual(set(proxy_usage.parse_windows("claude", body)), {"five_hour", "seven_day"})

    def test_codex_single_weekly_window_is_retained(self):
        body = {"rate_limit": {"primary_window": {"used_percent": 7, "reset_at": self.now + 86400,
                                                   "limit_window_seconds": 604800}}}
        self.assertEqual(set(proxy_usage.parse_windows("codex", body)), {"seven_day"})

    def test_config_requires_file_secret_and_rejects_old_graph_and_adapter_kinds(self):
        for key in ("plans", "accounts"):
            with self.assertRaises(config.ConfigError):
                config.validate_config(dict(self.raw, **{key: []}))
        for kind in ("usage", "reset", "activation"):
            raw = dict(self.raw, adapters=[{"id": "router", "kind": kind, "argv": ["/bin/true"]}])
            with self.assertRaises(config.ConfigError):
                config.validate_config(raw)
        raw = dict(self.raw, secret_refs=[{"id": "proxy", "source": "env", "name": "TEST_PROXY"}])
        with self.assertRaises(config.ConfigError):
            config.validate_config(raw)
        self.assertEqual(self.config.unmetered_max_inflight, 2)

    def test_forward_migration_drops_old_table_and_rebinds_backoff_cause(self):
        queue = db.QueueDB(self.config.database)
        queue.initialize()
        with sqlite3.connect(queue.path) as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=5")
            connection.execute("CREATE TABLE activation_leases (task_id TEXT)")
            connection.execute("DROP TABLE account_backoff")
            connection.execute("""CREATE TABLE account_backoff (
                provider_id TEXT, account_id TEXT, cause TEXT CHECK (cause IN ('activation_unswitched','codex_daemon_start')),
                failures INTEGER, not_before TEXT, detail TEXT, updated_at TEXT, PRIMARY KEY(provider_id,account_id))""")
            connection.execute("INSERT INTO account_backoff VALUES ('claude','claude-a','activation_unswitched',2,'2033-05-18T03:33:20Z','retained','now')")
        queue.initialize()
        queue.initialize()
        with sqlite3.connect(queue.path) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='activation_leases'").fetchone())
            self.assertEqual(connection.execute("SELECT cause,failures FROM account_backoff").fetchone(), ("account_unavailable", 2))
