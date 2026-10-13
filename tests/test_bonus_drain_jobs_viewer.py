"""Compatibility and security contracts for the established jobs viewer UI."""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from tests.readiness_fixture import reviewed

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
SERVER_PATH = SKILL_ROOT / "services" / "jobs-viewer" / "server.py"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import planner, usage
from bonus_drain.config import (
    DispatchBinding,
    LimitConfig,
    ProviderConfig,
    RuntimeConfig,
)

NOW = 2_000_000_000
EMPTY_PLAN = {"subscriptions": [], "gates": [], "allocations": [], "host_pressure": None}


def _viewer_config() -> RuntimeConfig:
    return RuntimeConfig(
        schema_version=1, source_path=None, database=Path("/unused/queue.db"),
        record_command=("/bin/true",), secret_refs=(), adapters=(),
        providers=tuple(
            ProviderConfig(provider, DispatchBinding("unused", provider), size)
            for provider, size in (("claude", "huge"), ("codex", "large"),
                                   ("grok", "small"), ("muse", "medium"))
        ),
        limits=tuple(
            LimitConfig(provider + "-weekly", provider, 604800, 95, 604800, 3,
                        max_percent_per_window=0.5, estimated_percent_per_job=1,
                        pacing_window_seconds=3600)
            for provider in ("claude", "codex")
        ),
        viewer={}, pr_exceptions=(), cache_dir=Path("/unused/cache"),
        proxy_secret_ref=None, unmetered_max_inflight=2,
    )


def _subscription(provider: str, label: str, *, used=22, reset=NOW + 36000,
                  fresh=True, short=None, unmetered=False) -> usage.UsageSnapshot:
    readings = {} if unmetered or used is None else {
        "seven_day": {"used_percent": used, "resets_at": reset},
    }
    if short is not None:
        readings["five_hour"] = {"used_percent": short, "resets_at": NOW + 18000}
    return usage.UsageSnapshot(
        provider, provider + "-" + label, NOW, readings,
        fresh=fresh, unmetered=unmetered,
    )


def _plan(*subscriptions, config=None, cycle_anchor=123, allocations=None, host_pressure=None):
    built = planner.build_plan(
        config or _viewer_config(),
        {(row.provider_id, row.account_id): row for row in subscriptions},
        eligible_count=20, now_epoch=NOW,
    ).to_dict()
    return {
        "generated_at": built["generated_at"], "gates": built["gates"],
        "subscriptions": built["subscriptions"], "allocations": allocations or [],
        "host_pressure": host_pressure, "cycle_anchor": cycle_anchor,
    }


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
        self.config = _viewer_config()
        config_patch = mock.patch.object(self.viewer.graph_config, "load_config", return_value=self.config)
        config_patch.start()
        self.addCleanup(config_patch.stop)

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
        # Muse adds one symbol to the existing icon sprite.
        self.assertEqual(
            hashlib.sha256(self.viewer.ICON_SPRITE.encode()).hexdigest(),
            "562da9e32e511ef6f74f8a21745e98a734390629eac7fdb60a913bdd729200be",
        )
        with (
            mock.patch.object(self.viewer, "render_bonus_body", return_value='<button class="task-run" data-engine="claude">force</button>'),
            mock.patch.object(self.viewer, "render_schedule_body", return_value="scheduled body"),
        ):
            page = self.viewer.render_page().decode()
        for marker in (
            "async work", "01 work queue", "02 schedules", "jobsViewerTab",
            'class="task-run"', 'data-engine="claude"', "scheduled body",
        ):
            self.assertIn(marker, page)
        for marker in ("authentication required", "sign in", "access secret", "bonus_drain_session"):
            self.assertNotIn(marker, page.lower())
        self.assertNotIn("independent provider capacity console", page)

    def test_provider_pool_cards_preserve_css_script_and_page_template(self) -> None:
        # PAGE embeds the sprite, so its hash changes only for the added Muse symbol.
        for name, expected in (
            ("CSS", "460cdff8a219ca0d0c892aa550e4292ece711346d632b7abb1a1e7802d7e82bd"),
            ("SCRIPT", "a5e2fb4eb4398d5b760feaa27158869e354523847580030f36b924bb8f8c902a"),
            ("PAGE", "7429b35b6a07a7eee4549a8dabaf88b8948928e56c51fb7e28db59ecde696c5f"),
        ):
            with self.subTest(component=name):
                self.assertEqual(hashlib.sha256(getattr(self.viewer, name).encode()).hexdigest(), expected)

    def test_open_pool_keeps_subscription_cards_idle_until_jobs_are_running(self) -> None:
        plan = _plan(
            _subscription("claude", "Business"), _subscription("codex", "Personal"),
            _subscription("grok", "d", unmetered=True),
        )
        claude = self.viewer._pool_cards(plan, "claude", 0, 0, config=self.config)[0]
        codex = self.viewer._pool_cards(plan, "codex", 0, 0, config=self.config)[0]
        grok = self.viewer._pool_cards(plan, "grok", 0, 0, config=self.config)[0]

        self.assertEqual(claude["name"], "Claude · Business")
        self.assertEqual(codex["name"], "Codex · Personal")
        self.assertEqual(grok["name"], "Grok")
        for card in (claude, codex):
            with self.subTest(account=card["name"]):
                self.assertFalse(card["draining"])
                row = self.viewer._account_row(card)
                self.assertNotIn('<i class="stripe on"></i>', row)
                self.assertIn('bar lg idle', row)

        self.assertIn("unmetered", self.viewer._account_row(grok).lower())
        self.assertIsNone(grok["u7"])

        draining = self.viewer._pool_cards(plan, "claude", 0, 1, config=self.config)[0]
        self.assertIn('bar lg draining', self.viewer._account_row(draining))
        self.assertIn('@keyframes drainshimmer', self.viewer.CSS)

    def test_account_rows_preserve_planner_usage_and_running_bar_markup(self) -> None:
        plan = _plan(_subscription("claude", "Personal", used=20))
        with mock.patch.object(self.viewer.time, "time", return_value=NOW):
            idle = self.viewer._account_row(self.viewer._pool_cards(plan, "claude", 1, 0, config=self.config)[0])
            running = self.viewer._account_row(self.viewer._pool_cards(plan, "claude", 1, 1, config=self.config)[0])
        self.assertIn("pts surplus", idle)
        self.assertIn("<b>20%</b>", idle)
        self.assertIn('<i class="stripe"></i>', idle)
        self.assertNotIn('<i class="stripe on"></i>', idle)
        self.assertIn('bar lg idle', idle)
        self.assertIn('bar lg draining', running)
        self.assertIn("pool running 1", running)
        self.assertIn('<i class="stripe"></i>', running)
        self.assertNotIn('<i class="stripe on"></i>', running)

    def test_usage_bar_marks_planner_floor_and_keeps_week_gone_in_the_pace_line(self) -> None:
        config = replace(self.config, limits=tuple(
            replace(limit, ceiling_percent=90) if limit.provider_id == "claude" else limit
            for limit in self.config.limits
        ))
        plan = _plan(_subscription("claude", "Personal", used=40, reset=NOW + 84 * 3600),
                     config=config)
        with (
            mock.patch.object(self.viewer.graph_config, "load_config", return_value=config),
            mock.patch.object(self.viewer.time, "time", return_value=NOW),
        ):
            card = self.viewer._pool_cards(plan, "claude", 1, 0, config=config)[0]
            self.assertEqual(card["reserve"], 42)
            row = self.viewer._account_row(card)
        self.assertNotIn('class="wk"', row)
        self.assertNotIn('title="week elapsed"', row)
        self.assertNotIn(".bar .wk", self.viewer.CSS)
        self.assertIn('class="fl" style="left:48.0%"', row)
        self.assertIn('<span class="fl"', row)
        self.assertIn("floor 48%", row)
        self.assertIn(">floor</span>", row)
        self.assertIn("50% of week gone", row)
        self.assertIn('title="floor"', row)
        self.assertIn(".bar .fl::before", self.viewer.CSS)
        self.assertIn("border-bottom:4px solid var(--floor)", self.viewer.CSS)
        self.assertIn("--floor:oklch(0.72 0.08 230)", self.viewer.CSS)

    def test_rendered_floor_uses_planner_pacing_and_omits_disabled_floor(self) -> None:
        for rate, expected_surplus in ((1.0, 30.0), (0.0, 40.0)):
            with self.subTest(floor_rate=rate):
                config = replace(self.config, limits=tuple(
                    replace(limit, ceiling_percent=90, max_percent_per_window=rate)
                    if limit.provider_id == "claude" else limit for limit in self.config.limits
                ))
                plan = _plan(_subscription("claude", "Personal", used=50), config=config)
                with (
                    mock.patch.object(self.viewer.graph_config, "load_config", return_value=config),
                    mock.patch.object(self.viewer.time, "time", return_value=NOW),
                ):
                    card = self.viewer._pool_cards(plan, "claude", 1, 0, config=config)[0]
                    self.assertEqual(card["surplus"], expected_surplus)
                    row = self.viewer._account_row(card)
                self.assertIn(f"pool mean {expected_surplus:g} pts surplus", row)
                if rate:
                    self.assertIn("floor 80%", row)
                    self.assertIn('title="floor"', row)
                else:
                    self.assertNotIn('title="floor"', row)

    def test_next_tick_capacity_keeps_codex_idle_until_actual_pool_dispatch(self) -> None:
        plan = _plan(_subscription("codex", "Personal", used=70))
        card = self.viewer._pool_cards(plan, "codex", 4, 0, config=self.config)[0]
        self.assertEqual(card["name"], "Codex · Personal")
        self.assertTrue(card["pool_open"])
        self.assertFalse(card["draining"])
        self.assertEqual(card["pool_batch"], 0)
        self.assertIn('bar lg idle', self.viewer._account_row(card))
        running = self.viewer._pool_cards(plan, "codex", 4, 2, config=self.config)[0]
        self.assertTrue(running["draining"])
        self.assertEqual(running["pool_batch"], 2)
        self.assertIn("pool running", running["tag"])
        self.assertIn('bar lg draining', self.viewer._account_row(running))

    def test_grok_and_muse_metered_usage_stays_numeric_when_idle_and_running(self) -> None:
        config = replace(self.config, limits=self.config.limits + tuple(
            LimitConfig(provider + "-weekly", provider, 604800, 95, 604800, 3,
                        max_percent_per_window=0.5, estimated_percent_per_job=1,
                        pacing_window_seconds=3600)
            for provider in ("grok", "muse")
        ))
        plan = _plan(
            _subscription("claude", "Business", used=48, reset=NOW + 7200),
            _subscription("grok", "d", used=3, reset=NOW + 14400),
            _subscription("muse", "e", used=9, reset=NOW + 18000, short=8),
            config=config,
        )
        with (
            mock.patch.object(self.viewer.graph_config, "load_config", return_value=config),
            mock.patch.object(self.viewer.time, "time", return_value=NOW),
        ):
            for provider, label, percent in (("grok", "d", 3), ("muse", "e", 9)):
                for live_batch in (0, 2):
                    with self.subTest(provider=provider, live_batch=live_batch):
                        cards = self.viewer._pool_cards(plan, provider, 4, live_batch, config=config)
                        self.assertEqual(len(cards), 1)
                        self.assertEqual(cards[0]["name"], provider.title() + " · " + label)
                        self.assertEqual(cards[0]["u7"], percent)
                        row = self.viewer._account_row(cards[0])
                        self.assertIn(f"<b>{percent}%</b>", row)
                        self.assertNotIn("unmetered", row)
                        self.assertIn('bar lg ' + ("draining" if live_batch else "idle"), row)
                        if live_batch:
                            self.assertIn("pool running 2", row)
            inflight = [
                {"ts": "2033-05-18T03:33:20Z", "task": provider + "-job",
                 "title": provider.title() + " job", "engine": provider, "cwd": "/unused"}
                for provider in ("claude", "grok", "muse")
            ]
            body = self._bonus_body(self.QUEUE_FIXTURE, plan=plan, inflight=inflight)
        cards = re.findall(r'<details class="arow">.*?</details>', body, re.DOTALL)
        self.assertEqual(len(cards), 3)
        for name, percent in (("Claude · Business", 48), ("Grok · d", 3), ("Muse · e", 9)):
            row = next(row for row in cards if name in row)
            self.assertIn(f"<b>{percent}%</b>", row)
            self.assertIn("pool running 1", row)
            self.assertIn('bar lg draining', row)
            self.assertNotIn("unmetered", row)

    def test_unmetered_subscriptions_share_one_provider_card_and_live_batch(self) -> None:
        for provider in ("grok", "muse"):
            with self.subTest(provider=provider):
                plan = _plan(_subscription(provider, "a", unmetered=True),
                             _subscription(provider, "b", unmetered=True))
                cards = self.viewer._pool_cards(plan, provider, 4, 1, config=self.config)
                self.assertEqual(len(cards), 1)
                card = cards[0]
                self.assertEqual(card["name"], provider.title())
                self.assertEqual(card["subscription_count"], 2)
                self.assertEqual(card["pool_batch"], 1)
                self.assertEqual(card["batch_n"], 2)
                self.assertTrue(card["draining"])
                self.assertIsNone(card["u7"])
                row = self.viewer._account_row(card)
                self.assertIn("unmetered", row)
                self.assertIn("1 running", row)
                self.assertIn('<i class="stripe on"></i>', row)

    def test_configured_empty_unmetered_pools_render_closed_cards_and_absent_pools_do_not(self) -> None:
        plan = _plan(config=self.config)
        for provider in ("grok", "muse"):
            with self.subTest(provider=provider):
                cards = self.viewer._pool_cards(plan, provider, 4, 0, config=self.config)
                self.assertEqual(len(cards), 1)
                row = self.viewer._account_row(cards[0])
                self.assertIn(provider.title(), row)
                self.assertIn("waiting · no proxy subscriptions", row)
                self.assertIn("unmetered", row)
                self.assertIn('<span>subscriptions</span><b>0</b>', row)
                self.assertNotIn('<i class="stripe on"></i>', row)
                self.assertEqual(
                    self.viewer._pool_cards(EMPTY_PLAN, provider, 4, 0, config=self.config), [],
                )
        body = self._bonus_body([], plan=plan)
        cards = re.findall(r'<details class="arow">.*?</details>', body, re.DOTALL)
        self.assertEqual(len(cards), 2)
        self.assertEqual(body.count("waiting · no proxy subscriptions"), 2)
        for provider in ("Grok", "Muse"):
            self.assertTrue(any(provider in row and "unmetered" in row for row in cards))
        absent = self._bonus_body([], plan=EMPTY_PLAN)
        self.assertNotIn('<details class="arow">', absent)

    def test_next_scout_is_systemd_backed(self) -> None:
        with mock.patch.object(self.viewer, "list_timers", return_value=[
            {"unit": "other.timer", "next": 1},
            {"unit": "bonus-drain-scout.timer", "next": 2_000},
        ]):
            self.assertEqual(self.viewer.next_scout(), 2_000)

    def test_remaining_and_force_buttons_use_graph_provider_eligibility(self) -> None:
        pick = [{"id": "claude-codex", "title": "Claude and Codex", "kind": "oneoff",
                 "priority": 2, "cwd": "/tmp", "goal": "test",
                 "eligible_providers": ["claude", "codex"],
                 "compatible_providers": ["claude", "codex"]}]
        with (
            mock.patch.object(self.viewer, "_remaining_snapshot", return_value=pick),
            mock.patch.object(self.viewer, "_last_runs", return_value={}),
        ):
            remaining = self.viewer.get_remaining(123)
        self.assertEqual(remaining[0]["eligible_providers"], ["claude", "codex"])
        with mock.patch.object(self.viewer, "MUTATIONS_ENABLED", True):
            buttons = self.viewer._run_buttons(remaining[0])
        self.assertIn('data-engine="claude"', buttons)
        self.assertIn('data-engine="codex"', buttons)
        self.assertIn('data-engine="grok"', buttons)
        grok = buttons[buttons.index('data-engine="grok"'):]
        self.assertIn('disabled title="This task is not eligible for Grok"', grok)

    def test_viewer_is_slim_and_retired_helpers_are_absent(self) -> None:
        self.assertLessEqual(len(SERVER_PATH.read_text().splitlines()), 2600)
        for name in (
            "get_usage", "get_codex_usage", "get_grok_usage", "get_gates",
            "_provider_usage", "_preview_account_gates", "_claude_multi_active",
            "_canonical_cycle", "_legacy_providers", "_rotation", "_verdict",
            "_pacing_strip", "get_dispatch_times", "_claude_cards", "_codex_cards",
            "_grok_cards", "_muse_cards", "_unmetered_card",
        ):
            with self.subTest(helper=name):
                self.assertFalse(hasattr(self.viewer, name))
        self.assertFalse(any(name.startswith("_normalize") for name in vars(self.viewer)))

    def test_read_plan_returns_raw_local_cli_payload_without_usage_cache_reads(self) -> None:
        payload = _plan(
            _subscription("claude", "Business"), _subscription("grok", "d", unmetered=True),
            allocations=[{"task_id": "t1", "provider_id": "grok", "account_id": "grok"}],
            host_pressure={"reason": "fixture pressure"}, cycle_anchor=NOW + 101,
        )
        self.assertEqual(set(self.viewer._plan_cache), {"t", "val"})
        self.viewer._plan_cache.update(t=0.0, val=None)
        response = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        with (
            mock.patch.object(self.viewer.subprocess, "run", return_value=response) as run,
            mock.patch.object(usage, "read_all") as read,
            mock.patch.object(usage, "read_accounts") as accounts,
        ):
            plan = self.viewer.read_plan()
        self.assertEqual(run.call_args.args[0],
                         [sys.executable, "-m", "bonus_drain", "gates", "--json", "--local"])
        self.assertEqual(plan, payload)
        self.assertIsInstance(plan["gates"], list)
        self.assertIsInstance(plan["allocations"], list)
        self.assertEqual(plan["cycle_anchor"], NOW + 101)
        self.assertNotIn("provider_gates", plan)
        self.assertNotIn("acct", plan)
        read.assert_not_called()
        accounts.assert_not_called()

    def test_read_plan_cache_expires_at_sixty_seconds(self) -> None:
        first, second = _plan(cycle_anchor=101), _plan(cycle_anchor=202)
        self.viewer._plan_cache.update(t=0.0, val=None)
        responses = [SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
                     for payload in (first, second)]
        with (
            mock.patch.object(self.viewer.subprocess, "run", side_effect=responses) as run,
            mock.patch.object(self.viewer.time, "time", return_value=1000) as clock,
        ):
            self.assertEqual(self.viewer.read_plan(), first)
            clock.return_value = 1059.999
            self.assertEqual(self.viewer.read_plan(), first)
            self.assertEqual(run.call_count, 1)
            clock.return_value = 1060
            self.assertEqual(self.viewer.read_plan(), second)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(self.viewer._plan_cache, {"t": 1060, "val": second})

    def test_failed_read_plan_returns_empty_sanitized_plan_instead_of_old_open_gates(self) -> None:
        previous = _plan(_subscription("claude", "Business"))
        failures = (
            SimpleNamespace(returncode=7, stdout=json.dumps(previous), stderr="credential=do_not_expose"),
            SimpleNamespace(returncode=0, stdout="credential=do_not_expose", stderr=""),
            SimpleNamespace(returncode=0, stdout=json.dumps([]), stderr=""),
            subprocess.TimeoutExpired(cmd=["do_not_expose"], timeout=25),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.viewer._plan_cache.update(t=1.0, val=previous)
                kwargs = ({"side_effect": failure} if isinstance(failure, Exception)
                          else {"return_value": failure})
                with (
                    mock.patch.object(self.viewer.subprocess, "run", **kwargs) as run,
                    mock.patch.object(self.viewer.time, "time", return_value=1000),
                    mock.patch.object(usage, "read_all") as read,
                ):
                    plan = self.viewer.read_plan()
                self.assertEqual(plan, EMPTY_PLAN)
                self.assertNotIn("do_not_expose", json.dumps(plan))
                self.assertEqual(run.call_count, 1)
                read.assert_not_called()

    def test_metered_cards_keep_original_account_readings_and_shared_pool_gate(self) -> None:
        plan = _plan(
            _subscription("claude", "Personal", used=80, reset=NOW + 40 * 3600, short=17),
            _subscription("claude", "Business", used=65, reset=NOW + 10 * 3600, short=71),
            _subscription("codex", "Other", used=99),
        )
        original = json.loads(json.dumps(plan))
        with mock.patch.object(usage, "read_all") as read:
            cards = self.viewer._pool_cards(plan, "claude", 7, 2, config=self.config)
        read.assert_not_called()
        self.assertEqual(plan, original)
        self.assertEqual(len(cards), 2)
        by_account = {card["account_id"]: card for card in cards}
        for label, used, hours, reserve, surplus in (
            ("Personal", 80, 40, 20, -5), ("Business", 65, 10, 5, 25),
        ):
            card = by_account["claude-" + label]
            self.assertEqual(card["name"], "Claude · " + label)
            self.assertEqual((card["u7"], card["r7"]),
                             (used, NOW + hours * 3600))
            self.assertEqual((card["reserve"], card["surplus"]), (reserve, surplus))
            self.assertEqual(card["ceiling"], 95)
            self.assertTrue(card["pool_open"])
            self.assertEqual(card["pool_surplus"], 10)
            self.assertEqual(card["pool_batch"], 2)
            self.assertTrue(card["draining"])

    def test_stale_or_missing_subscription_readings_render_unknown(self) -> None:
        for subscription in (
            _subscription("claude", "stale", used=86, fresh=False),
            _subscription("claude", "missing", used=None),
        ):
            with self.subTest(account=subscription.account_id):
                plan = _plan(subscription)
                if not subscription.fresh:
                    self.assertEqual(plan["subscriptions"][0]["pacing"]["claude-weekly"]["used_percent"], 86)
                card = self.viewer._pool_cards(plan, "claude", 1, 0, config=self.config)[0]
                self.assertIsNone(card["u7"])
                self.assertFalse(card["pool_open"])
                row = self.viewer._account_row(card)
                self.assertIn('bar lg unknown', row)
                self.assertIn("no spend reading", row)
                self.assertNotIn("<b>0%</b>", row)
                self.assertIn(card["pool_reason"], row)
                running = self.viewer._pool_cards(plan, "claude", 1, 1, config=self.config)[0]
                running_row = self.viewer._account_row(running)
                self.assertIn("pool running 1", running_row)
                self.assertIn('bar lg unknown', running_row)
                self.assertIn('<i class="stripe unk"></i>', running_row)
                self.assertIn('<span class="n2 unk">', running_row)
                self.assertNotIn('<i class="stripe on"></i>', running_row)

    def test_generic_cards_use_each_providers_own_limit_and_pacing(self) -> None:
        config = replace(self.config, limits=tuple(
            replace(limit, ceiling_percent=90, lead_seconds=86400, batch_size=5,
                    max_percent_per_window=2, pacing_window_seconds=7200)
            if limit.provider_id == "codex" else limit for limit in self.config.limits
        ))
        plan = _plan(_subscription("claude", "Personal", used=50, reset=NOW + 43200),
                     _subscription("codex", "Personal", used=50, reset=NOW + 43200),
                     config=config)
        with mock.patch.object(self.viewer.graph_config, "load_config", return_value=config):
            claude = self.viewer._pool_cards(plan, "claude", 2, 0, config=config)[0]
            codex = self.viewer._pool_cards(plan, "codex", 2, 0, config=config)[0]
        self.assertEqual((claude["ceiling"], claude["reserve"], claude["surplus"]), (95, 6, 39))
        self.assertEqual((codex["ceiling"], codex["reserve"], codex["surplus"]), (90, 12, 28))
        with mock.patch.object(self.viewer.time, "time", return_value=NOW):
            self.assertIn("floor 89%", self.viewer._account_row(claude))
            self.assertIn("floor 78%", self.viewer._account_row(codex))

    def test_closed_pool_reason_does_not_override_durable_running_state(self) -> None:
        plan = _plan(_subscription("codex", "Personal", used=99))
        closed = self.viewer._pool_cards(plan, "codex", 4, 0, config=self.config)[0]
        gate = next(gate for gate in plan["gates"] if gate["provider_id"] == "codex")
        self.assertFalse(closed["pool_open"])
        self.assertEqual(closed["pool_reason"], gate["reason"])
        self.assertIn(gate["reason"], self.viewer._account_row(closed))
        running = self.viewer._pool_cards(plan, "codex", 4, 1, config=self.config)[0]
        self.assertFalse(running["pool_open"])
        self.assertTrue(running["draining"])
        self.assertIn("pool running 1", self.viewer._account_row(running))

    def test_drain_order_keeps_reset_priority_and_unknown_last_in_a_running_shared_pool(self) -> None:
        for include_unknown in (False, True):
            with self.subTest(include_unknown=include_unknown):
                subscriptions = [
                    _subscription("claude", "Personal", reset=NOW + 7200),
                    _subscription("claude", "Business", reset=NOW + 10800),
                    _subscription("codex", "Personal", reset=NOW + 14400),
                    _subscription("codex", "Business", reset=NOW + 18000),
                ]
                expected = ["claude-Personal", "claude-Business", "codex-Personal", "codex-Business"]
                if include_unknown:
                    subscriptions.append(_subscription("codex", "Unknown", used=None))
                    expected.append("codex-Unknown")
                plan = _plan(*subscriptions, config=self.config)
                with mock.patch.object(self.viewer.time, "time", return_value=NOW):
                    cards = self.viewer._pool_cards(plan, "codex", 4, 2, config=self.config)
                    cards.extend(self.viewer._pool_cards(plan, "claude", 4, 0, config=self.config))
                    ordered = self.viewer._drain_order(cards)
                self.assertEqual([card["account_id"] for card in ordered], expected)

    def test_current_cycle_uses_exact_shared_planner_anchor_without_reconstruction(self) -> None:
        plan = _plan(_subscription("claude", "Personal", reset=NOW + 7200),
                     _subscription("claude", "Business", reset=NOW + 10800),
                     _subscription("codex", "Other", reset=NOW + 3600),
                     cycle_anchor=NOW + 101)
        with mock.patch.object(self.viewer, "_db") as database:
            self.assertEqual(self.viewer.current_cycle(plan), NOW + 101)
            self.assertEqual(self.viewer.current_cycle(dict(plan, cycle_anchor=0)), 0)
        database.assert_not_called()

    def test_missing_plan_anchor_falls_back_to_database_history_or_zero(self) -> None:
        plan = _plan(_subscription("claude", "Personal"))
        del plan["cycle_anchor"]
        for row, expected in (((NOW + 77,), NOW + 77), ((None,), 0)):
            with self.subTest(history=row), mock.patch.object(self.viewer, "_db") as database:
                database.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = row
                self.assertEqual(self.viewer.current_cycle(plan), expected)
                database.return_value.__enter__.return_value.execute.assert_called_once_with(
                    "SELECT MAX(cycle) FROM runs",
                )
        with mock.patch.object(self.viewer, "_db", side_effect=OSError("missing history")):
            self.assertEqual(self.viewer.current_cycle(EMPTY_PLAN), 0)

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
        cfg = mock.Mock(
            database=Path("/tmp/queue.db"), recurrence_timezone="America/New_York",
        )
        with (
            mock.patch.object(self.viewer.graph_config, "load_config", return_value=cfg),
            mock.patch.object(self.viewer, "QueueDB") as queue_type,
            mock.patch.object(self.viewer, "kick_task", return_value=result) as kick,
            mock.patch.dict(os.environ, {"BONUS_DRAIN_CONFIG": "/tmp/config.json"}),
        ):
            ok, message = self.viewer.run_task_now("portable-a", "claude")
        self.assertTrue(ok, message)
        kick.assert_called_once_with(cfg, queue_type.return_value, "portable-a", "claude")

    def test_requeue_restores_only_a_retryable_terminal_job(self) -> None:
        cfg = mock.Mock(
            database=Path("/tmp/queue.db"), recurrence_timezone="America/New_York",
        )
        queue = mock.Mock()
        queue.requeue.return_value = True
        with (
            mock.patch.object(self.viewer.graph_config, "load_config", return_value=cfg),
            mock.patch.object(self.viewer, "QueueDB", return_value=queue) as queue_type,
            mock.patch.dict(os.environ, {"BONUS_DRAIN_CONFIG": "/tmp/config.json"}),
        ):
            ok, message = self.viewer.requeue_task("portable-a")
        self.assertTrue(ok, message)
        queue_type.assert_called_once_with(
            cfg.database, recurrence_timezone="America/New_York",
        )
        queue.requeue.assert_called_once_with("portable-a")

        queue.requeue.side_effect = self.viewer.QueueError("portable-a last ran done; refusing to retry completed work")
        with (
            mock.patch.object(self.viewer.graph_config, "load_config", return_value=cfg),
            mock.patch.object(self.viewer, "QueueDB", return_value=queue),
        ):
            ok, message = self.viewer.requeue_task("portable-a")
        self.assertFalse(ok)
        self.assertIn("last ran done", message)

    def test_requeue_endpoint_uses_the_existing_mutation_boundary(self) -> None:
        self.viewer.ALLOWED_HOSTS = (self.HOST,)
        self.viewer.ALLOWED_ORIGINS = (self.ORIGIN,)
        self.viewer.MUTATIONS_ENABLED = True
        server = self._server()
        payload = json.dumps({"id": "portable-a"}).encode()
        headers = {
            "Host": self.HOST, "Origin": self.ORIGIN,
            "Content-Type": "application/json", "Content-Length": str(len(payload)),
        }
        with mock.patch.object(self.viewer, "requeue_task", return_value=(True, "requeued")) as requeue:
            status, _headers, body = self._request(
                server, "POST", "/api/bonus/task/requeue", headers=headers, body=payload,
            )
        self.assertEqual(status, 200, body)
        requeue.assert_called_once_with("portable-a")

    def test_inflight_rows_offer_done_and_failed_cutoffs(self) -> None:
        inflight = [{
            "ts": "2026-08-26T12:00:00Z", "task": "runaway-job",
            "title": "Runaway job", "engine": "grok", "cwd": "/tmp/runaway",
        }]
        self.viewer.MUTATIONS_ENABLED = True
        body = self._bonus_body([], inflight=inflight)
        row_start = body.index('<span class="fltitle">Runaway job</span>')
        row = body[row_start:body.find("</div>", body.find("flact", row_start)) + 6]
        self.assertIn('class="task-finish ionly" data-task-id="runaway-job" data-status="done"', body)
        self.assertIn('class="task-finish ionly" data-task-id="runaway-job" data-status="failed"', body)
        self.assertIn('aria-label="Mark Runaway job done"', body)
        self.assertIn('aria-label="Mark Runaway job failed"', body)
        self.assertIn(self.viewer.ico("check"), row)
        self.assertIn(self.viewer.ico("ban"), row)
        self.assertIn("/api/bonus/task/finish", self.viewer.SCRIPT)
        self.assertNotIn("unavailable", row)

    def test_inflight_t3_jobs_are_badged_as_t3(self) -> None:
        inflight = [
            {
                "ts": "2026-08-26T12:00:00Z", "task": "t3-job", "title": "Thread job",
                "engine": "claude", "cwd": "/tmp/t3", "surface": "t3",
            },
            {
                "ts": "2026-08-26T12:00:00Z", "task": "bg-job", "title": "Background job",
                "engine": "claude", "cwd": "/tmp/bg", "surface": "background",
            },
        ]
        body = self._bonus_body([], inflight=inflight)
        t3_start = body.index('<span class="fltitle">Thread job</span>')
        t3_row = body[t3_start:body.index('<span class="fltitle">Background job</span>')]
        bg_row = body[body.index('<span class="fltitle">Background job</span>'):]
        self.assertIn('class="dimtxt surfbadge"', t3_row)
        self.assertIn(">t3<", t3_row)
        self.assertNotIn("surfbadge", bg_row.split('<div class="fl">')[0])

    def test_inflight_cutoffs_are_disabled_when_the_viewer_is_read_only(self) -> None:
        inflight = [{
            "ts": "2026-08-26T12:00:00Z", "task": "runaway-job",
            "title": "Runaway job", "engine": "grok", "cwd": "/tmp/runaway",
        }]
        self.viewer.MUTATIONS_ENABLED = False
        body = self._bonus_body([], inflight=inflight)
        self.assertIn('class="task-finish ionly unavailable"', body)
        self.assertIn('disabled title="Viewer is read-only"', body)
        self.assertIn('data-status="done"', body)
        self.assertIn('data-status="failed"', body)

    def test_clicking_inflight_failed_disables_the_row_in_a_browser(self) -> None:
        chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if chrome is None:
            self.skipTest("Chrome is unavailable")
        inflight = [{
            "ts": "2026-08-26T12:00:00Z", "task": "runaway-job",
            "title": "Runaway job", "engine": "grok", "cwd": "/tmp/runaway",
        }]
        self.viewer.MUTATIONS_ENABLED = True
        with (
            mock.patch.object(
                self.viewer, "render_bonus_body",
                return_value=self._bonus_body([], inflight=inflight),
            ),
            mock.patch.object(self.viewer, "render_schedule_body", return_value="scheduled body"),
        ):
            page = self.viewer.render_page().decode()
        page = page.replace(
            "<head>",
            "<head><script>window.fetch=function(){return new Promise(function(){})}</script>",
            1,
        )
        with tempfile.TemporaryDirectory() as work:
            target = Path(work) / "page.html"
            target.write_text(
                page + '<script>document.querySelector('
                '\'.task-finish[data-status="failed"]\').click();</script>'
            )
            completed = subprocess.run(
                [
                    chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                    "--disable-dev-shm-usage", "--no-proxy-server",
                    f"--user-data-dir={work}/profile", "--dump-dom", target.as_uri(),
                ],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        dom = completed.stdout
        act_at = dom.index('class="flact"')
        act = dom[act_at:dom.find("</span>", act_at) + 7]
        self.assertIn('data-status="failed"', act)
        self.assertIn("busy", act)
        self.assertEqual(act.count("disabled"), 2)

    def test_finish_records_a_terminal_event_for_the_inflight_run(self) -> None:
        run = mock.Mock(
            task="runaway-job", eligibility_key="claude-personal/manual/123",
            kind="oneoff", cycle=123, account_id="claude-personal",
            provider_id="claude", router_job_id="job-9", attempt_id="attempt-9",
        )
        queue = mock.Mock()
        queue.inflight.return_value = [run]
        account = mock.Mock(activation_adapter_id="switch")
        cfg = mock.Mock(
            database=Path("/tmp/queue.db"), recurrence_timezone="America/New_York",
        )
        cfg.account.return_value = account
        with (
            mock.patch.object(self.viewer.graph_config, "load_config", return_value=cfg),
            mock.patch.object(self.viewer, "QueueDB", return_value=queue),
            mock.patch.dict(os.environ, {"BONUS_DRAIN_CONFIG": "/tmp/config.json"}),
        ):
            ok, message = self.viewer.finish_inflight_task("runaway-job", "failed")
        self.assertTrue(ok, message)
        queue.record.assert_called_once()
        args, kwargs = queue.record.call_args
        self.assertEqual(args[:2], ("runaway-job", "claude-personal/manual/123"))
        self.assertEqual(kwargs["status"], "failed")
        self.assertEqual(kwargs["kind"], "oneoff")
        self.assertEqual(kwargs["cycle"], 123)
        self.assertEqual(kwargs["provider_id"], "claude")
        self.assertEqual(kwargs["account_id"], "claude-personal")
        self.assertEqual(kwargs["router_job_id"], "job-9")
        self.assertEqual(kwargs["attempt_id"], "attempt-9")
        self.assertEqual(kwargs["outcome"]["reason"]["code"], "verification_needed")
        self.assertNotIn("completion", kwargs["outcome"])
        self.assertIn("failed", kwargs["summary"])

    def test_finish_rejects_jobs_that_are_not_in_flight(self) -> None:
        queue = mock.Mock()
        queue.inflight.return_value = []
        cfg = mock.Mock(
            database=Path("/tmp/queue.db"), recurrence_timezone="America/New_York",
        )
        with (
            mock.patch.object(self.viewer.graph_config, "load_config", return_value=cfg),
            mock.patch.object(self.viewer, "QueueDB", return_value=queue),
        ):
            ok, message = self.viewer.finish_inflight_task("idle-job", "done")
        self.assertFalse(ok)
        self.assertIn("not in flight", message)
        queue.record.assert_not_called()

    def test_finish_drops_a_real_inflight_run_from_the_queue(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db_path = Path(temporary.name) / "queue.db"
        queue = self.viewer.QueueDB(db_path)
        queue.initialize()
        queue.add_task(reviewed({
            "id": "runaway-job", "title": "Runaway", "kind": "oneoff",
            "priority": 2, "cwd": "/tmp", "goal": "run", "active": True,
        }))
        key = "claude-account/manual/2000000000"
        queue.record(
            "runaway-job", key, status="dispatched", provider_id="claude",
            account_id="claude-personal", router_job_id="job-9",
            kind="oneoff", cycle=2000000000,
        )
        self.assertEqual(len(queue.inflight()), 1)
        cfg = mock.Mock(database=db_path, recurrence_timezone="America/New_York")
        cfg.account.side_effect = self.viewer.graph_config.ConfigError("unknown account")
        with (
            mock.patch.object(self.viewer.graph_config, "load_config", return_value=cfg),
            mock.patch.dict(os.environ, {"BONUS_DRAIN_CONFIG": "/tmp/config.json"}),
        ):
            ok, message = self.viewer.finish_inflight_task("runaway-job", "failed")
        self.assertTrue(ok, message)
        self.assertEqual(queue.inflight(), [])
        last = queue.runs(task_id="runaway-job")[0]
        self.assertEqual(last.status, "failed")
        self.assertEqual(last.eligibility_key, key)

    def test_finish_real_attempt_records_owned_operator_outcome(self) -> None:
        for status in ("done", "failed"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as work:
                db_path = Path(work) / "queue.db"
                queue = self.viewer.QueueDB(db_path)
                queue.initialize()
                parent_id = f"operator-parent-{status}"
                child_id = f"operator-child-{status}"
                queue.add_task(reviewed({
                    "id": parent_id, "title": "Operator parent", "kind": "oneoff",
                    "priority": 2, "cwd": "/tmp", "goal": "run", "active": True,
                }))
                queue.add_task(reviewed({
                    "id": child_id, "title": "Operator child", "kind": "oneoff",
                    "priority": 2, "cwd": "/tmp", "goal": "run", "active": True,
                    "depends_on": [parent_id],
                }))
                key = "claude-personal/manual/2000000000"
                attempt = queue.claim(
                    parent_id, key, "claude", "claude-personal",
                    provider_max_size="huge", now_epoch=2_000_000_000,
                )
                self.assertIsNotNone(attempt)
                router_job_id = f"router-{status}"
                queue.record(
                    parent_id, key, attempt_id=attempt.id, status="dispatched",
                    provider_id="claude", account_id="claude-personal",
                    router_job_id=router_job_id, kind="oneoff", cycle=2_000_000_000,
                )
                cfg = mock.Mock(database=db_path, recurrence_timezone="America/New_York")
                account = mock.Mock(activation_adapter_id="switch")
                cfg.account.return_value = account
                with (
                    mock.patch.object(
                        self.viewer.graph_config, "load_config", return_value=cfg,
                    ),
                    mock.patch.dict(
                        os.environ, {"BONUS_DRAIN_CONFIG": "/tmp/config.json"},
                    ),
                ):
                    ok, message = self.viewer.finish_inflight_task(parent_id, status)

                self.assertTrue(ok, message)
                self.assertEqual(queue.inflight(), [])
                self.assertIsNone(queue.claim_for(parent_id, key))
                terminal = queue.runs(task_id=parent_id)[0]
                self.assertEqual(terminal.status, status)
                self.assertEqual(terminal.attempt_id, attempt.id)
                self.assertIsNotNone(terminal.outcome)
                if status == "done":
                    self.assertEqual(
                        terminal.outcome["reason"]["code"], "done_when_verified",
                    )
                    self.assertEqual(
                        terminal.outcome["completion"]["mechanism"],
                        "operator_receipt",
                    )
                    self.assertTrue(terminal.outcome["completion"]["verified"])
                    evidence = " ".join(terminal.outcome["completion"]["evidence"])
                    self.assertIn(attempt.id, evidence)
                    self.assertIn(router_job_id, evidence)
                    self.assertTrue(queue.readiness(child_id)["ready"])
                else:
                    self.assertEqual(
                        terminal.outcome["reason"]["code"], "verification_needed",
                    )
                    self.assertNotIn("completion", terminal.outcome)
                    self.assertFalse(queue.readiness(child_id)["ready"])

    def test_finish_endpoint_uses_the_existing_mutation_boundary(self) -> None:
        self.viewer.ALLOWED_HOSTS = (self.HOST,)
        self.viewer.ALLOWED_ORIGINS = (self.ORIGIN,)
        self.viewer.MUTATIONS_ENABLED = True
        server = self._server()
        payload = json.dumps({"id": "runaway-job", "status": "failed"}).encode()
        headers = {
            "Host": self.HOST, "Origin": self.ORIGIN,
            "Content-Type": "application/json", "Content-Length": str(len(payload)),
        }
        with mock.patch.object(
            self.viewer, "finish_inflight_task", return_value=(True, "failed"),
        ) as finish:
            status, _headers, body = self._request(
                server, "POST", "/api/bonus/task/finish", headers=headers, body=payload,
            )
            self.assertEqual(status, 200, body)
            finish.assert_called_once_with("runaway-job", "failed")
            rejected = json.dumps({"id": "runaway-job", "status": "skipped"}).encode()
            status, _headers, _body = self._request(
                server, "POST", "/api/bonus/task/finish",
                headers={**headers, "Content-Length": str(len(rejected))}, body=rejected,
            )
            self.assertEqual(status, 400)
            self.assertEqual(finish.call_count, 1)

    def test_run_log_offers_requeue_only_for_failed_and_skipped_jobs(self) -> None:
        history = [
            {"ts": "2026-08-26T12:00:00Z", "task": "failed-job", "title": "Failed job", "kind": "oneoff", "status": "failed", "engine": "codex", "cycle": 123, "summary": "error", "branch": None},
            {"ts": "2026-08-26T11:00:00Z", "task": "skipped-job", "title": "Skipped job", "kind": "oneoff", "status": "skipped", "engine": "claude", "cycle": 123, "summary": "precondition", "branch": None},
            {"ts": "2026-08-26T10:00:00Z", "task": "done-job", "title": "Done job", "kind": "oneoff", "status": "done", "engine": "claude", "cycle": 123, "summary": "complete", "branch": None},
        ]
        body = self._bonus_body([], runs=history)
        self.assertIn('class="task-requeue ionly" data-task-id="failed-job"', body)
        self.assertIn('class="task-requeue ionly" data-task-id="skipped-job"', body)
        self.assertIn(self.viewer.ico("cycle"), body)
        self.assertIn('aria-label="Codex">' + self.viewer.ico("codex"), body)
        self.assertIn('aria-label="Claude">' + self.viewer.ico("claude"), body)
        self.assertEqual(body.count('class="laction"'), len(history))
        done_start = body.index('<span class="ltitle">Done job</span>')
        done_end = body.find('</div>', done_start)
        self.assertNotIn("task-requeue", body[done_start:done_end])

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
            self.viewer.graph_dispatcher.DispatchError("launch unavailable"),
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
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("01 work queue", completed.stdout)
        self.assertIn("02 schedules", completed.stdout)
        self.assertIn('class="task-run"', completed.stdout)
        self.assertNotRegex(completed.stdout.lower(), r"sign in|access secret|authentication required")

    def test_upcoming_rows_render_size_or_unknown_without_annotating_history(self) -> None:
        unexpected = '<img src=x onerror=alert(1)>'
        remaining = [
            {
                "id": "medium-job", "title": "Medium upcoming", "kind": "recurring",
                "priority": 2, "cadence": "weekly", "cwd": "/tmp/medium", "goal": "medium goal",
                "eligible_providers": ["claude", "codex", "grok", "muse"],
                "compatible_providers": ["claude", "codex", "grok", "muse"], "last_ts": "2026-08-23T12:00:00Z", "size": "medium",
            },
            {
                "id": "legacy-job", "title": "Legacy upcoming", "kind": "oneoff",
                "priority": 2, "cadence": None, "cwd": "/tmp/legacy", "goal": "legacy goal",
                "eligible_providers": ["claude", "codex", "grok", "muse"],
                "compatible_providers": ["claude", "codex", "grok", "muse"], "last_ts": "2026-08-24T12:00:00Z", "size": None,
            },
            {
                "id": "new-recurring", "title": "New recurring", "kind": "recurring",
                "priority": 2, "cadence": "weekly", "cwd": "/tmp/scheduled", "goal": "new goal",
                "eligible_providers": ["claude"], "compatible_providers": ["claude"], "last_ts": None, "size": "small",
            },
            {
                "id": "unexpected-job", "title": "Unexpected upcoming", "kind": "oneoff",
                "priority": 2, "cadence": None, "cwd": "/tmp/unexpected", "goal": "unexpected goal",
                "eligible_providers": ["claude", "codex", "grok", "muse"],
                "compatible_providers": ["claude", "codex", "grok", "muse"], "last_ts": None, "size": unexpected,
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
            mock.patch.object(self.viewer, "read_plan", return_value=dict(EMPTY_PLAN, cycle_anchor=123)),
            mock.patch.object(self.viewer, "current_cycle", return_value=123),
            mock.patch.object(self.viewer, "get_remaining", return_value=remaining),
            mock.patch.object(self.viewer, "get_recent_runs", return_value=history),
            mock.patch.object(self.viewer, "get_disabled", return_value=disabled),
            mock.patch.object(self.viewer, "get_inflight", return_value=[]),
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

        run_start = body.index("<span>results / run history</span>")
        disabled_start = body.index("<span><i class=\"caret\"></i>disabled")
        history_section = body[run_start:disabled_start]
        disabled_section = body[disabled_start:body.index("<footer>", disabled_start)]
        self.assertNotIn("size estimate", history_section)
        self.assertNotIn("size estimate", disabled_section)


    QUEUE_FIXTURE: ClassVar[list[dict[str, object]]] = [
        {
            "id": "weekly-claude", "title": "Weekly Claude sweep", "kind": "recurring",
            "priority": 1, "cadence": "weekly", "cwd": "/tmp/one", "goal": "sweep",
            "size": "small", "last_ts": None, "eligible_providers": ["claude"], "compatible_providers": ["claude"],
        },
        {
            "id": "oneoff-codex", "title": "One-off Codex refactor", "kind": "oneoff",
            "priority": 2, "cadence": None, "cwd": "/tmp/two", "goal": "refactor",
            "size": "large", "last_ts": None, "eligible_providers": ["codex", "grok"], "compatible_providers": ["codex", "grok"],
        },
        {
            "id": "oneoff-claude", "title": "One-off Claude audit", "kind": "oneoff",
            "priority": 2, "cadence": None, "cwd": "/tmp/three", "goal": "audit",
            "size": "small", "last_ts": None, "eligible_providers": ["claude", "codex"], "compatible_providers": ["claude", "codex"],
        },
    ]

    def _bonus_body(self, remaining, runs=None, plan=None, inflight=None):
        with (
            mock.patch.object(self.viewer, "read_plan",
                              return_value=plan if plan is not None else dict(EMPTY_PLAN, cycle_anchor=123)),
            mock.patch.object(self.viewer, "get_remaining", return_value=remaining),
            mock.patch.object(self.viewer, "get_recent_runs", return_value=runs or []),
            mock.patch.object(self.viewer, "get_disabled", return_value=[]),
            mock.patch.object(self.viewer, "get_inflight", return_value=inflight or []),
        ):
            return self.viewer.render_bonus_body()

    def test_rendered_capacity_uses_inflight_counts_and_keeps_planned_batches_idle(self) -> None:
        plan = _plan(
            _subscription("claude", "Personal"), _subscription("claude", "Business"),
            _subscription("codex", "Other"), _subscription("grok", "d", unmetered=True),
        )
        inflight = [
            {"ts": "2033-05-18T03:33:20Z", "task": provider + "-job",
             "title": provider.title() + " job", "engine": provider, "cwd": "/unused"}
            for provider in ("claude", "grok")
        ]
        with mock.patch.object(usage, "read_all") as read:
            body = self._bonus_body(self.QUEUE_FIXTURE, plan=plan, inflight=inflight)
        read.assert_not_called()
        cards = re.findall(r'<details class="arow">.*?</details>', body, re.DOTALL)
        self.assertEqual(len(cards), 5)
        for name in ("Claude · Personal", "Claude · Business"):
            row = next(row for row in cards if name in row)
            self.assertIn("pool running 1", row)
            self.assertIn('bar lg draining', row)
            self.assertIn('<i class="stripe"></i>', row)
            self.assertNotIn('<i class="stripe on"></i>', row)
        codex = next(row for row in cards if "Codex · Other" in row)
        self.assertIn('bar lg idle', codex)
        self.assertNotIn("pool running", codex)
        grok = next(row for row in cards if "Grok" in row)
        self.assertIn("1 running", grok)
        self.assertIn("unmetered", grok)
        self.assertIn('<i class="stripe on"></i>', grok)
        muse = next(row for row in cards if "Muse" in row)
        self.assertIn("waiting · no proxy subscriptions", muse)
        self.assertIn("unmetered", muse)
        self.assertIn('<span>subscriptions</span><b>0</b>', muse)
        self.assertIn("0 running", muse)
        self.assertNotIn("pool running", muse)
        self.assertNotIn('<i class="stripe on"></i>', muse)
        self.assertNotIn('data-fold="rotation"', body)

    def test_rendered_capacity_preserves_host_pressure_and_queued_work(self) -> None:
        plan = _plan(_subscription("claude", "Personal"),
                     _subscription("grok", "d", unmetered=True),
                     host_pressure={"reason": "fixture pressure"})
        inflight = [
            {"ts": "2033-05-18T03:33:20Z", "task": provider + "-job",
             "title": provider.title() + " job", "engine": provider, "cwd": "/unused"}
            for provider in ("claude", "grok")
        ]
        body = self._bonus_body(self.QUEUE_FIXTURE, plan=plan, inflight=inflight)
        self.assertIn("waiting · host pressure", body)
        self.assertIn('id="qlist"', body)
        self.assertIn("Weekly Claude sweep", body)
        cards = re.findall(r'<details class="arow">.*?</details>', body, re.DOTALL)
        for name in ("Claude · Personal", "Grok"):
            row = next(row for row in cards if name in row)
            self.assertIn('<span class="n2 warn">waiting · host pressure</span>', row)
            self.assertNotIn("pool running 1", row)
        claude = next(row for row in cards if "Claude · Personal" in row)
        self.assertIn('bar lg draining', claude)

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

        # The original facets stay together. Readiness and work-group navigation occupy a
        # dedicated second row, rather than joining the primary row and making it wrap.
        primary = bar[bar.index('qfilter-primary'):bar.index('qfilter-secondary')]
        secondary = bar[bar.index('qfilter-secondary'):bar.index('id="qfreset"')]
        self.assertIn('data-group="kind"', primary)
        self.assertIn('data-group="size"', primary)
        self.assertNotIn('data-group="state"', primary)
        self.assertNotIn('data-group="workgroup"', primary)
        self.assertIn('qfilter-secondary', secondary)
        self.assertNotIn('data-group="mode"', bar)

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

    def test_provider_filter_includes_waiting_tasks_not_currently_eligible(self) -> None:
        remaining = [
            dict(
                self.QUEUE_FIXTURE[0],
                readiness={"state": "ready", "ready": True},
                compatible_providers=["claude"],
            ),
            {
                "id": "waiting-claude", "title": "Waiting Claude child", "kind": "oneoff",
                "priority": 2, "cadence": None, "cwd": "/tmp/wait", "goal": "wait",
                "size": "small", "last_ts": None, "eligible_providers": [],
                "compatible_providers": ["claude"],
                "readiness": {"state": "waiting", "ready": False, "reason": "Waiting for parent"},
            },
            dict(self.QUEUE_FIXTURE[1], readiness={"state": "ready", "ready": True}),
        ]
        body = self._bonus_body(remaining)
        bar = body[body.index('id="qfilters"'):body.index('id="qlist"')]
        waiting_at = body.index("Waiting Claude child")
        waiting_row = body[body.rfind('<div class="qrow"', 0, waiting_at):waiting_at]
        self.assertIn('data-providers="claude"', waiting_row)
        self.assertIn(
            'data-value="claude" aria-pressed="false" '
            'aria-label="Claude" title="Claude">'
            + self.viewer.ico("claude") + "<i>2</i>",
            bar,
        )
        self.assertIn(
            'data-value="codex" aria-pressed="false" '
            'aria-label="Codex" title="Codex">'
            + self.viewer.ico("codex") + "<i>1</i>",
            bar,
        )

    def test_provider_filter_keeps_waiting_rows_in_a_browser(self) -> None:
        chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if chrome is None:
            self.skipTest("Chrome is unavailable")
        remaining = [
            dict(
                self.QUEUE_FIXTURE[0],
                compatible_providers=["claude"],
                readiness={"state": "ready", "ready": True},
            ),
            {
                "id": "waiting-claude", "title": "Waiting Claude child", "kind": "oneoff",
                "priority": 2, "cadence": None, "cwd": "/tmp/wait", "goal": "wait",
                "size": "small", "last_ts": None, "eligible_providers": [],
                "compatible_providers": ["claude"],
                "readiness": {"state": "waiting", "ready": False, "reason": "Waiting for parent"},
            },
            dict(self.QUEUE_FIXTURE[1], readiness={"state": "ready", "ready": True}),
        ]
        with (
            mock.patch.object(self.viewer, "render_bonus_body",
                              return_value=self._bonus_body(remaining)),
            mock.patch.object(self.viewer, "render_schedule_body", return_value="scheduled body"),
        ):
            page = self.viewer.render_page().decode()

        with tempfile.TemporaryDirectory() as work:
            target = Path(work) / "page.html"
            target.write_text(
                page + '<script>document.querySelector('
                '\'.fchip[data-group="provider"][data-value="claude"]\').click();</script>'
            )
            completed = subprocess.run(
                [
                    chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                    "--disable-dev-shm-usage", "--no-proxy-server",
                    f"--user-data-dir={work}/profile", "--dump-dom", target.as_uri(),
                ],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        dom = completed.stdout
        waiting_at = dom.index("Waiting Claude child")
        waiting_row = dom[dom.rfind('<div class="qrow"', 0, waiting_at):waiting_at]
        codex_at = dom.index("One-off Codex refactor")
        codex_row = dom[dom.rfind('<div class="qrow"', 0, codex_at):codex_at]
        self.assertNotIn("hidden", waiting_row)
        self.assertIn("hidden", codex_row)
        self.assertIn(">2 of 3 jobs<", dom)

    def test_single_value_facets_and_an_empty_queue_render_no_dead_controls(self) -> None:
        uniform = [dict(t, priority=2, kind="oneoff", cadence=None, size="small",
                        eligible_providers=["claude"], compatible_providers=["claude"])
                   for t in self.QUEUE_FIXTURE]
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
        self.assertIn("No queued work", drained)

    def test_readiness_and_work_group_filters_have_a_second_row(self) -> None:
        remaining = [
            dict(self.QUEUE_FIXTURE[0], readiness={"state": "ready"}, work_group="Research"),
            dict(self.QUEUE_FIXTURE[1], readiness={"state": "waiting"}, work_group="Quality"),
            dict(self.QUEUE_FIXTURE[2], readiness={"state": "cooldown"}, work_group="Quality"),
        ]
        body = self._bonus_body(remaining)
        bar = body[body.index('id="qfilters"'):body.index('id="qlist"')]
        primary = bar[bar.index('qfilter-primary'):bar.index('qfilter-secondary')]
        secondary = bar[bar.index('qfilter-secondary'):bar.index('id="qfreset"')]

        self.assertIn('role="group" aria-label="filter by readiness"', secondary)
        self.assertIn('data-value="ready"', secondary)
        self.assertIn('data-value="waiting"', secondary)
        self.assertIn('data-value="cooldown"', secondary)
        self.assertIn('aria-label="Ready" title="Ready">Ready', secondary)
        self.assertIn('aria-label="Waiting" title="Waiting">Waiting', secondary)
        self.assertIn('aria-label="Cool Down" title="Cool Down">Cool Down', secondary)
        self.assertIn('role="group" aria-label="filter by work group"', secondary)
        self.assertIn('data-value="Research"', secondary)
        self.assertIn('data-value="Quality"', secondary)
        self.assertNotIn('data-group="state"', primary)
        self.assertNotIn('data-group="workgroup"', primary)

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
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
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

    def test_mobile_folds_preserve_header_and_usage_with_no_rotation(self) -> None:
        """Phone: the header and usage start closed behind their own headers. Desktop CSS
        never hides the bodies, so the wrappers must not become the layout."""
        body = self._bonus_body(self.QUEUE_FIXTURE)
        self.assertIn('class="mfold mfold-wrap" data-fold="header"', body)
        self.assertIn('class="hd mfold-sum"', body)
        self.assertNotIn('class="work-summary"', body)
        self.assertNotIn('data-fold="rotation"', body)
        self.assertIn('class="rows mfold" data-fold="drain"', body)
        self.assertIn('class="rowhd mfold-sum"', body)
        header_at = body.index('data-fold="header"')
        drain_at = body.index('data-fold="drain"')
        remaining_at = body.index("queued · priority order")
        self.assertNotIn('class="vbar', body)
        self.assertLess(header_at, drain_at)
        self.assertLess(drain_at, remaining_at)
        self.assertNotIn('class="sec capacity-fold"', body)
        self.assertNotIn('class="task-edit"', body)
        self.assertNotIn('id="work-editor"', body)
        self.assertIn("<i class=\"caret\"></i>usage · provider capacity", body)
        self.assertNotIn("<i class=\"caret\"></i>rotation", body)
        self.assertIn("async scheduler", body)
        self.assertNotIn("bonus scheduler", body)
        self.assertIn("next ", body)
        self.assertNotIn("Grok eligible", body)
        drain_hdr = body[body.index("usage · provider capacity"):body.index("queued · priority order")]
        self.assertIn('class="rowplan"', drain_hdr)
        self.assertIn('class="nxt"', drain_hdr)
        self.assertIn('href="#i-claude"', drain_hdr)
        self.assertIn('href="#i-codex"', drain_hdr)
        self.assertIn('href="#i-grok"', drain_hdr)
        self.assertNotIn("eligible", drain_hdr)
        self.assertIn(".rowplan{display:inline-flex;align-items:center", self.viewer.CSS)
        self.assertIn(".rowhd .nxt .ico{width:1.25em;height:1.25em", self.viewer.CSS)

    def test_queued_jobs_show_a_next_scout_chip_when_planned(self) -> None:
        now = 2_000_000_000
        scout_at = now + 30 * 60
        plan = _plan(allocations=[{
            "task_id": "weekly-claude", "provider_id": "claude", "account_id": "claude",
        }])
        with (
            mock.patch.object(self.viewer.time, "time", return_value=now),
            mock.patch.object(self.viewer, "next_scout", return_value=scout_at),
        ):
            body = self._bonus_body(self.QUEUE_FIXTURE, plan=plan)
        claude_row = body[body.index("Weekly Claude sweep"):body.index("One-off Codex refactor")]
        self.assertIn('class="qkick"', claude_row)
        self.assertIn('href="#i-claude"', claude_row)
        self.assertIn("in 30m", claude_row)
        codex_row = body[body.index("One-off Codex refactor"):body.index("One-off Claude audit")]
        self.assertNotIn('class="qkick"', codex_row)

        css, script = self.viewer.CSS, self.viewer.SCRIPT
        self.assertIn(".mfold-sum .caret{display:none}", css)
        self.assertIn("@media(max-width:640px)", css)
        self.assertIn(".mfold:not(.open)>.mfold-body{display:none}", css)
        self.assertIn(".mfold-wrap{display:contents}", css)
        self.assertIn("bonusMobileFolds", script)
        self.assertIn("max-width:640px", script)

    def test_queue_rows_keep_the_full_goal_for_the_mobile_modal(self) -> None:
        long_goal = ("x" * 200) + " <script>alert(1)</script>"
        body = self._bonus_body([dict(self.QUEUE_FIXTURE[0], title="Long goal job", goal=long_goal)])
        self.assertIn('id="qmodal"', body)
        self.assertIn('aria-labelledby="qmodal-title"', body)
        self.assertIn('class="qdesc" hidden>', body)
        self.assertIn(".qmodal::backdrop", self.viewer.CSS)
        self.assertIn("qmodal-title", self.viewer.SCRIPT)

        row_at = body.index('class="qtitle">Long goal job')
        row = body[row_at:body.index('id="qnone"', row_at)]
        desc_open = row.index('class="qdesc" hidden>')
        desc = row[desc_open:row.index("</div>", desc_open)]
        self.assertIn("x" * 200, desc)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", desc)
        self.assertNotIn("<script>alert(1)</script>", desc)

        sub_open = row.index('class="qsub"')
        sub_text = row[row.index(">", sub_open) + 1:row.index("</div>", sub_open)]
        self.assertTrue(sub_text.endswith("…"))
        self.assertLess(len(sub_text), 180)

    def test_tapping_a_mobile_fold_header_opens_it_in_a_browser(self) -> None:
        chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if chrome is None:
            self.skipTest("Chrome is unavailable")
        with (
            mock.patch.object(self.viewer, "render_bonus_body",
                              return_value=self._bonus_body(self.QUEUE_FIXTURE)),
            mock.patch.object(self.viewer, "render_schedule_body", return_value="scheduled body"),
        ):
            page = self.viewer.render_page().decode()
        # dump-dom's viewport is desktop-sized, so the production matchMedia would treat
        # this as desktop and refuse to toggle. Force the phone branch; the CSS hide is
        # already under the 640px breakpoint, so the class is what we can assert here.
        page = page.replace(
            "<head>",
            "<head><script>window.matchMedia=function(q){"
            "return{matches:String(q).indexOf('max-width')>=0,media:q,"
            "addEventListener:function(){},removeEventListener:function(){}}}"
            "</script>",
            1,
        )

        def dump(extra: str) -> str:
            with tempfile.TemporaryDirectory() as work:
                target = Path(work) / "page.html"
                target.write_text(page + f"<script>{extra}</script>")
                completed = subprocess.run(
                    [
                        chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                        "--disable-dev-shm-usage", "--no-proxy-server",
                        f"--user-data-dir={work}/profile", "--dump-dom", target.as_uri(),
                    ],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return completed.stdout

        def fold(dom: str, name: str) -> str:
            body_at = dom.index("<body")
            marker = f'data-fold="{name}"'
            hit = dom.index(marker, body_at)
            start = dom.rfind("<div", body_at, hit)
            later = [dom.find(f'data-fold="{other}"', hit + 1) for other in
                     ("header", "drain") if other != name]
            later = [i for i in later if i >= 0]
            return dom[start:min(later) if later else hit + 400]

        closed = dump("")
        for name in ("header", "drain"):
            with self.subTest(fold=name, state="closed"):
                markup = fold(closed, name)
                self.assertNotRegex(markup, r'class="[^"]*\bopen\b')
                self.assertIn('aria-expanded="false"', markup)

        opened = dump("".join(
            f"document.querySelector('.mfold[data-fold=\"{name}\"]>.mfold-sum').click();"
            for name in ("header", "drain")
        ))
        for name in ("header", "drain"):
            with self.subTest(fold=name, state="open"):
                markup = fold(opened, name)
                self.assertRegex(markup, r'class="[^"]*\bopen\b')
                self.assertIn('aria-expanded="true"', markup)

        persisted = dump(
            "document.querySelector('.mfold[data-fold=\"header\"]>.mfold-sum').click();"
            "document.documentElement.setAttribute('data-header-fold',"
            "JSON.parse(localStorage.getItem('bonusMobileFolds')||'{}').header?'1':'0');"
        )
        self.assertIn('data-header-fold="1"', persisted)

    def test_tapping_a_mobile_queue_row_opens_the_description_modal_in_a_browser(self) -> None:
        chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if chrome is None:
            self.skipTest("Chrome is unavailable")
        with (
            mock.patch.object(self.viewer, "render_bonus_body",
                              return_value=self._bonus_body(self.QUEUE_FIXTURE)),
            mock.patch.object(self.viewer, "render_schedule_body", return_value="scheduled body"),
        ):
            page = self.viewer.render_page().decode()
        mobile_page = page.replace(
            "<head>",
            "<head><script>window.matchMedia=function(q){"
            "return{matches:String(q).indexOf('max-width')>=0,media:q,"
            "addEventListener:function(){},removeEventListener:function(){}}}"
            "</script>",
            1,
        )

        def dump(html: str, extra: str) -> str:
            with tempfile.TemporaryDirectory() as work:
                target = Path(work) / "page.html"
                target.write_text(html + f"<script>{extra}</script>")
                completed = subprocess.run(
                    [
                        chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                        "--disable-dev-shm-usage", "--no-proxy-server",
                        f"--user-data-dir={work}/profile", "--dump-dom", target.as_uri(),
                    ],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return completed.stdout

        def dialog(dom: str) -> str:
            start = dom.index('id="qmodal"')
            start = dom.rfind("<dialog", 0, start)
            return dom[start:dom.index("</dialog>", start) + len("</dialog>")]

        opened = dump(
            mobile_page,
            "document.querySelector('.qrow .qtitle').click();",
        )
        markup = dialog(opened)
        self.assertIn(" open", markup)
        self.assertIn("Weekly Claude sweep", markup)
        self.assertIn("sweep", markup[markup.index('id="qmodal-body"'):])
        self.assertIn('role="button"', opened[opened.index('class="qtitle">Weekly Claude sweep') - 80:])

        controls = dump(
            mobile_page,
            "document.querySelector('.qrow .qact').click();",
        )
        self.assertNotIn(" open", dialog(controls))

        desktop = dump(
            page,
            "document.querySelector('.qrow .qtitle').click();",
        )
        self.assertNotIn(" open", dialog(desktop))
        self.assertNotIn('role="button"', desktop[desktop.index('class="qtitle">Weekly Claude sweep') - 80:])

        closed = dump(
            mobile_page,
            "document.querySelector('.qrow .qtitle').click();"
            "document.querySelector('.qmodal-x').click();",
        )
        self.assertNotIn(" open", dialog(closed))


class RootBlockerViewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.viewer = _load_server()

    @staticmethod
    def _waiting(root_blocker: object) -> dict[str, object]:
        return {
            "id": "C", "title": "Publish report", "work_group": None, "source_ref": None,
            "readiness": {
                "state": "waiting",
                "reason": "Waiting for B",
                "hold_reason": None,
                "dependencies": [{
                    "id": "B", "title": "Build report", "status": "queued",
                    "satisfied": False, "satisfy": "done", "detail": None,
                }],
                "checks": [],
                "root_blocker": root_blocker,
            },
        }

    def test_work_meta_renders_root_blocker(self) -> None:
        html = self.viewer._work_meta(self._waiting({
            "task_id": "A", "title": "Fetch <actor>", "status": "held",
            "reason": "Missing GitHub test actor",
        }))
        self.assertIn("Root blocker:", html)
        self.assertIn("<code>A</code>", html)
        self.assertIn("Fetch &lt;actor&gt;", html)
        self.assertNotIn("Fetch <actor>", html)
        self.assertIn("held", html)
        self.assertIn("Missing GitHub test actor", html)
        self.assertLess(html.index("Root blocker:"), html.index("prerequisites complete"))

        # Liveness: a task with no root blocker renders exactly as before.
        plain = self.viewer._work_meta(self._waiting(None))
        self.assertNotIn("Root blocker:", plain)
        self.assertIn("0/1 prerequisites complete", plain)


class CheckReadinessViewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.viewer = _load_server()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(os.path.realpath(temporary.name))

    def test_never_evaluated_check_renders_not_checked_yet(self) -> None:
        from bonus_drain import checks, db

        from tests.test_bonus_dependency_recovery import NOW, task

        queue = db.QueueDB(self.root / "queue.db")
        queue.initialize()
        spec = {"type": "issue_open", "repo": "owner/repo", "number": 12}
        added = queue.add_task(reviewed(task("fresh", self.root, checks=[spec])))
        readiness = queue.readiness("fresh", now_epoch=NOW + 10)

        html = self.viewer._work_meta({**added.to_dict(), "readiness": readiness})

        self.assertIn(f"Not checked yet: {checks.describe(spec)}", html)
        self.assertNotIn("Waiting for preflight check", html)
        self.assertNotIn("Check failed", html)


if __name__ == "__main__":
    unittest.main()
