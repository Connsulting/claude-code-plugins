"""Proxy HTTP boundary, sanitized cache, provider pools and forward migration."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

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
        sleep = mock.patch.object(proxy_usage.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        self.requests = []
        self.files = [
            {"provider": "anthropic", "auth_index": "a", "access_token": "private-provider-token"},
            {"provider": "claude", "auth_index": "b"},
            {"provider": "openai", "auth_index": "c"},
            {"provider": "xai", "auth_index": "d"},
            {"provider": "meta", "auth_index": "e", "name": "meta account+e.json"},
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
        self.bodies["d"] = {"config": {
            "creditUsagePercent": 3.0, "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": datetime.fromtimestamp(self.now - 604800, timezone.utc).isoformat(timespec="milliseconds"),
                "end": datetime.fromtimestamp(self.now + 14400, timezone.utc).isoformat(timespec="milliseconds"),
            },
        }}
        self.bodies["e"] = {
            "api_key": "private-muse-api-key", "email": "private-muse-personal-field",
            "subs_usage": {
                "window": {"used_percent": 8.0, "window_duration_mins": 300, "resets_at": self.now + 18000},
                "weekly": {"used_percent": 9.0, "resets_at": self.now + 18000},
            },
        }
        self.muse_auth = {"dca_token": "dca:private-muse-token", "other": "private-auth-field"}
        self.fail_once = set()
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
                response = (owner.muse_auth if self.path.startswith("/v0/management/auth-files/download?")
                            else {"files": owner.files})
                self.wfile.write(json.dumps(response).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, body))
                self.send_response(200)
                self.end_headers()
                index = body["auth_index"]
                failed = index in owner.failure or index in owner.fail_once
                owner.fail_once.discard(index)
                response = {"status_code": 401 if failed else 200,
                            "body": "private-provider-token" if failed
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
            "providers": [{"id": name, "max_size": {"claude": "huge", "codex": "large", "grok": "small", "muse": "medium"}[name], "dispatch": {"adapter_id": "router", "provider": name}}
                          for name in ("claude", "codex", "grok", "muse")],
            "limits": [{"id": name + "-weekly", "provider_id": name,
                        "window_seconds": 604800, "ceiling_percent": 98,
                        "lead_seconds": 604800, "batch_size": 3}
                       for name in ("claude", "codex", "grok", "muse")],
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
        self.assertEqual(set(accounts), {"claude-a", "claude-b", "codex-c", "grok-d", "muse-e"})
        self.assertEqual(set(accounts["claude-a"]["limits"]), {"five_hour", "seven_day", "seven_day_sonnet"})
        self.assertEqual(accounts["codex-c"]["limits"]["seven_day"]["used_percent"], 12)
        self.assertEqual(set(accounts["codex-c"]["limits"]), {"five_hour", "seven_day"})
        queue = db.QueueDB(self.config.database)
        for index in range(8):
            queue.add_task(reviewed({"id": str(index), "title": "fixture", "kind": "oneoff",
                                    "priority": 2, "cwd": str(self.root), "goal": "fixture", "size": "small"}))
        count = len(self.requests)
        plan = self.cli("plan")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        planned = json.loads(plan.stdout)
        self.assertEqual(len(self.requests), count)
        self.assertEqual([b["provider_id"] for b in planned["batches"]], ["claude", "codex", "grok", "muse"])
        self.assertEqual(planned["batches"][0]["resets_at"], self.now + 3600)
        self.assertEqual(len(planned["subscriptions"]), 5)
        self.assertNotIn("accounts", planned)
        for provider, percent in (("grok", 3), ("muse", 9)):
            gate = next(g for g in planned["gates"] if g["provider_id"] == provider)
            self.assertFalse(gate["unmetered"])
            self.assertGreater(gate["surplus"], 0)
            self.assertEqual(gate["limit_ids"], [provider + "-weekly"])
            account = accounts[provider + ("-d" if provider == "grok" else "-e")]
            self.assertFalse(account["unmetered"])
            self.assertEqual(account["limits"]["seven_day"]["used_percent"], percent)
        for path in self.config.usage_cache_dir.rglob("*.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text()
            self.assertNotIn("fixture-management-credential", text)
            self.assertNotIn("private-provider-token", text)
            self.assertNotIn("Authorization", text)
            for sensitive in ("dca:private-muse-token", "private-muse-api-key",
                              "private-muse-personal-field", "private-auth-field"):
                self.assertNotIn(sensitive, text)

    def test_all_four_providers_call_allowed_endpoints_with_fixed_headers(self):
        usage.refresh_all(self.config, now_epoch=self.now)
        self.assertEqual(self.requests[0], ("GET", "/v0/management/auth-files", None))
        calls = [body for method, path, body in self.requests if method == "POST"]
        self.assertEqual([body["auth_index"] for body in calls], ["a", "b", "c", "d", "e"])
        for call in calls[:-1]:
            self.assertEqual(call["method"], "GET")
            self.assertEqual(call["header"]["Authorization"], "Bearer $TOKEN$")
        self.assertEqual(calls[0]["url"], proxy_usage.ANTHROPIC_URL)
        self.assertEqual(calls[0]["header"]["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(calls[0]["header"]["User-Agent"], "claude-cli/2.1.280 (external, cli)")
        self.assertEqual(calls[2]["url"], proxy_usage.CODEX_URL)
        self.assertEqual(calls[3]["url"], proxy_usage.GROK_URL)
        self.assertEqual(calls[3]["header"]["x-xai-token-auth"], "xai-grok-cli")
        self.assertEqual(calls[3]["header"]["x-grok-client-version"], "1.0.46")
        self.assertIn(("GET", "/v0/management/auth-files/download?name=meta%20account%2Be.json", None), self.requests)
        self.assertEqual(calls[4], {
            "auth_index": "e", "method": "POST", "url": proxy_usage.MUSE_URL,
            "header": {"Authorization": "Bearer dca:private-muse-token", **proxy_usage.TARGETS[proxy_usage.MUSE_URL]},
            "data": "{}",
        })

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

    def test_failed_subscription_closes_whole_pool_and_does_not_echo_response(self):
        self.failure.add("a")
        result = self.cli("refresh")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("private-provider-token", result.stdout + result.stderr)
        self.assertNotIn("fixture-management-credential", result.stdout + result.stderr)
        records = usage.read_all(self.config)
        self.assertFalse(records[("claude", "claude-a")].fresh)
        self.assertTrue(records[("codex", "codex-c")].fresh)
        pool = planner.build_plan(self.config, records, eligible_count=10, now_epoch=self.now)
        self.assertFalse(any(batch.provider_id == "claude" for batch in pool.batches))

    def test_refresh_removes_disabled_and_deleted_inventory_accounts(self):
        usage.refresh_all(self.config)
        self.files = [dict(row, disabled=True) if row["auth_index"] == "a" else row
                      for row in self.files if row["auth_index"] != "c"]
        usage.refresh_all(self.config)
        ids = {account.id for account in usage.read_accounts(self.config)}
        self.assertNotIn("claude-a", ids)
        self.assertNotIn("codex-c", ids)

    def test_unknown_configured_provider_is_unmetered_without_api_call(self):
        raw = dict(self.raw, providers=[*self.raw["providers"], {"id": "future", "max_size": "huge", "dispatch": {"adapter_id": "router", "provider": "future"}}])
        self.files.append({"provider": "future", "auth_index": "f"})
        records = usage.refresh_all(config.validate_config(raw))
        self.assertTrue(records[("future", "future-f")].unmetered)
        self.assertEqual(len([request for request in self.requests if request[0] == "POST"]), 5)

    def test_proxy_aliases_map_to_configured_grok_and_muse_providers(self):
        self.files.append({"provider": "grok", "auth_index": "g"})
        self.bodies["g"] = self.bodies["d"]
        records = usage.refresh_all(self.config, now_epoch=self.now)
        self.assertIn(("grok", "grok-d"), records)
        self.assertIn(("grok", "grok-g"), records)
        self.assertIn(("muse", "muse-e"), records)
        self.assertFalse(any(provider in {"xai", "meta"} for provider, _account in records))
        self.assertFalse(records[("grok", "grok-d")].unmetered)
        self.assertFalse(records[("muse", "muse-e")].unmetered)

    def test_viewer_renders_all_six_subscriptions_and_api_returns_same_inventory(self):
        from tests.test_bonus_drain_jobs_viewer import (
            JobsViewerContractTests,
            _load_server,
        )
        self.files.append({"provider": "openai", "auth_index": "c2"})
        self.bodies["c2"] = self.bodies["c"]
        usage.refresh_all(self.config, now_epoch=self.now)
        db.QueueDB(self.config.database).initialize()
        viewer = _load_server()
        viewer.BONUS_SKILL_DIR = SKILL
        viewer.BONUSDB_SH = SKILL / "bonusdb.sh"
        viewer.DB_PATH = self.config.database
        server = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
        authority = "fixture-viewer.example"
        origin = f"https://{authority}"
        self.config_path.write_text(json.dumps(dict(self.raw, viewer={
            "bind": "127.0.0.1", "remote": {"trusted_loopback_proxy": True,
                "allowed_hosts": [authority], "allowed_origins": [origin]},
        })))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with (mock.patch.dict(os.environ, {"BONUS_DRAIN_CONFIG": str(self.config_path),
                                          "BONUS_DRAIN_BIN": str(SKILL / "bin/bonus-drain"),
                                          "BONUS_SKILL_DIR": str(SKILL)}),
              mock.patch.object(viewer, "list_timers", return_value=[])):
            viewer.configure_request_boundary()
            body = viewer.render_bonus_body()
            rejected, _headers, _payload = JobsViewerContractTests._request(
                server, "GET", "/api/bonus", headers={"Host": "other.example.test"},
            )
            self.assertEqual(rejected, 403)
            status, _headers, payload = JobsViewerContractTests._request(
                server, "GET", "/api/bonus", headers={"Host": authority, "Origin": origin},
            )
        self.assertEqual(status, 200)
        inventory = json.loads(payload)["usage"]
        self.assertIsInstance(inventory, list)
        self.assertEqual(sum(row["provider_id"] == "claude" for row in inventory), 2)
        self.assertEqual(sum(row["provider_id"] == "codex" for row in inventory), 2)
        unmetered = {row["provider_id"] for row in inventory if row["unmetered"]}
        self.assertEqual(unmetered, set())
        cards = re.findall(r'<details class="arow">.*?</details>', body, re.DOTALL)
        self.assertEqual(len(cards), 6)
        self.assertEqual(sum("Claude ·" in card for card in cards), 2)
        self.assertEqual(sum("Codex ·" in card for card in cards), 2)
        self.assertTrue(any("Grok" in card and "<b>3%</b>" in card for card in cards))
        self.assertTrue(any("Muse" in card and "<b>9%</b>" in card for card in cards))

    def test_max_size_is_required_and_rejects_unknown_values(self):
        for value in (None, "unknown", "gigantic", 2, True):
            with self.subTest(value=value):
                providers = [dict(item) for item in self.raw["providers"]]
                if value is None:
                    providers[0].pop("max_size")
                else:
                    providers[0]["max_size"] = value
                with self.assertRaises(config.ConfigError):
                    config.validate_config(dict(self.raw, providers=providers))

    def test_manual_dispatch_rejects_account_selection_but_inventory_filter_remains(self):
        usage.refresh_all(self.config, now_epoch=self.now)
        for command in ("dispatch", "run-now"):
            with self.subTest(command=command):
                result = self.cli(command, "any-task", "claude", "--account", "claude-a")
                self.assertEqual(result.returncode, 2)
                self.assertIn("unrecognized arguments", result.stderr)
        inventory = self.cli("usage", "--provider", "grok", "--account", "grok-d")
        self.assertEqual(inventory.returncode, 0, inventory.stderr)
        self.assertEqual([row["account_id"] for row in json.loads(inventory.stdout)["accounts"]], ["grok-d"])

    def test_terminal_record_retains_historical_account_ids_after_config_migration(self):
        queue = db.QueueDB(self.config.database)
        receipts = (("claude", "claude-personal"), ("codex", "codex-business"),
                    ("grok", "grok-personal"), ("claude", "claude-7ad913"))
        before = []
        for index, (provider, account) in enumerate(receipts):
            task_id = f"historical-{index}"
            queue.add_task(reviewed({"id": task_id, "title": task_id, "kind": "oneoff",
                                    "priority": 2, "cwd": str(self.root), "goal": "fixture",
                                    "size": "small"}))
            key = f"{account}/weekly/{self.now + 1000}"
            attempt = queue.claim(task_id, key, provider, account,
                                  provider_max_size="huge", now_epoch=self.now)
            self.assertIsNotNone(attempt)
            queue.record(task_id, key, attempt_id=attempt.id, status="dispatched",
                         provider_id=provider, account_id=account, router_job_id=f"old-job-{index}")
            before.append((task_id, key, provider, account, attempt.id))
        usage.refresh_all(self.config, now_epoch=self.now)
        queue.initialize()
        for task_id, key, provider, account, attempt_id in before:
            with self.subTest(account=account):
                claim = queue.claim_for(task_id, key)
                self.assertEqual((claim.provider_id, claim.account_id, claim.attempt_id),
                                 (provider, account, attempt_id))
                result = self.cli("record", "--task", task_id, "--status", "failed",
                                  "--eligibility-key", key, "--provider-id", provider,
                                  "--account-id", account, "--attempt-id", attempt_id,
                                  "--summary", "historical worker terminal")
                self.assertEqual(result.returncode, 0, result.stderr)
                row = json.loads(result.stdout)["run"]
                self.assertEqual((row["provider_id"], row["account_id"], row["attempt_id"]),
                                 (provider, account, attempt_id))
                self.assertIsNone(queue.claim_for(task_id, key))
        dispatched = [row for row in queue.runs() if row.status == "dispatched"]
        self.assertCountEqual([(row.provider_id, row.account_id) for row in dispatched], receipts)

    def test_terminal_old_graph_config_ignores_routing_errors_but_keeps_database_authority(self):
        queue = db.QueueDB(self.config.database)
        queue.add_task(reviewed({"id": "old-worker", "title": "Old worker", "kind": "oneoff",
                                "cwd": str(self.root), "goal": "fixture", "size": "small"}))
        key = f"claude-personal/weekly/{self.now + 1000}"
        attempt = queue.claim("old-worker", key, "claude", "claude-personal",
                              provider_max_size="huge", now_epoch=self.now)
        queue.record("old-worker", key, attempt_id=attempt.id, status="dispatched",
                     provider_id="claude", account_id="claude-personal", router_job_id="old-job")
        old = dict(self.raw, accounts=[{"id": "claude-personal", "plan_id": "old"}])
        old["providers"] = [{key: value for key, value in item.items() if key != "max_size"}
                            for item in old["providers"]]
        self.config_path.write_text(json.dumps(old))
        args = ("--task", "old-worker", "--status", "failed", "--eligibility-key", key,
                "--provider-id", "claude", "--account-id", "claude-personal",
                "--attempt-id", attempt.id, "--summary", "old worker terminal")
        mismatch = self.cli("record", *args, "--database", str(self.root / "other.db"))
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertFalse((self.root / "other.db").exists())
        self.assertIsNotNone(queue.claim_for("old-worker", key))
        recorded = self.cli("record", *args)
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(json.loads(recorded.stdout)["run"]["account_id"], "claude-personal")

    def test_unmetered_provider_cap_counts_all_accounts_and_running_claims(self):
        raw = dict(self.raw, providers=[*self.raw["providers"], {
            "id": "future", "max_size": "small", "dispatch": {"adapter_id": "router", "provider": "future"}}])
        self.config = config.validate_config(raw)
        self.files.extend([{"provider": "future", "auth_index": "d"},
                           {"provider": "future", "auth_index": "d2"}])
        usage.refresh_all(self.config)
        queue = db.QueueDB(self.config.database)
        for index in range(4):
            queue.add_task(reviewed({"id": str(index), "title": "fixture", "kind": "oneoff",
                                    "priority": 2, "cwd": str(self.root), "goal": "fixture", "size": "small",
                                    "allowed_providers": ["future"]}))
        queue.claim("0", "future-d2/unmetered/123", "future", "future-d2", provider_max_size="huge", now_epoch=self.now)
        tick = scout.plan_tick(self.config, queue, now_epoch=self.now)
        self.assertEqual([(b.provider_id, b.batch_size) for b in tick.plan.batches], [("future", 1)])
        queue.claim("1", "future-d/unmetered/123", "future", "future-d", provider_max_size="huge", now_epoch=self.now)
        self.assertEqual(scout.plan_tick(self.config, queue, now_epoch=self.now).plan.batches, ())
        raised_cap = replace(self.config, unmetered_max_inflight=3)
        self.assertEqual(scout.plan_tick(raised_cap, queue, now_epoch=self.now).plan.batches[0].batch_size, 1)

    def test_transient_failure_is_retried_before_pool_planning(self):
        for provider, index in (("claude", "a"), ("codex", "c"), ("grok", "d"), ("muse", "e")):
            with self.subTest(provider=provider):
                self.requests.clear()
                self.sleep.reset_mock()
                self.fail_once.add(index)
                records = usage.refresh_all(self.config, now_epoch=self.now)
                snapshot = records[(provider, provider + "-" + index)]
                self.assertTrue(snapshot.fresh)
                self.assertIsNone(snapshot.error)
                self.assertTrue(snapshot.limits)
                self.sleep.assert_called_once_with(2)
                calls = [body for method, _path, body in self.requests
                         if method == "POST" and body["auth_index"] == index]
                self.assertEqual(len(calls), 2)
                plan = planner.build_plan(self.config, records, eligible_count=10, now_epoch=self.now)
                self.assertTrue(next(g for g in plan.gates if g.provider_id == provider).open)
                if provider == "muse":
                    downloads = [path for method, path, _body in self.requests
                                 if method == "GET" and "/download?" in path]
                    self.assertEqual(len(downloads), 2)

    def test_persistent_failure_records_error_after_exactly_two_attempts(self):
        self.failure.add("c")
        records = usage.refresh_all(self.config, now_epoch=self.now)
        self.sleep.assert_called_once_with(2)
        self.assertEqual(sum(method == "POST" and body["auth_index"] == "c"
                             for method, _path, body in self.requests), 2)
        snapshot = records[("codex", "codex-c")]
        self.assertFalse(snapshot.fresh)
        self.assertEqual(snapshot.error, "proxy usage unavailable")
        self.assertEqual(snapshot.limits, {})
        plan = planner.build_plan(self.config, records, eligible_count=10, now_epoch=self.now)
        self.assertFalse(next(g for g in plan.gates if g.provider_id == "codex").open)

    def test_muse_download_rejects_missing_or_invalid_token_without_api_call(self):
        for token in (None, "private-provider-token", "dca:", "dca:private\ntoken", 123):
            with self.subTest(token_type=type(token).__name__):
                self.muse_auth = {"dca_token": token}
                self.requests.clear()
                records = usage.refresh_all(self.config, now_epoch=self.now)
                snapshot = records[("muse", "muse-e")]
                self.assertEqual(snapshot.error, "proxy usage unavailable")
                self.assertFalse(any(method == "POST" and body["auth_index"] == "e"
                                     for method, _path, body in self.requests))
                self.assertNotIn("private", json.dumps(snapshot.cache_dict()))

    def test_muse_missing_auth_filename_fails_closed(self):
        self.files[4].pop("name")
        records = usage.refresh_all(self.config, now_epoch=self.now)
        self.assertEqual(records[("muse", "muse-e")].error, "proxy usage unavailable")
        self.assertFalse(any("/download?" in path for _method, path, _body in self.requests))

    def test_grok_parser_maps_iso_weekly_period(self):
        windows = proxy_usage.parse_windows("grok", self.bodies["d"])
        self.assertEqual(windows, {"seven_day": {"used_percent": 3.0, "resets_at": self.now + 14400}})

    def test_muse_parser_keeps_only_short_and_weekly_usage(self):
        windows = proxy_usage.parse_windows("muse", self.bodies["e"])
        self.assertEqual(windows, {
            "five_hour": {"used_percent": 8.0, "resets_at": self.now + 18000},
            "seven_day": {"used_percent": 9.0, "resets_at": self.now + 18000},
        })

    def test_grok_and_muse_parsers_reject_malformed_usage(self):
        invalid = [
            ("grok", {}), ("grok", {"config": {"currentPeriod": {"type": "monthly"}}}),
            ("muse", {}), ("muse", {"subs_usage": {"window": {"window_duration_mins": 60}}}),
        ]
        for percent in (None, True, -1, 101, float("nan"), "3"):
            grok = json.loads(json.dumps(self.bodies["d"]))
            grok["config"]["creditUsagePercent"] = percent
            invalid.append(("grok", grok))
            for name in ("window", "weekly"):
                muse = json.loads(json.dumps(self.bodies["e"]))
                muse["subs_usage"][name]["used_percent"] = percent
                invalid.append(("muse", muse))
        for provider, body in invalid:
            with self.subTest(provider=provider), self.assertRaises(usage.UsageError):
                proxy_usage.parse_windows(provider, body)

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
