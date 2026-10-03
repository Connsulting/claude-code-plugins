"""Scheduling, dispatch, CLI, and notification behavior for preflight gates.

External gh/git behavior uses the recorded-shape ``FakeRunner`` from
``tests.test_bonus_drain_preflight_checks`` (captured 2026-10-02 with gh/git on this
host, see plan Recorded tool output shapes).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from tests.test_bonus_dependency_recovery import (
    KEY, NOW, captured_json, iso, reason, rows, runtime,
)
from tests.test_bonus_drain_preflight_checks import (
    GH_OFFLINE_STDERR, ISSUE_2855_CLOSED, ISSUE_3815_OPEN, ISSUE_OPEN_2855, ISSUE_OPEN_3815,
    ISSUE_OPEN_IN_V0_10_0, LS_REMOTE_MAIN, PR_2994_INTO_EPIC, PR_2994_MERGED, RELEASE_0_11_1,
    FakeRunner, PreflightCase, authority, exited, add_goal, gh_issue, gh_pr, gh_release,
    git_ls_remote, handoff,
    in_milestone, ok, table_counts, with_fields,
)
from tests.test_bonus_drain_scout_inflight import (
    HOUR, _multi_account_config, _multi_snapshots, _open_snapshots, _task as provider_task,
    _two_provider_config,
)
from bonus_drain import checks, cli, db, dispatcher, notifications, scout, usage
from bonus_drain import config as config_module

UNSWITCHED = "requested account did not become active"
# The exact agent-router diagnostic for a Codex daemon that never started (no thread launched).
CODEX_DAEMON_TIMEOUT = (
    b"agent-router: `/home/user/.local/bin/codex app-server daemon start` timed out after 10s\n"
)


def alpha_snapshots(now: int = NOW) -> dict[tuple[str, str], usage.UsageSnapshot]:
    return {
        ("alpha", "alpha-account"): usage.UsageSnapshot(
            "alpha", "alpha-account", now,
            {"alpha-weekly": {"used_percent": 70, "resets_at": now + 5_000}},
        ),
    }


def launched_router(calls: list[list[str]] | None = None):
    record = calls if calls is not None else []

    def route(argv: list[str], **_kwargs: object) -> dict[str, object]:
        record.append(list(argv))
        if "--dry-run" in argv:
            return {"provider_id": "alpha"}
        return {"dispatch": {"job_id": f"job-{len(record)}", "launched": True}}

    return route


def backoff_rows(queue: db.QueueDB) -> list[tuple[str, str, str, int]]:
    return [
        (row["provider_id"], row["account_id"], row["cause"], row["failures"])
        for row in rows(queue, "SELECT * FROM account_backoff ORDER BY provider_id,account_id")
    ]


def not_before(queue: db.QueueDB, provider_id: str, account_id: str) -> float:
    row = rows(
        queue, "SELECT not_before FROM account_backoff WHERE provider_id=? AND account_id=?",
        (provider_id, account_id),
    )[0]
    return db._timestamp_epoch(row["not_before"])


def unswitched_activation(action: str, _account_id: str) -> None:
    if action == "activate":
        raise dispatcher.ActivationUnavailable(UNSWITCHED, known_not_switched=True)


class HermeticEnvironment:
    """Keep CLI config resolution inside the test directory."""

    def hermetic(self, home: Path) -> None:
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "XDG_CACHE_HOME": str(home / ".cache"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)  # type: ignore[attr-defined]
        os.environ.pop("BONUS_DRAIN_CONFIG", None)


class AccountHoldDispatchTests(PreflightCase):
    def dispatch(self, task_id: str, cfg, **kwargs: Any):
        kwargs.setdefault("router_call", launched_router())
        kwargs.setdefault("check_runner", self.runner())
        return dispatcher.dispatch(
            cfg, self.queue, task_id=task_id, eligibility_key=f"manual/{task_id}",
            requested_provider=kwargs.pop("provider", "alpha"), **kwargs,
        )

    def test_injected_unswitched_activation_holds_account_not_task(self) -> None:
        cfg = runtime(self.queue.path, activation=True)
        self.add("held")
        router = mock.Mock()

        with (
            mock.patch.object(db.time, "time", return_value=NOW),
            self.assertRaises(dispatcher.ActivationUnavailable) as raised,
        ):
            self.dispatch("held", cfg, router_call=router, activation_call=unswitched_activation)

        router.assert_not_called()
        hold = raised.exception.account_hold
        self.assertEqual(
            (hold.provider_id, hold.account_id, hold.cause),
            ("alpha", "alpha-account", "activation_unswitched"),
        )
        self.assertEqual(self.queue.attempts(task_id="held"), [])
        self.assertIsNone(self.queue.claim_for("held"))
        self.assertEqual(backoff_rows(self.queue), [("alpha", "alpha-account", "activation_unswitched", 1)])
        self.assertEqual(not_before(self.queue, "alpha", "alpha-account"), NOW + 1_800)
        self.assertTrue(self.queue.readiness("held", now_epoch=NOW)["ready"])

    def test_lease_managed_unswitched_activation_holds_account(self) -> None:
        base = runtime(self.queue.path)
        active = self.root / "active"
        active.write_text("Personal\n", encoding="utf-8")
        switch = config_module.AdapterConfig(
            "switch", "activation",
            (str(self.root / "bin" / "account-switch"), "--label", "Personal", "--active-path", str(active)),
        )
        account = replace(base.accounts[0], activation_adapter_id="switch", activation_scope="launch")
        cfg = replace(base, adapters=(*base.adapters, switch), accounts=(account,))
        self.add("held")

        def boom(_cfg: object, _account: object, action: str, _callback: object) -> None:
            if action == "activate":
                # The exact adapter text for a rotation the adapter proved never happened.
                raise dispatcher.DispatchError(
                    "account activation activate failed: adapter switch exited 1: "
                    f"bonus-drain-account-activation: {UNSWITCHED}"
                )

        with (
            mock.patch.object(dispatcher, "_activation", side_effect=boom),
            mock.patch.object(db.time, "time", return_value=NOW),
            self.assertRaises(dispatcher.ActivationUnavailable) as raised,
        ):
            self.dispatch("held", cfg)

        self.assertEqual(raised.exception.account_hold.cause, "activation_unswitched")
        self.assertEqual(self.queue.attempts(task_id="held"), [])
        self.assertIsNone(self.queue.claim_for("held"))
        self.assertEqual(self.queue.activation_leases(provider_id="alpha"), [])
        self.assertEqual(backoff_rows(self.queue), [("alpha", "alpha-account", "activation_unswitched", 1)])
        self.assertTrue(db.doctor(self.queue).ok)

    def test_codex_daemon_timeout_holds_launcher(self) -> None:
        codex = config_module.ProviderConfig(
            "codex", config_module.DispatchBinding("router", "codex"), frozenset(), "single",
        )
        cfg = replace(runtime(self.queue.path), providers=(codex,), plans=(), accounts=(), limits=())
        self.add("daemon")
        rejected = subprocess.CompletedProcess([], 1, b"", CODEX_DAEMON_TIMEOUT)

        with (
            mock.patch("bonus_drain.adapters.run_bounded_process", return_value=rejected),
            mock.patch.object(db.time, "time", return_value=NOW),
            self.assertRaises(dispatcher.ProviderLaunchUnavailable) as raised,
        ):
            dispatcher.dispatch(
                cfg, self.queue, task_id="daemon", eligibility_key="manual/daemon",
                requested_provider="codex", check_runner=self.runner(),
            )

        self.assertIsInstance(raised.exception, dispatcher.KnownDispatchFailure)
        self.assertIn("codex app-server daemon start", str(raised.exception))
        hold = raised.exception.account_hold
        self.assertEqual((hold.provider_id, hold.account_id, hold.cause), ("codex", "*", "codex_daemon_start"))
        self.assertEqual(self.queue.attempts(task_id="daemon"), [])
        self.assertIsNone(self.queue.claim_for("daemon"))
        self.assertEqual(backoff_rows(self.queue), [("codex", "*", "codex_daemon_start", 1)])

    def test_backoff_doubles_on_repeated_failure(self) -> None:
        cfg = runtime(self.queue.path, activation=True)
        self.add("flapping")
        deltas = []
        for index in range(5):
            moment = NOW + index * 20_000
            with (
                mock.patch.object(db.time, "time", return_value=moment),
                self.assertRaises(dispatcher.ActivationUnavailable),
            ):
                self.dispatch(
                    "flapping", cfg, activation_call=unswitched_activation, now_epoch=moment,
                )
            deltas.append(not_before(self.queue, "alpha", "alpha-account") - moment)
        self.assertEqual(deltas, [1_800, 3_600, 7_200, 14_400, 14_400])
        self.assertEqual(backoff_rows(self.queue), [("alpha", "alpha-account", "activation_unswitched", 5)])
        self.assertEqual(self.queue.attempts(task_id="flapping"), [])

    def test_successful_dispatch_clears_backoff(self) -> None:
        cfg = runtime(self.queue.path, activation=True)
        self.add("flapping")
        with (
            mock.patch.object(db.time, "time", return_value=NOW),
            self.assertRaises(dispatcher.ActivationUnavailable),
        ):
            self.dispatch("flapping", cfg, activation_call=unswitched_activation)
        self.assertEqual(len(backoff_rows(self.queue)), 1)

        with mock.patch.object(db.time, "time", return_value=NOW + 60):
            result = self.dispatch("flapping", cfg, activation_call=lambda *_args: None)

        self.assertEqual(result.account_id, "alpha-account")
        self.assertEqual(backoff_rows(self.queue), [])
        self.assertEqual([item.state for item in self.queue.attempts(task_id="flapping")], ["dispatched"])


class AccountHoldScoutTests(PreflightCase):
    def setUp(self) -> None:
        super().setUp()
        self.active = self.root / "active"
        self.active.write_text("Personal\n", encoding="utf-8")
        self.config = _multi_account_config(self.queue, self.root / "cache", self.active)
        self.events: list[tuple[str, str]] = []
        self.routed: list[str] = []
        self.personal_refusal = UNSWITCHED

    def activate(self, action: str, account_id: str) -> None:
        self.events.append((action, account_id))
        if action == "activate" and account_id == "alpha-personal" and self.personal_refusal:
            raise dispatcher.ActivationUnavailable(self.personal_refusal, known_not_switched=True)

    def route(self, argv: list[str], **_kwargs: object) -> dict[str, object]:
        self.routed.append(argv[argv.index("--provider") + 1])
        return {"dispatch": {"job_id": f"job-{len(self.routed)}", "launched": True}}

    def tick(self, now: int, *, business_used: float = 70, config=None, queue=None) -> scout.ScoutReport:
        with (
            mock.patch.object(scout, "read_all", return_value=_multi_snapshots(70, business_used)),
            mock.patch.object(db.time, "time", return_value=now),
        ):
            return scout.run_once(
                config or self.config, queue or self.queue, now_epoch=now,
                activation_call=self.activate, router_call=self.route,
                check_runner=self.runner(),
            )

    def attempt_states(self, task_id: str) -> list[str]:
        return [item.state for item in self.queue.attempts(task_id=task_id)]

    def finish(self, task_id: str, now: int) -> None:
        claim = self.queue.claim_for(task_id)
        self.queue.record(
            task_id, claim.eligibility_key, attempt_id=claim.attempt_id, status="failed",
            outcome=reason(), provider_id=claim.provider_id, account_id=claim.account_id,
            timestamp=iso(now), now_epoch=now,
        )

    def test_scout_uses_sibling_account_during_backoff(self) -> None:
        self.queue.add_task(provider_task("alpha-one", "alpha"))

        first = self.tick(NOW)
        self.assertEqual(first.dispatched, ())
        self.assertEqual(first.errors, ())
        self.assertEqual(list(first.account_holds), [{
            "task_id": "alpha-one", "provider_id": "alpha",
            "account_id": "alpha-personal", "cause": "activation_unswitched",
        }])
        self.assertEqual(self.queue.attempts(task_id="alpha-one"), [])

        second = self.tick(NOW + 600)
        self.assertIn(
            "account backoff until", second.plan.closed[("alpha", "alpha-personal")],
        )
        self.assertEqual([item.account_id for item in second.dispatched], ["alpha-business"])
        self.assertEqual(self.attempt_states("alpha-one"), ["dispatched"])
        self.assertNotIn(("activate", "alpha-personal"), self.events[1:])

    def test_no_consecutive_infra_aborts_across_ticks(self) -> None:
        self.queue.add_task(provider_task("alpha-one", "alpha"))
        reports = [
            self.tick(moment, business_used=80)
            for moment in (NOW, NOW + 600, NOW + 1_801)
        ]
        for report in reports:
            self.assertEqual(report.errors, ())
            self.assertEqual(report.dispatched, ())
        self.assertEqual([len(report.account_holds) for report in reports], [1, 0, 1])
        self.assertNotIn("aborted", self.attempt_states("alpha-one"))
        self.assertEqual(self.queue.attempts(task_id="alpha-one"), [])
        self.assertEqual(
            backoff_rows(self.queue), [("alpha", "alpha-personal", "activation_unswitched", 2)],
        )
        self.assertEqual(
            [event for event in self.events if event[0] == "activate"],
            [("activate", "alpha-personal"), ("activate", "alpha-personal")],
        )

    def test_provider_launch_unavailable_breaks_only_its_batch(self) -> None:
        config = _two_provider_config(self.queue, self.root / "cache")
        for task_id, provider in (("alpha-one", "alpha"), ("alpha-two", "alpha"), ("beta-one", "beta")):
            self.queue.add_task(provider_task(task_id, provider))
        routed: list[str] = []

        def route(argv: list[str], **_kwargs: object):
            provider = argv[argv.index("--provider") + 1]
            routed.append(provider)
            if provider == "alpha":
                return subprocess.CompletedProcess([], 1, b"", CODEX_DAEMON_TIMEOUT)
            return {"dispatch": {"job_id": "beta-job", "launched": True}}

        with (
            mock.patch.object(scout, "read_all", return_value=_open_snapshots()),
            mock.patch.object(db.time, "time", return_value=NOW),
        ):
            report = scout.run_once(
                config, self.queue, now_epoch=NOW, router_call=route, check_runner=self.runner(),
            )

        self.assertEqual(routed.count("alpha"), 1)
        self.assertEqual([item.provider_id for item in report.dispatched], ["beta"])
        self.assertEqual(report.errors, ())
        self.assertEqual(
            [(hold["provider_id"], hold["account_id"], hold["cause"]) for hold in report.account_holds],
            [("alpha", "alpha-account", "codex_daemon_start")],
        )
        for task_id in ("alpha-one", "alpha-two"):
            self.assertEqual(self.queue.attempts(task_id=task_id), [])

    def test_account_hold_never_cycles_health_notices(self) -> None:
        config = replace(self.config, scout_ntfy_url="https://ntfy.example.test/bonus-drain")
        self.queue.add_task(provider_task("alpha-one", "alpha"))

        def stuck_rows(queue: db.QueueDB) -> list[int]:
            return [row["stuck"] for row in rows(queue, "SELECT stuck FROM scout_notification_state")]

        with mock.patch.object(notifications, "urlopen") as urlopen:
            first = self.tick(NOW, config=config)
            self.assertNotIn(1, stuck_rows(self.queue))
            second = self.tick(NOW + 600, config=config)
            self.assertNotIn(1, stuck_rows(self.queue))
            self.assertEqual([item.task_id for item in second.dispatched], ["alpha-one"])
            self.finish("alpha-one", NOW + 900)
            self.queue.add_task(provider_task("alpha-two", "alpha"))
            third = self.tick(NOW + 1_801, config=config)
            self.assertNotIn(1, stuck_rows(self.queue))

        urlopen.assert_not_called()
        for report in (first, second, third):
            self.assertEqual(report.errors, ())
        self.assertEqual([len(report.account_holds) for report in (first, second, third)], [1, 0, 1])
        self.assertEqual(third.account_holds[0]["task_id"], "alpha-two")
        self.assertEqual(
            backoff_rows(self.queue), [("alpha", "alpha-personal", "activation_unswitched", 2)],
        )

        # Liveness: contention (no proven unswitched account) is still a scout error and alerts.
        contention_root = self.mkdir("contention")
        queue = db.QueueDB(contention_root / "queue.db")
        queue.initialize()
        queue.add_task(provider_task("alpha-busy", "alpha"))
        contention = replace(config, database=queue.path)
        self.personal_refusal = "active work refused rotation"
        with mock.patch.object(notifications, "urlopen") as urlopen:
            refused = self.tick(NOW, config=contention, queue=queue)
        self.assertEqual([error["task_id"] for error in refused.errors], ["alpha-busy"])
        self.assertEqual(refused.account_holds, ())
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual([item.state for item in queue.attempts(task_id="alpha-busy")], ["aborted"])


class DependencyEdgeCliTests(PreflightCase):
    def setUp(self) -> None:
        super().setUp()
        self.pr_repo = self.mkdir("pr-repo")
        self.other = self.mkdir("other")
        self.cfg = replace(runtime(self.queue.path), pr_exceptions=(
            {"path": str(self.pr_repo), "allow_push": True, "allow_pr": True},
        ))
        self.add("pr-parent", cwd=str(self.pr_repo))
        self.add("pr-parent-2", cwd=str(self.pr_repo))
        self.add("plain-parent", cwd=str(self.other))

    def default_runner(self) -> FakeRunner:
        return self.runner()

    def run_cli(self, *argv: str, fake: FakeRunner | None = None) -> tuple[int, list[object]]:
        # add and edit evaluate checks; keep every tool call on a recorded-shape fake whose
        # cleanup asserts no unexpected argv reached it.
        fake = fake if fake is not None else self.default_runner()
        with (
            mock.patch.object(cli, "_queue", return_value=(self.cfg, self.queue)),
            mock.patch.object(checks, "subprocess_runner", fake),
            captured_json() as payloads,
        ):
            code = cli.main(list(argv))
        return code, payloads

    def add_cli(self, task_id: str, *extra: str) -> tuple[int, list[object]]:
        return self.run_cli(
            "add", "--database", str(self.queue.path), "--id", task_id, "--title", task_id,
            "--kind", "oneoff", "--size", "small", "--cwd", str(self.root),
            "--goal", f"complete {task_id}", "--json", *extra,
        )

    def test_add_defaults_merged_for_pr_repository_parent(self) -> None:
        code, payloads = self.add_cli("child", "--depends-on", "pr-parent")
        self.assertEqual(code, 0, payloads)
        child = self.queue.task("child")
        self.assertEqual(child.depends_on, ("pr-parent",))
        self.assertEqual(child.merged_depends_on, ("pr-parent",))
        self.assertEqual(payloads[0]["task"]["merged_depends_on"], ["pr-parent"])

    def test_add_defaults_done_elsewhere(self) -> None:
        code, payloads = self.add_cli("child", "--depends-on", "plain-parent")
        self.assertEqual(code, 0, payloads)
        self.assertEqual(self.queue.task("child").depends_on, ("plain-parent",))
        self.assertEqual(self.queue.task("child").merged_depends_on, ())

    def test_suffix_overrides_default(self) -> None:
        code, payloads = self.add_cli("child", "--depends-on", "pr-parent:done,plain-parent:merged")
        self.assertEqual(code, 0, payloads)
        child = self.queue.task("child")
        self.assertEqual(child.depends_on, ("plain-parent", "pr-parent"))
        self.assertEqual(child.merged_depends_on, ("plain-parent",))

    def test_bad_suffix_rejected(self) -> None:
        code, _payloads = self.add_cli("child", "--depends-on", "pr-parent:bogus")
        self.assertEqual(code, 2)
        self.assertIsNone(self.queue.task("child"))

    def test_edit_new_edge_defaults_existing_edge_keeps_mode(self) -> None:
        self.assertEqual(self.add_cli("child", "--depends-on", "pr-parent:done")[0], 0)

        code, payloads = self.run_cli(
            "edit", "--database", str(self.queue.path), "child",
            "--changes", json.dumps({"depends_on": ["pr-parent", "pr-parent-2", "plain-parent"]}),
        )

        self.assertEqual(code, 0, payloads)
        child = self.queue.task("child")
        self.assertEqual(child.depends_on, ("plain-parent", "pr-parent", "pr-parent-2"))
        self.assertEqual(child.merged_depends_on, ("pr-parent-2",))

    def test_edit_rejects_explicit_merged_depends_on(self) -> None:
        self.assertEqual(self.add_cli("child", "--depends-on", "plain-parent")[0], 0)

        code, _payloads = self.run_cli(
            "edit", "--database", str(self.queue.path), "child",
            "--changes", json.dumps({"depends_on": ["pr-parent"], "merged_depends_on": ["pr-parent"]}),
        )

        self.assertEqual(code, 2)
        self.assertEqual(self.queue.task("child").depends_on, ("plain-parent",))


class EnqueueValidationCliTests(HermeticEnvironment, PreflightCase):
    def setUp(self) -> None:
        super().setUp()
        self.hermetic(self.mkdir("home"))
        self.project = self.mkdir("project")
        (self.project / ".git").mkdir()
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "airtable": {"command": "airtable-mcp", "env": {"AIRTABLE_API_KEY": "${AIRTABLE_API_KEY}"}},
            "leaky": {"command": "leaky-mcp", "env": {"AIRTABLE_API_KEY": "airtable-key"}},
        }}), encoding="utf-8")
        base = runtime(self.queue.path)
        self.cfg = replace(base, providers=(
            *base.providers,
            config_module.ProviderConfig(
                "claude", config_module.DispatchBinding("router", "claude"), frozenset(), "single",
            ),
            config_module.ProviderConfig(
                "codex", config_module.DispatchBinding("router", "codex"), frozenset(), "single",
            ),
        ))

    def default_runner(self) -> FakeRunner:
        return self.runner({
            git_ls_remote(self.project): ok(LS_REMOTE_MAIN),
            gh_issue(12, "owner/repo"): ok(ISSUE_3815_OPEN),
            gh_release("v1.2.0", "owner/repo"): ok(with_fields(RELEASE_0_11_1, tagName="v1.2.0")),
        })

    def run_cli(self, *argv: str, fake: FakeRunner | None = None) -> tuple[int, list[object]]:
        # add and edit evaluate checks; keep every tool call on a recorded-shape fake whose
        # cleanup asserts no unexpected argv reached it.
        fake = fake if fake is not None else self.default_runner()
        with (
            mock.patch.object(cli, "_queue", return_value=(self.cfg, self.queue)),
            mock.patch.object(checks, "subprocess_runner", fake),
            captured_json() as payloads,
        ):
            code = cli.main(list(argv))
        return code, payloads

    def add_cli(
        self, task_id: str, *extra: str, cwd: Path | None = None, fake: FakeRunner | None = None,
    ) -> tuple[int, list[object]]:
        return self.run_cli(
            "add", "--database", str(self.queue.path), "--id", task_id, "--title", task_id,
            "--kind", "oneoff", "--size", "small", "--cwd", str(cwd or self.project),
            "--goal", f"complete {task_id}", "--json", *extra, fake=fake,
        )

    def assertRefused(self, task_id: str, *extra: str, contains: str | None = None) -> None:
        code, payloads = self.add_cli(task_id, *extra)
        self.assertEqual(code, 2, payloads)
        self.assertIsNone(self.queue.task(task_id))
        if contains is not None:
            self.assertIn(contains, json.dumps(payloads))

    def test_add_rejects_origin_start_ref(self) -> None:
        self.assertRefused("bad", "--start-ref", "origin/main", contains="remote-tracking")

    def test_add_rejects_malformed_check_json(self) -> None:
        self.assertRefused("bad", "--check", "{bad")

    def test_add_rejects_invalid_check_spec(self) -> None:
        self.assertRefused(
            "bad", "--check", json.dumps({"type": "issue_open", "repo": "owner/repo"}), contains="checks",
        )

    def test_add_rejects_unknown_dependency(self) -> None:
        self.assertRefused("bad", "--depends-on", "ghost", contains="ghost")

    def test_add_rejects_mcp_env_literal(self) -> None:
        self.assertRefused("bad", "--mcp", "leaky", contains="NAME")

    def test_add_rejects_unresolvable_mcp_name(self) -> None:
        self.assertRefused("bad", "--mcp", "wiki", contains="wiki")

    def test_add_accepts_valid_task_with_mcp_checks_and_start_ref(self) -> None:
        fake = self.default_runner()
        code, payloads = self.add_cli(
            "valid", "--mcp", "airtable", "--start-ref", "main",
            "--check", json.dumps({"type": "issue_open", "repo": "owner/repo", "number": 12}),
            "--check", json.dumps({"type": "release_exists", "repo": "owner/repo", "tag": "v1.2.0"}),
            fake=fake,
        )
        self.assertEqual(code, 0, payloads)
        self.assertEqual(fake.unexpected, [])
        stored = self.queue.task("valid")
        self.assertEqual((stored.mcp, stored.start_ref), ("airtable", "refs/heads/main"))
        self.assertEqual([item["type"] for item in stored.to_dict()["checks"]], ["issue_open", "release_exists"])
        code, payloads = self.add_cli("no-mcp", "--mcp", "none")
        self.assertEqual(code, 0, payloads)
        self.assertEqual(self.queue.task("no-mcp").mcp, "none")

    def test_codex_only_task_skips_mcp_validation(self) -> None:
        code, payloads = self.add_cli(
            "codex-task", "--providers", "codex", "--mcp", "project-connectors",
        )
        self.assertEqual(code, 0, payloads)
        self.assertEqual(self.queue.task("codex-task").mcp, "project-connectors")

    def test_set_mcp_validates(self) -> None:
        self.assertEqual(self.add_cli("target")[0], 0)

        code, _payloads = self.run_cli("set-mcp", "--database", str(self.queue.path), "target", "wiki")
        self.assertEqual(code, 2)
        self.assertIsNone(self.queue.task("target").mcp)

        code, payloads = self.run_cli("set-mcp", "--database", str(self.queue.path), "target", "none", "--json")
        self.assertEqual(code, 0, payloads)
        self.assertEqual(self.queue.task("target").mcp, "none")


class QueueTimeCheckCliTests(HermeticEnvironment, PreflightCase):
    """add and edit evaluate launch checks synchronously and print the results."""

    OPEN_12 = {"type": "issue_open", "repo": "owner/repo", "number": 12}

    def setUp(self) -> None:
        super().setUp()
        self.hermetic(self.mkdir("home"))
        self.cfg = runtime(self.queue.path)
        now_patch = mock.patch.dict(os.environ, {"BONUS_DRAIN_NOW": str(NOW)})
        now_patch.start()
        self.addCleanup(now_patch.stop)

    def run_cli(self, fake: FakeRunner, *argv: str) -> tuple[int, list[object], str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(cli, "_queue", return_value=(self.cfg, self.queue)),
            # Looked up at call time by the CLI, so patching the module attribute is enough.
            mock.patch.object(checks, "subprocess_runner", fake),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            captured_json() as payloads,
        ):
            code = cli.main(list(argv))
        return code, payloads, stdout.getvalue(), stderr.getvalue()

    def add_cli(self, fake: FakeRunner, task_id: str, *extra: str):
        return self.run_cli(
            fake, "add", "--database", str(self.queue.path), "--id", task_id, "--title", task_id,
            "--kind", "oneoff", "--size", "small", "--cwd", str(self.root),
            "--goal", f"complete {task_id}", *extra,
        )

    def passing_runner(self) -> FakeRunner:
        return self.runner({
            git_ls_remote(self.root): ok(LS_REMOTE_MAIN),
            gh_issue(12, "owner/repo"): ok(ISSUE_OPEN_IN_V0_10_0),
        })

    PASSING = ("--source-ref", "owner/repo#12", "--start-ref", "main",
               "--check", json.dumps(in_milestone(12)))

    def described(self) -> list[str]:
        return [
            checks.describe({"type": "base_ref_exists", "ref": "refs/heads/main"}),
            checks.describe(self.OPEN_12),
            checks.describe(in_milestone(12)),
        ]

    def test_add_passing_task_prints_results_and_is_ready_immediately(self) -> None:
        fake = self.passing_runner()

        code, payloads, out, err = self.add_cli(fake, "ready-task", *self.PASSING)

        self.assertEqual(code, 0, (payloads, out, err))
        self.assertIn("added: ready-task", out)
        lines = out.splitlines()
        for description in self.described():
            with self.subTest(check=description):
                matching = [line for line in lines if description in line]
                self.assertEqual(len(matching), 1, out)
                self.assertIn("pass", matching[0])
        self.assertIn("readiness: ready", out)
        self.assertNotIn("CHECK FAILED", err)
        self.assertNotIn("CHECK UNVERIFIED", err)
        # One shared issue view answers both issue checks.
        self.assertEqual(fake.tool_calls("issue"), [gh_issue(12, "owner/repo")])
        # No scout tick and no further refresh: the stored results already make it ready.
        status = self.ready("ready-task")
        self.assertTrue(status["ready"], status)
        self.assertEqual((status["state"], status["hold_reason"]), ("ready", None))
        self.assertEqual({item["status"] for item in status["checks"]}, {"pass"})

    def test_add_json_includes_checks_and_readiness(self) -> None:
        code, payloads, _out, err = self.add_cli(
            self.passing_runner(), "json-task", "--json", *self.PASSING,
        )

        self.assertEqual(code, 0, (payloads, err))
        self.assertEqual(len(payloads), 1, payloads)
        payload = payloads[0]
        self.assertEqual(payload["task"]["id"], "json-task")
        self.assertEqual(len(payload["checks"]), 3, payload["checks"])
        for entry in payload["checks"]:
            self.assertTrue({"status", "check", "detail"} <= set(entry), entry)
            self.assertEqual(entry["status"], "pass")
        self.assertCountEqual([entry["check"] for entry in payload["checks"]], self.described())
        self.assertEqual(payload["readiness"]["state"], "ready")
        self.assertTrue(payload["readiness"]["ready"])

    def test_add_failing_check_reports_on_stderr_and_stays_queued(self) -> None:
        fake = self.runner({gh_issue(2855): ok(ISSUE_2855_CLOSED)})

        code, payloads, out, err = self.add_cli(
            fake, "closed-task", "--check", json.dumps(ISSUE_OPEN_2855),
        )

        self.assertEqual(code, 0, (payloads, out, err))
        self.assertIn("CHECK FAILED", err)
        self.assertIn(checks.describe(ISSUE_OPEN_2855), err)
        self.assertIn("issue is CLOSED", err)
        self.assertIn("added: closed-task", out)
        self.assertIn("readiness: waiting", out)
        stored = self.queue.task("closed-task")
        self.assertIsNotNone(stored)
        self.assertTrue(stored.active)
        status = self.ready("closed-task")
        self.assertEqual((status["state"], status["hold_reason"]), ("waiting", "check_failed"))
        self.assertIn("issue is CLOSED", status["reason"])

    def test_add_unverifiable_check_reports_on_stderr(self) -> None:
        fake = self.runner({gh_issue(4000, "owner/repo"): exited(1, GH_OFFLINE_STDERR)})
        offline = {"type": "issue_open", "repo": "owner/repo", "number": 4000}

        code, payloads, out, err = self.add_cli(fake, "offline-task", "--check", json.dumps(offline))

        self.assertEqual(code, 0, (payloads, out, err))
        self.assertIn("CHECK UNVERIFIED", err)
        self.assertIn(checks.describe(offline), err)
        self.assertNotIn("CHECK FAILED", err)
        self.assertTrue(self.queue.task("offline-task").active)
        status = self.ready("offline-task")
        self.assertEqual((status["state"], status["hold_reason"]), ("waiting", "check_unknown"))

    def test_edit_changing_checks_re_evaluates_synchronously(self) -> None:
        idle = self.runner()
        self.assertEqual(self.add_cli(idle, "edited")[0], 0)
        self.assertEqual(idle.calls, [])
        self.assertTrue(self.ready("edited")["ready"])

        failing = self.runner({gh_issue(2855): ok(ISSUE_2855_CLOSED)})
        code, payloads, _out, err = self.run_cli(
            failing, "edit", "--database", str(self.queue.path), "edited",
            "--changes", json.dumps({"checks": [ISSUE_OPEN_2855]}),
        )

        self.assertEqual(code, 0, (payloads, err))
        payload = payloads[0]
        self.assertEqual(payload["task"]["checks"], [ISSUE_OPEN_2855])
        self.assertEqual(
            [(entry["status"], entry["check"]) for entry in payload["checks"]],
            [("fail", checks.describe(ISSUE_OPEN_2855))],
        )
        self.assertEqual(payload["readiness"]["hold_reason"], "check_failed")
        self.assertIn("CHECK FAILED", err)
        self.assertEqual(self.ready("edited")["hold_reason"], "check_failed")

        passing = self.runner({gh_issue(3815): ok(ISSUE_3815_OPEN)})
        code, payloads, _out, err = self.run_cli(
            passing, "edit", "--database", str(self.queue.path), "edited",
            "--changes", json.dumps({"checks": [ISSUE_OPEN_3815]}),
        )

        self.assertEqual(code, 0, (payloads, err))
        self.assertEqual(
            [(entry["status"], entry["check"]) for entry in payloads[0]["checks"]],
            [("pass", checks.describe(ISSUE_OPEN_3815))],
        )
        self.assertEqual(payloads[0]["readiness"]["state"], "ready")
        self.assertNotIn("CHECK", err)
        status = self.ready("edited")
        self.assertTrue(status["ready"], status)

    def test_add_evaluates_every_check_without_a_cap(self) -> None:
        responses = {
            git_ls_remote(self.root): ok(LS_REMOTE_MAIN),
            gh_issue(12, "owner/repo"): ok(ISSUE_3815_OPEN),
        }
        extra: list[str] = ["--source-ref", "owner/repo#12", "--start-ref", "main"]
        for number in range(5001, 5001 + checks.MAX_CHECKS_PER_TASK):
            extra += ["--check", json.dumps({"type": "issue_open", "repo": "owner/repo", "number": number})]
            responses[gh_issue(number, "owner/repo")] = ok(ISSUE_3815_OPEN)
        fake = self.runner(responses)

        with mock.patch.object(checks, "refresh", wraps=checks.refresh) as refresh:
            code, payloads, _out, err = self.add_cli(fake, "many-checks", "--json", *extra)

        self.assertEqual(code, 0, (payloads, err))
        self.assertEqual(len(payloads[0]["checks"]), checks.MAX_CHECKS_PER_TASK + 2)
        self.assertEqual({entry["status"] for entry in payloads[0]["checks"]}, {"pass"})
        self.assertEqual(len(fake.tool_calls("issue")), checks.MAX_CHECKS_PER_TASK + 1)
        self.assertEqual(refresh.call_count, 1)
        kwargs = refresh.call_args.kwargs
        self.assertEqual(tuple(kwargs["task_ids"]), ("many-checks",))
        self.assertIsNone(kwargs["max_calls"])
        self.assertIsNone(kwargs["budget_seconds"])
        status = self.ready("many-checks")
        self.assertTrue(status["ready"], status)
        self.assertEqual(len(status["checks"]), checks.MAX_CHECKS_PER_TASK + 2)

    def test_check_failing_at_add_flips_ready_after_one_scout_refresh(self) -> None:
        fake = self.runner({gh_issue(3815): [ok(ISSUE_2855_CLOSED), ok(ISSUE_3815_OPEN)]})

        code, _payloads, _out, err = self.add_cli(
            fake, "flips", "--check", json.dumps(ISSUE_OPEN_3815),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("CHECK FAILED", err)
        self.assertEqual(self.ready("flips")["hold_reason"], "check_failed")

        later = NOW + checks.RETRY_TTL_SECONDS
        checks.refresh(self.queue, runner=fake, now_epoch=later)

        self.assertEqual(len(fake.tool_calls("issue")), 2)
        status = self.ready("flips", now=later + 10)
        self.assertTrue(status["ready"], status)
        self.assertEqual(status["hold_reason"], None)


    def test_add_child_of_unfinished_parent_still_evaluates_its_checks(self) -> None:
        self.add("parent")
        fake = self.runner({gh_issue(2855): ok(ISSUE_2855_CLOSED)})

        code, payloads, _out, err = self.add_cli(
            fake, "child", "--json", "--depends-on", "parent",
            "--check", json.dumps(ISSUE_OPEN_2855),
        )

        self.assertEqual(code, 0, (payloads, err))
        self.assertIn("CHECK FAILED", err)
        self.assertIn(checks.describe(ISSUE_OPEN_2855), err)
        self.assertEqual(
            [(entry["status"], entry["check"]) for entry in payloads[0]["checks"]],
            [("fail", checks.describe(ISSUE_OPEN_2855))],
        )
        self.assertEqual(fake.tool_calls("issue"), [gh_issue(2855)])
        self.assertTrue(self.queue.task("child").active)
        status = self.ready("child")
        self.assertEqual(status["state"], "waiting")
        edge = next(item for item in status["dependencies"] if item["id"] == "parent")
        self.assertFalse(edge["satisfied"])

    def _edit_title(self, fake: FakeRunner, task_id: str, title: str):
        return self.run_cli(
            fake, "edit", "--database", str(self.queue.path), task_id,
            "--changes", json.dumps({"title": title}),
        )

    def test_edit_without_check_change_re_evaluates_a_fresh_fail(self) -> None:
        fake = self.runner({gh_issue(3815): [ok(ISSUE_2855_CLOSED), ok(ISSUE_3815_OPEN)]})
        code, _payloads, _out, err = self.add_cli(fake, "retitled", "--check", json.dumps(ISSUE_OPEN_3815))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ready("retitled")["hold_reason"], "check_failed")

        # Same clock: the fail is fresh, well inside RETRY_TTL_SECONDS.
        code, payloads, _out, err = self._edit_title(fake, "retitled", "Retitled task")

        self.assertEqual(code, 0, (payloads, err))
        self.assertEqual(len(fake.tool_calls("issue")), 2)
        self.assertEqual(
            [(entry["status"], entry["check"]) for entry in payloads[0]["checks"]],
            [("pass", checks.describe(ISSUE_OPEN_3815))],
        )
        self.assertEqual(payloads[0]["readiness"]["state"], "ready")
        self.assertNotIn("CHECK", err)
        status = self.ready("retitled")
        self.assertTrue(status["ready"], status)

    def test_edit_without_check_change_re_evaluates_a_fresh_pass(self) -> None:
        fake = self.runner({gh_issue(3815): ok(ISSUE_3815_OPEN)})
        code, _payloads, _out, err = self.add_cli(fake, "steady", "--check", json.dumps(ISSUE_OPEN_3815))
        self.assertEqual(code, 0, err)

        code, payloads, _out, err = self._edit_title(fake, "steady", "Steady task")

        self.assertEqual(code, 0, (payloads, err))
        self.assertEqual(len(fake.tool_calls("issue")), 2)
        self.assertEqual(
            [entry["status"] for entry in payloads[0]["checks"]], ["pass"],
        )
        self.assertEqual(payloads[0]["readiness"]["state"], "ready")
        self.assertTrue(self.ready("steady")["ready"])


    def _deactivate(self, task_id: str) -> None:
        code, payloads, _out, err = self.run_cli(
            self.runner(), "deactivate", "--database", str(self.queue.path), task_id,
        )
        self.assertEqual(code, 0, (payloads, err))
        self.assertFalse(self.queue.task(task_id).active)

    def test_edit_of_paused_task_evaluates_changed_checks(self) -> None:
        idle = self.runner()
        self.assertEqual(self.add_cli(idle, "paused-a")[0], 0)
        self._deactivate("paused-a")
        failing = self.runner({gh_issue(2855): ok(ISSUE_2855_CLOSED)})

        code, payloads, _out, err = self.run_cli(
            failing, "edit", "--database", str(self.queue.path), "paused-a",
            "--changes", json.dumps({"checks": [ISSUE_OPEN_2855]}),
        )

        self.assertEqual(code, 0, (payloads, err))
        self.assertEqual(
            [(entry["status"], entry["check"]) for entry in payloads[0]["checks"]],
            [("fail", checks.describe(ISSUE_OPEN_2855))],
        )
        self.assertIn("CHECK FAILED", err)
        self.assertFalse(self.queue.task("paused-a").active)
        self.assertEqual(self.ready("paused-a")["state"], "paused")

    def test_edit_of_paused_task_re_evaluates_stored_unknown(self) -> None:
        fake = self.runner({gh_issue(3815): [
            checks.CheckToolError("gh timed out after 20s"), ok(ISSUE_3815_OPEN),
        ]})
        code, _payloads, _out, err = self.add_cli(fake, "paused-b", "--check", json.dumps(ISSUE_OPEN_3815))
        self.assertEqual(code, 0, err)
        self.assertIn("CHECK UNVERIFIED", err)
        self._deactivate("paused-b")

        code, payloads, _out, err = self.run_cli(
            fake, "edit", "--database", str(self.queue.path), "paused-b",
            "--changes", json.dumps({"title": "Paused task, retitled"}),
        )

        self.assertEqual(code, 0, (payloads, err))
        self.assertEqual(len(fake.tool_calls("issue")), 2)
        self.assertEqual(
            [(entry["status"], entry["check"]) for entry in payloads[0]["checks"]],
            [("pass", checks.describe(ISSUE_OPEN_3815))],
        )
        self.assertNotIn("CHECK", err)
        self.assertFalse(self.queue.task("paused-b").active)
        self.assertEqual(self.ready("paused-b")["state"], "paused")

    def test_scout_refresh_skips_paused_tasks(self) -> None:
        self.add("paused-c", checks=[ISSUE_OPEN_3815], active=False)
        idle = self.runner()

        result = checks.refresh(self.queue, runner=idle, now_epoch=NOW)

        self.assertEqual(idle.calls, [])
        self.assertEqual(result["evaluated"], [])
        self.assertEqual(rows(self.queue, "SELECT * FROM check_results WHERE task_id='paused-c'"), [])
        self.assertEqual(self.ready("paused-c")["state"], "paused")


class ScoutPreflightTests(HermeticEnvironment, PreflightCase):
    def scout(self, cfg=None, **kwargs: Any) -> scout.ScoutReport:
        now = kwargs.pop("now", NOW)
        with mock.patch.object(scout, "read_all", return_value=alpha_snapshots(now)):
            return scout.run_once(cfg or runtime(self.queue.path), self.queue, now_epoch=now, **kwargs)

    def test_queue_json_local_never_runs_tools(self) -> None:
        self.hermetic(self.mkdir("home"))
        self.add("declared", checks=[ISSUE_OPEN_3815])
        self.add("builtin", source_ref="https://github.com/curie-eng/curie/issues/2855", start_ref="main")
        self.add("parent")
        self.add("child", depends_on=["parent"], merged_depends_on=["parent"])
        self.complete("parent", handoff())
        forbidden = AssertionError("a view load ran a tool")

        with (
            mock.patch.object(subprocess, "run", side_effect=forbidden),
            mock.patch.object(subprocess, "Popen", side_effect=forbidden),
            mock.patch.object(checks, "subprocess_runner", side_effect=forbidden),
            captured_json() as payloads,
        ):
            code = cli.main([
                "queue", "0", "--json", "--local", "--database", str(self.queue.path),
                "--now", str(NOW + 10),
            ])

        self.assertEqual(code, 0, payloads)
        readiness = payloads[0]["readiness"]
        self.assertEqual(readiness["declared"]["hold_reason"], "check_unchecked")
        self.assertEqual(readiness["builtin"]["hold_reason"], "check_unchecked")
        edge = next(item for item in readiness["child"]["dependencies"] if item["id"] == "parent")
        self.assertEqual(edge["status"], "merge_check_pending")
        self.assertEqual(rows(self.queue, "SELECT * FROM check_results"), [])

    def test_dry_run_scout_runs_no_checks(self) -> None:
        self.add("gated", checks=[ISSUE_OPEN_3815])
        self.add("plain")
        fake = self.runner()

        report = self.scout(
            dry_run=True, check_runner=fake,
            router_call=lambda *_args, **_kwargs: self.fail("dry run called the router"),
        )

        self.assertEqual(fake.calls, [])
        self.assertEqual(rows(self.queue, "SELECT * FROM check_results"), [])
        self.assertIsNone(report.preflight)
        self.assertEqual([item["task_id"] for item in report.previews], ["plain"])

    def test_manual_dispatch_evaluates_pending_check_synchronously(self) -> None:
        self.add("gated", checks=[ISSUE_OPEN_3815])
        fake = self.runner({gh_issue(3815): ok(ISSUE_3815_OPEN)})
        calls: list[list[str]] = []

        result = dispatcher.dispatch(
            runtime(self.queue.path), self.queue, task_id="gated", eligibility_key="manual/gated",
            requested_provider="alpha", router_call=launched_router(calls), check_runner=fake,
            now_epoch=NOW,
        )

        self.assertEqual(fake.tool_calls("issue"), [gh_issue(3815)])
        self.assertEqual(result.task_id, "gated")
        self.assertEqual(len(calls), 1)
        self.assertEqual([row["status"] for row in rows(self.queue, "SELECT status FROM check_results")], ["pass"])

        # A definitive negative found during the synchronous refresh refuses the launch.
        self.add("closed", checks=[ISSUE_OPEN_2855])
        closed = self.runner({gh_issue(2855): ok(ISSUE_2855_CLOSED)})
        router = mock.Mock()
        with self.assertRaises(dispatcher.AlreadyClaimed) as raised:
            dispatcher.dispatch(
                runtime(self.queue.path), self.queue, task_id="closed", eligibility_key="manual/closed",
                requested_provider="alpha", router_call=router, check_runner=closed, now_epoch=NOW,
            )
        self.assertIn("issue is CLOSED", str(raised.exception))
        router.assert_not_called()
        self.assertEqual(self.queue.attempts(task_id="closed"), [])

    def test_bonus_dispatch_never_runs_checks(self) -> None:
        self.add("gated", checks=[ISSUE_OPEN_3815])
        idle = self.runner()
        with self.assertRaises(dispatcher.AlreadyClaimed):
            dispatcher.dispatch(
                runtime(self.queue.path), self.queue, task_id="gated", eligibility_key=KEY,
                requested_provider="alpha", trigger="bonus", router_call=launched_router(),
                check_runner=idle, now_epoch=NOW,
            )
        self.assertEqual(idle.calls, [])
        self.assertEqual(self.queue.attempts(task_id="gated"), [])

        self.refresh(self.runner({gh_issue(3815): ok(ISSUE_3815_OPEN)}), now=NOW)
        result = dispatcher.dispatch(
            runtime(self.queue.path), self.queue, task_id="gated", eligibility_key=KEY,
            requested_provider="alpha", trigger="bonus", router_call=launched_router(),
            check_runner=idle, now_epoch=NOW + 10,
        )
        self.assertEqual(result.task_id, "gated")
        self.assertEqual(idle.calls, [])

    def test_refresher_failure_reports_preflight_failed(self) -> None:
        self.add("plain")
        with mock.patch.object(checks, "refresh", side_effect=RuntimeError("gh auth expired")):
            report = self.scout(router_call=launched_router(), check_runner=self.runner())

        self.assertIn(
            ("*", "preflight_failed"),
            [(error["task_id"], error["kind"]) for error in report.errors],
        )
        self.assertEqual([item.task_id for item in report.dispatched], ["plain"])


class PromptPreflightTests(PreflightCase):
    def dispatch(self, task_id: str, fake: FakeRunner, *, now: int = NOW) -> str:
        result = dispatcher.dispatch(
            runtime(self.queue.path), self.queue, task_id=task_id,
            eligibility_key=f"manual/{task_id}", requested_provider="alpha",
            router_call=launched_router(), check_runner=fake, now_epoch=now,
        )
        return result.prompt

    def test_prompt_lists_verified_checks(self) -> None:
        self.add("verified", checks=[ISSUE_OPEN_3815])
        prompt = self.dispatch("verified", self.runner({gh_issue(3815): ok(ISSUE_3815_OPEN)}))

        self.assertIn("Preflight checks already verified by the queue before this launch", prompt)
        self.assertIn("evaluate only the free-text precondition", prompt)
        self.assertIn("- " + checks.describe(ISSUE_OPEN_3815), prompt)
        self.assertNotIn("could not verify because of tool or network errors", prompt)

    def test_prompt_lists_unverified_checks(self) -> None:
        self.add("degraded", checks=[ISSUE_OPEN_3815])
        offline = self.runner({gh_issue(3815): exited(1, GH_OFFLINE_STDERR)})
        self.refresh(offline, now=NOW - 3_700)
        self.refresh(offline, now=NOW - 100)
        idle = self.runner()

        prompt = self.dispatch("degraded", idle)

        self.assertEqual(idle.calls, [])
        self.assertIn("could not verify because of tool or network errors", prompt)
        self.assertIn("verify these yourself before starting", prompt)
        self.assertIn("- " + checks.describe(ISSUE_OPEN_3815), prompt)
        self.assertNotIn("Preflight checks already verified by the queue", prompt)

    def merged_child(self) -> None:
        self.add("parent")
        self.add("child", depends_on=["parent"], merged_depends_on=["parent"])
        self.complete("parent", handoff(), now=NOW - 4_000)

    MERGE_SPEC = {"type": "pr_merged", "repo": "owner/repo", "head": "task/x", "base": "main"}

    def test_prompt_lists_unverified_dependency_merge(self) -> None:
        # Review r1 P1: a merge GitHub could not confirm for an hour still launches, so the
        # worker must be told to verify that the prerequisite actually landed.
        self.merged_child()
        offline = self.runner({gh_pr("task/x", "owner/repo"): exited(1, GH_OFFLINE_STDERR)})
        self.refresh(offline, now=NOW - 3_700)
        self.refresh(offline, now=NOW - 100)
        status = self.ready("child", now=NOW)
        self.assertTrue(status["ready"], status)
        self.assertIn("merge unverified", status["dependencies"][0]["detail"])
        idle = self.runner()

        # dispatch() re-reads the dependency base on the wall clock; pin it to the test clock.
        with mock.patch.object(db.time, "time", return_value=NOW):
            prompt = self.dispatch("child", idle)

        self.assertEqual(idle.calls, [])
        self.assertIn("could not verify because of tool or network errors", prompt)
        self.assertIn("- " + checks.describe(self.MERGE_SPEC), prompt)

    def test_prompt_does_not_ask_to_verify_passed_dependency_merge(self) -> None:
        self.merged_child()
        merged = with_fields(PR_2994_MERGED, baseRefName="main", headRefName="task/x", number=41)
        self.refresh(self.runner({gh_pr("task/x", "owner/repo"): ok(merged)}), now=NOW - 100)

        with mock.patch.object(db.time, "time", return_value=NOW):
            prompt = self.dispatch("child", self.runner())

        self.assertNotIn("could not verify because of tool or network errors", prompt)
        unverified_section = prompt.split("could not verify", 1)[-1] if "could not verify" in prompt else ""
        self.assertNotIn(checks.describe(self.MERGE_SPEC), unverified_section)

    def test_prompt_without_checks_has_no_preflight_section(self) -> None:
        self.add("plain")
        idle = self.runner()
        prompt = self.dispatch("plain", idle)
        self.assertEqual(idle.calls, [])
        self.assertNotIn("Preflight checks", prompt)
        self.assertNotIn("could not verify", prompt)
        self.assertEqual(
            dispatcher.render_prompt(
                runtime(self.queue.path), self.queue.task("plain"), KEY, "alpha", "alpha-account",
                preflight_checks=(),
            ).count("Preflight"),
            0,
        )


class CollisionAllocationTests(PreflightCase):
    def tick(self, config, snapshots) -> scout.TickPlan:
        with mock.patch.object(scout, "read_all", return_value=snapshots):
            return scout.plan_tick(config, self.queue, now_epoch=NOW, provider_holds=())

    def allocated(self, tick: scout.TickPlan) -> dict[str, list[str]]:
        return {
            provider_id: [item.id for item in tasks]
            for (provider_id, _account_id), tasks in tick.allocations.items()
            if tasks
        }

    def slots(self, *, alpha_used: float = 74, beta_used: float = 74):
        """Used 74 leaves one launch slot on a provider; 73 leaves two."""

        config = _two_provider_config(self.queue, self.root / "cache")
        snapshots = {
            (provider, f"{provider}-account"): usage.UsageSnapshot(
                provider, f"{provider}-account", NOW,
                {f"{provider}-plan-weekly": {"used_percent": used, "resets_at": NOW + 40 * HOUR}},
            )
            for provider, used in (("alpha", alpha_used), ("beta", beta_used))
        }
        return config, snapshots

    def grouped(self, task_id: str, priority: int, *providers: str, group: str | None = None) -> None:
        value = provider_task(task_id, *providers)
        value["priority"] = priority
        value["cwd"] = str(self.root)
        if group:
            value["work_group"] = group
        self.queue.add_task(value)

    def test_plan_tick_allocates_one_task_per_group(self) -> None:
        self.add("g1", work_group="Report work", priority=1)
        self.add("g2", work_group="Report work", priority=2)
        self.add("solo", priority=3)

        tick = self.tick(runtime(self.queue.path), alpha_snapshots())

        allocated = sorted(item for ids in self.allocated(tick).values() for item in ids)
        self.assertEqual(allocated, ["g1", "solo"])

    def test_unallocated_task_reserves_no_key(self) -> None:
        # One Claude-like slot (alpha) and one Codex-like slot (beta).
        config, snapshots = self.slots()
        self.grouped("unrelated", 0, "alpha")
        self.grouped("group-a", 1, "alpha", group="Group G")
        self.grouped("group-b", 2, "alpha", "beta", group="Group G")

        tick = self.tick(config, snapshots)

        # group-a found no slot, so it reserved nothing and beta is not left idle.
        self.assertEqual(self.allocated(tick), {"alpha": ["unrelated"], "beta": ["group-b"]})

    def test_reassignment_keeps_reserved_key(self) -> None:
        # Beta has a spare second slot, so only the reserved key keeps group-b out of it.
        config, snapshots = self.slots(alpha_used=74, beta_used=73)
        self.grouped("group-a", 0, "alpha", "beta", group="Group G")
        self.grouped("constrained", 1, "alpha")
        self.grouped("group-b", 2, "alpha", "beta", group="Group G")

        tick = self.tick(config, snapshots)

        self.assertEqual(self.allocated(tick), {"alpha": ["constrained"], "beta": ["group-a"]})


class GoalCollisionReservationTests(PreflightCase):
    """Review r1 P2: goal members never wait, but still reserve their keys within a tick."""

    def setUp(self) -> None:
        super().setUp()
        self.config = _two_provider_config(self.queue, self.root / "cache")

    def member(self, task_id: str, priority: int) -> None:
        value = provider_task(task_id, "alpha", "beta")
        value.update(priority=priority, cwd=str(self.root), work_group="Group G")
        self.queue.add_task(value)

    def run_tick(self) -> tuple[scout.TickPlan, scout.ScoutReport]:
        with mock.patch.object(scout, "read_all", return_value=_open_snapshots()):
            tick = scout.plan_tick(self.config, self.queue, now_epoch=NOW, provider_holds=())
        # The fixture goal carries only the admission fields; goal coordination turns are not
        # under test here, so the goal tick itself is held idle.
        with (
            mock.patch.object(scout, "read_all", return_value=_open_snapshots()),
            mock.patch.object(db.time, "time", return_value=NOW),
            mock.patch.object(scout.goals.GoalStore, "tick", return_value=()),
        ):
            report = scout.run_once(
                self.config, self.queue, now_epoch=NOW,
                router_call=launched_router(), check_runner=self.runner(),
            )
        return tick, report

    @staticmethod
    def allocated(tick: scout.TickPlan) -> list[str]:
        return sorted(item.id for tasks in tick.allocations.values() for item in tasks)

    def test_goal_member_reserves_group_against_ordinary_task_in_same_tick(self) -> None:
        self.member("goal-member", 0)
        self.member("ordinary", 1)
        add_goal(self.queue, "goal-1", ["goal-member"])

        tick, report = self.run_tick()

        self.assertEqual(self.allocated(tick), ["goal-member"])
        self.assertEqual(report.errors, ())
        self.assertEqual([item.task_id for item in report.dispatched], ["goal-member"])
        self.assertEqual(self.queue.attempts(task_id="ordinary"), [])
        self.assertEqual(
            self.queue.readiness("ordinary", now_epoch=NOW)["hold_reason"], "collision",
        )

    def task_for(self, task_id: str, priority: int, *providers: str, group: str | None = None) -> None:
        value = provider_task(task_id, *providers)
        value.update(priority=priority, cwd=str(self.root))
        if group:
            value["work_group"] = group
        self.queue.add_task(value)

    def one_slot_each(self) -> dict[tuple[str, str], usage.UsageSnapshot]:
        # Used 74 leaves exactly one launch slot on each provider.
        return {
            (provider, f"{provider}-account"): usage.UsageSnapshot(
                provider, f"{provider}-account", NOW,
                {f"{provider}-plan-weekly": {"used_percent": 74, "resets_at": NOW + 40 * HOUR}},
            )
            for provider in ("alpha", "beta")
        }

    def test_unplaced_goal_does_not_starve_higher_priority_ordinary_task(self) -> None:
        # Review r2 P2: an ordinary task ahead of a goal candidate in its group must keep its
        # priority when the goal never gets a slot.
        self.task_for("unrelated-alpha", 0, "alpha")
        self.task_for("ordinary", 1, "alpha", "beta", group="Group G")
        self.task_for("goal-member", 2, "alpha", group="Group G")
        self.task_for("low", 3, "alpha", "beta")
        add_goal(self.queue, "goal-1", ["goal-member"])

        with mock.patch.object(scout, "read_all", return_value=self.one_slot_each()):
            tick = scout.plan_tick(self.config, self.queue, now_epoch=NOW, provider_holds=())

        allocated = self.allocated(tick)
        self.assertIn("ordinary", allocated)
        self.assertNotIn("low", allocated)
        self.assertEqual(allocated, ["ordinary", "unrelated-alpha"])

    def test_ordinary_before_goal_member_dispatches_without_scout_error(self) -> None:
        # Goal members never wait on collision keys, so an ordinary task ahead of one in
        # priority order cannot turn the goal's claim into a scout error.
        self.member("ordinary", 0)
        self.member("goal-member", 1)
        add_goal(self.queue, "goal-1", ["goal-member"])

        tick, report = self.run_tick()

        self.assertEqual(report.errors, ())
        self.assertIn("goal-member", [item.task_id for item in report.dispatched])
        dispatched = {item.task_id for item in report.dispatched}
        self.assertEqual(set(self.allocated(tick)), dispatched)
        for task_id in ("ordinary", "goal-member"):
            self.assertNotIn(
                "aborted", [item.state for item in self.queue.attempts(task_id=task_id)],
            )

    def test_goal_members_sharing_group_still_allocate_together(self) -> None:
        self.member("member-a", 0)
        self.member("member-b", 1)
        add_goal(self.queue, "goal-1", ["member-a", "member-b"], max_inflight=2)

        tick, report = self.run_tick()

        self.assertEqual(self.allocated(tick), ["member-a", "member-b"])
        self.assertEqual(report.errors, ())
        self.assertEqual(sorted(item.task_id for item in report.dispatched), ["member-a", "member-b"])


class ResumeAndNoticeScoutTests(HermeticEnvironment, PreflightCase):
    def scout(self, now: int, *, cfg=None, runner: FakeRunner | None = None) -> scout.ScoutReport:
        with mock.patch.object(scout, "read_all", return_value=alpha_snapshots(NOW)):
            return scout.run_once(
                cfg or runtime(self.queue.path), self.queue, now_epoch=now,
                router_call=launched_router(), check_runner=runner or self.runner(),
            )

    def test_record_cli_refuses_awaiting_human(self) -> None:
        self.hermetic(self.mkdir("home"))
        self.add("parked")
        attempt = self.claim("parked")
        self.queue.record(
            "parked", KEY, attempt_id=attempt.id, status="dispatched",
            provider_id="alpha", account_id="alpha-account", router_job_id="job-parked",
            timestamp=iso(NOW), now_epoch=NOW,
        )
        common = [
            "record", "--database", str(self.queue.path), "--task", "parked", "--kind", "oneoff",
            "--eligibility-key", KEY, "--attempt-id", attempt.id,
            "--provider-id", "alpha", "--account-id", "alpha-account", "--json",
        ]

        with captured_json() as payloads:
            code = cli.main([*common, "--status", "awaiting_human"])

        self.assertNotEqual(code, 0)
        # Refused as a status choice (or the retired-status message), not for a missing outcome.
        self.assertRegex(json.dumps(payloads), r"invalid choice: 'awaiting_human'|awaiting_human is retired")
        self.assertEqual(self.queue.claim_for("parked").attempt_id, attempt.id)
        self.assertNotIn("awaiting_human", [row["status"] for row in rows(self.queue, "SELECT status FROM runs")])

        with captured_json() as payloads:
            code = cli.main([*common, "--status", "failed", "--summary", "blocked on an external decision"])
        self.assertEqual(code, 0, payloads)
        self.assertEqual(self.queue.attempts(task_id="parked")[0].state, "failed")

    def test_scout_tick_resumes_satisfied_hold(self) -> None:
        self.add("blocked")
        self.block("blocked", authority(resume_when=[PR_2994_INTO_EPIC]))
        fake = self.runner({gh_pr(2994): ok(PR_2994_MERGED)})

        report = self.scout(NOW + 60, runner=fake)

        self.assertEqual(report.errors, ())
        self.assertIn(
            ("blocked", "pr_merged", "pass"),
            [(item["task_id"], item["type"], item["status"]) for item in report.preflight["checks"]["evaluated"]],
        )
        self.assertEqual([item["task_id"] for item in report.preflight["resumed"]], ["blocked"])
        self.assertEqual([item.task_id for item in report.dispatched], ["blocked"])
        self.assertEqual(
            [item.state for item in self.queue.attempts(task_id="blocked")], ["failed", "dispatched"],
        )
        self.assertEqual(report.to_dict()["preflight"]["resumed"][0]["task_id"], "blocked")

    def test_blocker_notified_once_across_ticks(self) -> None:
        cfg = replace(runtime(self.queue.path), scout_ntfy_url="https://ntfy.example.test/bonus-drain")
        self.add("standalone")
        self.block("standalone", authority("Missing GitHub test actor"))

        with mock.patch.object(notifications, "urlopen") as urlopen:
            first = self.scout(NOW + 60, cfg=cfg)
            second = self.scout(NOW + 700, cfg=cfg)

        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        body = request.data.decode("utf-8")
        self.assertIn("standalone", body)
        self.assertIn("bonus-drain held-report --json", body)
        self.assertNotIn("Missing GitHub test actor", body)
        self.assertEqual(first.preflight["notified"], ["standalone"])
        self.assertEqual(second.preflight["notified"], [])

    def test_no_ntfy_url_reserves_nothing(self) -> None:
        self.add("standalone")
        self.block("standalone")

        with mock.patch.object(notifications, "urlopen") as urlopen:
            quiet = self.scout(NOW + 60)
            self.assertEqual(rows(self.queue, "SELECT * FROM blocker_notices"), [])
            self.assertEqual(quiet.preflight["notified"], [])
            cfg = replace(runtime(self.queue.path), scout_ntfy_url="https://ntfy.example.test/bonus-drain")
            loud = self.scout(NOW + 700, cfg=cfg)

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(loud.preflight["notified"], ["standalone"])


class HeldReportCliTests(HermeticEnvironment, PreflightCase):
    def test_held_report_json_lists_both_sources(self) -> None:
        self.hermetic(self.mkdir("home"))
        self.add("A")
        self.add("B", depends_on=["A"])
        held = self.block("A", authority("Missing GitHub test actor"))
        self.add("S")
        standalone = self.block("S", authority("Production deploy key is not provisioned"))
        self.queue.reconcile_recoveries(now_epoch=NOW + 5, dry_run=False)
        before = table_counts(self.queue.path)

        with captured_json() as payloads:
            code = cli.main(["held-report", "--database", str(self.queue.path), "--json"])

        self.assertEqual(code, 0, payloads)
        self.assertEqual(table_counts(self.queue.path), before)
        report = {item["task_id"]: item for item in payloads[0]["held"]}
        self.assertEqual(set(report), {"A", "S"})
        self.assertEqual(report["A"]["source"], "held_recovery")
        self.assertEqual(report["A"]["descendants"], ["B"])
        self.assertEqual(report["A"]["source_attempt_id"], held.id)
        self.assertEqual(report["S"]["source"], "failed_attempt")
        self.assertEqual(report["S"]["source_attempt_id"], standalone.id)
        self.assertEqual(report["S"]["held_since"], self.queue.attempts(task_id="S")[0].terminal_at)

    def test_dependency_holds_include_root_blocker(self) -> None:
        for name in ("a", "b"):
            self.add(name)
            self.complete(name, {**handoff(), "branch_ref": f"refs/heads/task/{name}"})
        self.add("child", depends_on=["a", "b"])
        self.add("independent", priority=3)

        with mock.patch.object(scout, "read_all", return_value=alpha_snapshots()):
            tick = scout.plan_tick(runtime(self.queue.path), self.queue, now_epoch=NOW, provider_holds=())

        holds = {item["task_id"]: item for item in tick.dependency_holds}
        self.assertEqual(holds["child"]["hold_reason"], "integration_required")
        self.assertIn("root_blocker", holds["child"])
        self.assertEqual(
            holds["child"]["root_blocker"],
            self.queue.readiness("child", now_epoch=NOW)["root_blocker"],
        )


if __name__ == "__main__":
    unittest.main()
