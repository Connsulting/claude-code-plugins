"""Launch-blocker probe checks: MCP authentication, Kubernetes resources, OpenRouter credit.

Every tool observation goes through ``FakeRunner`` from the preflight check tests, which
replays recorded or documented output by exact argv and never parses it. Each constant
below names where its shape came from.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

from tests.test_bonus_dependency_recovery import NOW, captured_json, rows, runtime
from tests.test_bonus_drain_preflight_checks import (
    GH_OFFLINE_STDERR, ISSUE_OPEN_3815, FakeRunner, PreflightCase, exited, gh_issue, ok,
)
from tests.test_bonus_drain_preflight_dispatch import HermeticEnvironment
from tests.readiness_fixture import review_json
from bonus_drain import checks, cli

# --- Claude: ``claude mcp get <server>`` ----------------------------------------------
# Observed 2026-10-03 on this host (plan D4): a connected server prints its name line, a
# Scope line (text shortened as in the plan), and a Status line, exit 0.
CLAUDE_CONNECTED = "linear-cn:\n  Scope: ...\n  Status: ✔ Connected\n"
# Status labels in the shipped claude binary (plan D4), on the same recorded layout.
CLAUDE_NEEDS_AUTH = "linear-cn:\n  Scope: ...\n  Status: ! Needs authentication\n"
CLAUDE_FAILED = "linear-cn:\n  Scope: ...\n  Status: ✗ Failed to connect\n"
# Observed 2026-10-03 on this host: exit 1, empty stdout, this line on stderr.
CLAUDE_NO_SERVER_STDERR = (
    'No MCP server named "linear-cn". Configured servers: claude.ai Claude Docs, '
    "claude.ai Granola, claude.ai Microsoft 365, claude.ai Notion\n"
)

# --- Codex: ``codex mcp list --json`` -------------------------------------------------
# Observed 2026-10-03 on this host (two of the three servers listed, unchanged).
CODEX_LIST = [
    {
        "name": "linear-cn",
        "enabled": True,
        "disabled_reason": None,
        "transport": {
            "type": "streamable_http",
            "url": "https://mcp.linear.app/mcp",
            "bearer_token_env_var": None,
            "http_headers": None,
            "env_http_headers": None,
            "http_headers_helper": None,
        },
        "startup_timeout_sec": None,
        "tool_timeout_sec": None,
        "auth_status": "o_auth",
    },
    {
        "name": "slack-curietech",
        "enabled": True,
        "disabled_reason": None,
        "transport": {
            "type": "stdio",
            "command": "/home/theconnman/git/connsulting/meta/bin/slack-mcp",
            "args": ["curietech"],
            "env": None,
            "env_vars": [],
            "cwd": None,
        },
        "startup_timeout_sec": None,
        "tool_timeout_sec": None,
        "auth_status": "unsupported",
    },
]


def codex_list(**linear_changes: object) -> str:
    """The recorded list, with linear-cn re-keyed (``not_logged_in`` is a codex value per plan D4)."""

    servers = [dict(item) for item in CODEX_LIST]
    servers[0].update(linear_changes)
    return json.dumps(servers, indent=2) + "\n"


# --- Kubernetes: ``kubectl --context C [-n NS] get KIND NAME -o name`` ----------------
# Observed 2026-10-03 on this host (plan D4).
KUBECTL_FOUND_NAMESPACE = "namespace/default\n"
KUBECTL_NOT_FOUND_STDERR = 'Error from server (NotFound): services "nope-xyz" not found\n'
KUBECTL_NO_CONTEXT_STDERR = (
    "Error in configuration: context was not found for specified context: nosuchctx\n"
)
# The recorded found shape (``<kind>/<name>``) re-keyed to the service the fixtures probe.
KUBECTL_FOUND_SERVICE = "service/nope-xyz\n"

# --- OpenRouter: curl with ``-w "\n%{http_code}"`` (body, then the status line) -------
KEY_URL = "https://openrouter.ai/api/v1/key"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"
# Observed 2026-10-03 on this host: an invalid key, exit 0.
OPENROUTER_401 = '{"error":{"message":"User not found.","code":401}}\n401'
# Observed 2026-10-03 on this host: the key environment variable is unset, exit 2.
CURL_VARIABLE_STDERR = (
    "curl: option --variable: variable expansion failure\n"
    "curl: try 'curl --help' or 'curl --manual' for more information\n"
)
# Documented: https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key.md
# (limit_remaining is null when the key has no spend limit).
KEY_LIMITED = {"data": {"limit": 100, "limit_remaining": 74.5, "usage": 25.5, "is_management_key": False}}
KEY_UNLIMITED = {"data": {"limit": None, "limit_remaining": None, "usage": 25.5, "is_management_key": True}}
# Documented: https://openrouter.ai/docs/api-reference/get-credits (management keys only;
# a regular key gets 403 "Only management keys can perform this operation").
CREDITS = {"data": {"total_credits": 100.5, "total_usage": 25.75}}
CREDITS_403 = {"error": {"code": 403, "message": "Only management keys can perform this operation"}}

# The documented /credits shape with the balance fully spent.
CREDITS_SPENT = {"data": {"total_credits": 25.5, "total_usage": 25.5}}
# The documented /key shape for a regular key with no spend limit.
KEY_UNLIMITED_REGULAR = {"data": {**KEY_UNLIMITED["data"], "is_management_key": False}}

SECRET_ENV_KEY = "sk-or-v1-fixture-env-secret-0123456789abcdef"
SECRET_FILE_KEY = "sk-or-v1-fixture-file-secret-fedcba9876543210"
FILE_VARIABLE = "BONUS_DRAIN_OPENROUTER_KEY"
BALANCE_FILE_VARIABLE = "BONUS_DRAIN_OPENROUTER_BALANCE_KEY"
SECRET_BALANCE_ENV_KEY = "sk-or-v1-fixture-balance-env-secret-a1b2c3"
SECRET_BALANCE_FILE_KEY = "sk-or-v1-fixture-balance-file-secret-d4e5f6"


def http(body: object, status: int) -> str:
    return json.dumps(body, separators=(",", ":")) + f"\n{status}"


def claude_get(server: str = "linear-cn") -> tuple[str, ...]:
    return ("claude", "mcp", "get", server)


CODEX_LIST_ARGV = ("codex", "mcp", "list", "--json")


def kubectl(context: str, kind: str, name: str, namespace: str | None = None) -> tuple[str, ...]:
    scope = ("-n", namespace) if namespace else ()
    return ("kubectl", "--context", context, *scope, "get", kind, name, "-o", "name")


def curl(
    url: str, *, key_env: str | None = None, key_file: str | None = None,
    file_variable: str = FILE_VARIABLE,
) -> tuple[str, ...]:
    if key_env is not None:
        variable, name = f"%{key_env}", key_env
    else:
        variable, name = f"{file_variable}@{key_file}", file_variable
    return (
        "curl", "-sS", "--max-time", "15", "--variable", variable,
        "--expand-header", "Authorization: Bearer {{" + name + ":trim}}",
        "-w", "\n%{http_code}", url,
    )


MCP_CLAUDE = {"type": "mcp_authenticated", "provider": "claude", "server": "linear-cn"}
MCP_CODEX_LINEAR = {"type": "mcp_authenticated", "provider": "codex", "server": "linear-cn"}
MCP_CODEX_SLACK = {"type": "mcp_authenticated", "provider": "codex", "server": "slack-curietech"}
K8S_NAMESPACE = {"type": "k8s_resource_exists", "context": "k8", "kind": "namespace", "name": "default"}
K8S_SERVICE = {
    "type": "k8s_resource_exists", "context": "k8", "namespace": "default",
    "kind": "service", "name": "nope-xyz",
}
EKS_CONTEXT = "arn:aws:eks:us-east-1:123456789012:cluster/Prod"


def credit(min_usd: object = 5, **source: str) -> dict[str, object]:
    """An openrouter_credit spec; the key defaults to env OPENROUTER_API_KEY unless a key source is given."""

    keys = {} if {"key_env", "key_file"} & set(source) else {"key_env": "OPENROUTER_API_KEY"}
    return {"type": "openrouter_credit", "min_usd": min_usd, **keys, **source}


class ProbeCase(PreflightCase):
    def evaluate(self, raw: dict[str, object], responses: dict[tuple[str, ...], Any], *, cwd: str | None = None):
        fake = self.runner(responses)
        result = checks.evaluate(checks.normalize_check(raw), cwd=cwd or str(self.root), runner=fake)
        return result, fake

    def assertVerdict(self, result, status: str, detail: str | None = None) -> None:
        self.assertEqual(result.status, status, result)
        if detail is not None:
            self.assertIn(detail, result.detail)

    def attempt_rows(self) -> list[dict[str, object]]:
        return rows(self.queue, "SELECT * FROM task_attempts")


class ProbeSpecTests(ProbeCase):
    def test_normalize_accepts_each_new_type(self) -> None:
        valid = [
            MCP_CLAUDE,
            MCP_CODEX_LINEAR,
            K8S_NAMESPACE,
            K8S_SERVICE,
            {"type": "k8s_resource_exists", "context": EKS_CONTEXT, "namespace": "curie",
             "kind": "deployment", "name": "curie-email-e2e"},
            {"type": "k8s_resource_exists", "context": "user@cluster.example", "kind": "service",
             "name": "curie-email-e2e"},
            credit(5),
            credit(2.5),
            credit(5, key_file="/home/user/.config/openrouter/key"),
            credit(5, balance_key_env="OPENROUTER_MANAGEMENT_KEY"),
            credit(5, key_file="/keys/openrouter", balance_key_file="/keys/openrouter-management"),
        ]
        for raw in valid:
            with self.subTest(raw=raw):
                self.assertEqual(checks.normalize_check(raw), raw)
        self.assertTrue(
            {"mcp_authenticated", "k8s_resource_exists", "openrouter_credit"} <= set(checks.CHECK_FIELDS),
        )
        self.assertEqual(
            checks.HOLDING_TYPES,
            frozenset({"mcp_authenticated", "k8s_resource_exists", "openrouter_credit"}),
        )

    def test_describe_new_types(self) -> None:
        self.assertEqual(checks.describe(MCP_CLAUDE), "mcp_authenticated claude:linear-cn")
        self.assertEqual(
            checks.describe({"type": "k8s_resource_exists", "context": "k8", "namespace": "default",
                             "kind": "service", "name": "x"}),
            "k8s_resource_exists k8:default/service/x",
        )
        self.assertEqual(checks.describe(credit(5)), "openrouter_credit >= $5 (env OPENROUTER_API_KEY)")
        self.assertEqual(
            checks.describe(credit(5, key_file="/keys/openrouter")),
            "openrouter_credit >= $5 (file /keys/openrouter)",
        )

    def test_normalize_rejects_invalid_probe_specs(self) -> None:
        invalid = {
            "k8s without context": {"type": "k8s_resource_exists", "kind": "service", "name": "x"},
            "k8s context option": {"type": "k8s_resource_exists", "context": "-k8", "kind": "service", "name": "x"},
            "k8s kind with space": {"type": "k8s_resource_exists", "context": "k8", "kind": "svc x", "name": "x"},
            "k8s name option": {"type": "k8s_resource_exists", "context": "k8", "kind": "service", "name": "-x"},
            "k8s namespace option": {
                "type": "k8s_resource_exists", "context": "k8", "kind": "service", "name": "x",
                "namespace": "--all-namespaces",
            },
            "k8s extra key": {**K8S_NAMESPACE, "selector": "app=x"},
            "openrouter both sources": credit(5, key_env="OPENROUTER_API_KEY", key_file="/keys/openrouter"),
            "openrouter neither source": {"type": "openrouter_credit", "min_usd": 5},
            "openrouter bool min": credit(True),
            "openrouter zero min": credit(0),
            "openrouter negative min": credit(-1),
            "openrouter text min": credit("5"),
            "openrouter missing min": {"type": "openrouter_credit", "key_env": "OPENROUTER_API_KEY"},
            "openrouter relative file": credit(5, key_file="keys/openrouter"),
            "openrouter parent segment": credit(5, key_file="/keys/../etc/openrouter"),
            "openrouter bad env name": credit(5, key_env="1OPENROUTER"),
            "openrouter literal key": {**credit(5), "key": SECRET_ENV_KEY},
            "openrouter both balance sources": credit(
                5, balance_key_env="OPENROUTER_MANAGEMENT_KEY", balance_key_file="/keys/management",
            ),
            "openrouter relative balance file": credit(5, balance_key_file="keys/management"),
            "openrouter balance parent segment": credit(5, balance_key_file="/keys/../management"),
            "openrouter bad balance env name": credit(5, balance_key_env="1MANAGEMENT"),
            "mcp provider list": {"type": "mcp_authenticated", "provider": [], "server": "linear-cn"},
            "mcp provider object": {"type": "mcp_authenticated", "provider": {}, "server": "linear-cn"},
            "mcp server list": {"type": "mcp_authenticated", "provider": "claude", "server": []},
            "k8s kind object": {"type": "k8s_resource_exists", "context": "k8", "kind": {}, "name": "x"},
            "mcp unknown provider": {"type": "mcp_authenticated", "provider": "grok", "server": "linear-cn"},
            "mcp server option": {"type": "mcp_authenticated", "provider": "claude", "server": "--help"},
            "mcp missing server": {"type": "mcp_authenticated", "provider": "claude"},
        }
        for label, raw in invalid.items():
            with self.subTest(label), self.assertRaises(checks.CheckError):
                checks.normalize_check(raw)

    def test_claude_context_is_the_checkout(self) -> None:
        project = self.mkdir("project")
        self.assertEqual(
            checks.check_context(checks.normalize_check(MCP_CLAUDE), cwd=str(project)),
            {"cwd": os.path.realpath(project)},
        )
        self.assertEqual(checks.check_context(checks.normalize_check(K8S_SERVICE), cwd=str(project)), {})


class McpClaudeTests(ProbeCase):
    def test_status_lines(self) -> None:
        cases = [
            (ok(CLAUDE_CONNECTED), "pass", "linear-cn is connected"),
            (ok(CLAUDE_NEEDS_AUTH), "fail", "linear-cn needs authentication (run /mcp in Claude to re-authenticate)"),
            (exited(1, CLAUDE_NO_SERVER_STDERR), "fail", f"linear-cn is not configured for claude in {self.root}"),
            (ok(CLAUDE_FAILED), "unknown", None),
        ]
        for response, status, detail in cases:
            with self.subTest(status=status, detail=detail):
                result, fake = self.evaluate(MCP_CLAUDE, {claude_get(): response})
                self.assertVerdict(result, status, detail)
                self.assertEqual(fake.calls, [claude_get()])

    def test_runner_receives_task_cwd(self) -> None:
        project = self.mkdir("project")
        self.add("mcp-task", cwd=str(project), checks=[MCP_CLAUDE])
        fake = self.runner({claude_get(): ok(CLAUDE_CONNECTED)})

        self.refresh(fake, task_ids=("mcp-task",), due_only=False, max_calls=None, budget_seconds=None)

        self.assertEqual(fake.calls, [claude_get()])
        self.assertEqual(fake.cwds, [str(project)])
        self.assertTrue(self.ready("mcp-task")["ready"])


class McpCodexTests(ProbeCase):
    def test_list_shapes(self) -> None:
        cases = [
            (ok(codex_list()), "pass", "linear-cn auth_status o_auth"),
            (ok(codex_list(auth_status="not_logged_in")), "fail", "needs login (codex mcp login linear-cn)"),
            (ok(codex_list(name="linear-other")), "fail", "not configured"),
            (ok(codex_list(enabled=False)), "fail", "disabled"),
            # A truncated copy of the recorded output: unparseable, never a verdict.
            (ok(codex_list()[:40]), "unknown", None),
        ]
        for response, status, detail in cases:
            with self.subTest(status=status, detail=detail):
                result, fake = self.evaluate(MCP_CODEX_LINEAR, {CODEX_LIST_ARGV: response})
                self.assertVerdict(result, status, detail)
                self.assertEqual(fake.calls, [CODEX_LIST_ARGV])
                self.assertEqual(fake.cwds, [str(self.root)])

    def test_unsupported_auth_passes(self) -> None:
        result, _fake = self.evaluate(MCP_CODEX_SLACK, {CODEX_LIST_ARGV: ok(codex_list())})
        self.assertVerdict(result, "pass", "slack-curietech auth_status unsupported")

    def test_two_codex_checks_share_one_list_call(self) -> None:
        self.add("codex-a", checks=[MCP_CODEX_LINEAR])
        self.add("codex-b", checks=[MCP_CODEX_SLACK])
        fake = self.runner({CODEX_LIST_ARGV: ok(codex_list())})

        result = self.refresh(fake)

        self.assertEqual(fake.calls, [CODEX_LIST_ARGV])
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(
            sorted((item["task_id"], item["status"]) for item in result["evaluated"]),
            [("codex-a", "pass"), ("codex-b", "pass")],
        )


class K8sResourceTests(ProbeCase):
    def test_kubectl_shapes(self) -> None:
        found, fake = self.evaluate(K8S_NAMESPACE, {
            kubectl("k8", "namespace", "default"): ok(KUBECTL_FOUND_NAMESPACE),
        })
        self.assertVerdict(found, "pass")
        self.assertEqual(fake.calls, [("kubectl", "--context", "k8", "get", "namespace", "default", "-o", "name")])

        missing, fake = self.evaluate(K8S_SERVICE, {
            kubectl("k8", "service", "nope-xyz", "default"): exited(1, KUBECTL_NOT_FOUND_STDERR),
        })
        self.assertVerdict(missing, "fail", "service/nope-xyz not found in k8/default")
        self.assertEqual(fake.calls, [(
            "kubectl", "--context", "k8", "-n", "default", "get", "service", "nope-xyz", "-o", "name",
        )])

        no_context = {**K8S_NAMESPACE, "context": "nosuchctx"}
        result, _fake = self.evaluate(no_context, {
            kubectl("nosuchctx", "namespace", "default"): exited(1, KUBECTL_NO_CONTEXT_STDERR),
        })
        self.assertVerdict(result, "fail", "kube context nosuchctx is not configured")

    def test_other_errors_are_unknown(self) -> None:
        argv = kubectl("k8", "service", "nope-xyz", "default")
        for label, response in (
            ("exit without diagnostic", exited(1)),
            ("tool error", checks.CheckToolError("kubectl timed out after 20s")),
        ):
            with self.subTest(label):
                result, _fake = self.evaluate(K8S_SERVICE, {argv: response})
                self.assertVerdict(result, "unknown")

    def test_eks_arn_context_is_passed_verbatim(self) -> None:
        spec = {**K8S_SERVICE, "context": EKS_CONTEXT}
        result, fake = self.evaluate(spec, {
            kubectl(EKS_CONTEXT, "service", "nope-xyz", "default"): ok(KUBECTL_FOUND_SERVICE),
        })
        self.assertVerdict(result, "pass")
        self.assertEqual(fake.calls[0][:3], ("kubectl", "--context", EKS_CONTEXT))


class OpenRouterCreditTests(ProbeCase):
    """F3: the key allowance and the account balance must both cover min_usd."""

    BALANCE_ENV = "OPENROUTER_MANAGEMENT_KEY"

    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ, {
            "OPENROUTER_API_KEY": SECRET_ENV_KEY, self.BALANCE_ENV: SECRET_BALANCE_ENV_KEY,
        })
        env.start()
        self.addCleanup(env.stop)
        self.key_file = self.root / "openrouter.key"
        self.key_file.write_text(SECRET_FILE_KEY + "\n")
        self.balance_file = self.root / "openrouter-management.key"
        self.balance_file.write_text(SECRET_BALANCE_FILE_KEY + "\n")
        self.key = curl(KEY_URL, key_env="OPENROUTER_API_KEY")
        self.own_credits = curl(CREDITS_URL, key_env="OPENROUTER_API_KEY")
        self.balance_credits = curl(CREDITS_URL, key_env=self.BALANCE_ENV)

    def assertNoSecret(self, fake: FakeRunner, *texts: object) -> None:
        for secret in (SECRET_ENV_KEY, SECRET_FILE_KEY, SECRET_BALANCE_ENV_KEY, SECRET_BALANCE_FILE_KEY):
            for argv in fake.calls:
                for item in argv:
                    self.assertNotIn(secret, item)
            for text in texts:
                self.assertNotIn(secret, str(text))

    def test_allowance_below_threshold_fails_without_reading_balance(self) -> None:
        result, fake = self.evaluate(credit(100, balance_key_env=self.BALANCE_ENV), {
            self.key: ok(http(KEY_LIMITED, 200)),
        })
        self.assertVerdict(result, "fail", "74.50")
        self.assertEqual(fake.calls, [self.key])
        self.assertNoSecret(fake, result.detail)

    def test_limited_key_with_allowance_but_zero_balance_fails(self) -> None:
        result, fake = self.evaluate(credit(5, balance_key_env=self.BALANCE_ENV), {
            self.key: ok(http(KEY_LIMITED, 200)),
            self.balance_credits: ok(http(CREDITS_SPENT, 200)),
        })
        self.assertVerdict(result, "fail", "$0.00")
        self.assertEqual(fake.calls, [self.key, self.balance_credits])
        self.assertNoSecret(fake, result.detail)

    def test_limited_key_with_balance_key_passes(self) -> None:
        result, fake = self.evaluate(credit(5, balance_key_env=self.BALANCE_ENV), {
            self.key: ok(http(KEY_LIMITED, 200)),
            self.balance_credits: ok(http(CREDITS, 200)),
        })
        self.assertVerdict(result, "pass", "74.75")
        self.assertEqual(fake.calls, [self.key, self.balance_credits])
        self.assertNoSecret(fake, result.detail)

    def test_management_key_reads_its_own_balance(self) -> None:
        responses = {self.key: ok(http(KEY_UNLIMITED, 200)), self.own_credits: ok(http(CREDITS, 200))}

        passing, fake = self.evaluate(credit(5), responses)
        self.assertVerdict(passing, "pass", "OpenRouter remaining credit $74.75")
        self.assertEqual(fake.calls, [self.key, self.own_credits])
        self.assertNoSecret(fake, passing.detail)

        failing, fake = self.evaluate(credit(80), responses)
        self.assertVerdict(failing, "fail", "OpenRouter remaining credit $74.75 is below $80")
        self.assertNoSecret(fake, failing.detail)

    def test_regular_key_without_balance_key_is_unknown(self) -> None:
        for label, body in (("limited", KEY_LIMITED), ("unlimited", KEY_UNLIMITED_REGULAR)):
            with self.subTest(label):
                result, fake = self.evaluate(credit(5), {self.key: ok(http(body, 200))})
                self.assertVerdict(
                    result, "unknown",
                    "account balance needs a management key (set balance_key_env or balance_key_file)",
                )
                self.assertEqual(fake.calls, [self.key])
                self.assertNoSecret(fake, result.detail)

    def test_balance_key_file_source(self) -> None:
        path = str(self.balance_file)
        balance = curl(CREDITS_URL, key_file=path, file_variable=BALANCE_FILE_VARIABLE)
        result, fake = self.evaluate(credit(5, balance_key_file=path), {
            self.key: ok(http(KEY_UNLIMITED_REGULAR, 200)), balance: ok(http(CREDITS, 200)),
        })
        self.assertVerdict(result, "pass", "74.75")
        self.assertEqual(fake.calls, [self.key, balance])
        self.assertNoSecret(fake, result.detail)

    def test_balance_errors(self) -> None:
        spec = credit(5, balance_key_env=self.BALANCE_ENV)
        rejected, fake = self.evaluate(spec, {
            self.key: ok(http(KEY_UNLIMITED_REGULAR, 200)),
            self.balance_credits: ok(OPENROUTER_401),
        })
        self.assertVerdict(rejected, "fail", "OpenRouter rejected the balance key")
        self.assertNoSecret(fake, rejected.detail)

        forbidden, fake = self.evaluate(spec, {
            self.key: ok(http(KEY_UNLIMITED_REGULAR, 200)),
            self.balance_credits: ok(http(CREDITS_403, 403)),
        })
        self.assertVerdict(forbidden, "unknown")
        self.assertNoSecret(fake, forbidden.detail)

    def test_rejected_key_fails(self) -> None:
        result, fake = self.evaluate(credit(5, balance_key_env=self.BALANCE_ENV), {self.key: ok(OPENROUTER_401)})
        self.assertVerdict(result, "fail", "OpenRouter rejected the key")
        self.assertEqual(fake.calls, [self.key])
        self.assertNoSecret(fake, result.detail)

    def test_missing_key_variable_is_unknown_with_a_clear_detail(self) -> None:
        result, fake = self.evaluate(credit(5), {self.key: exited(2, CURL_VARIABLE_STDERR)})
        self.assertVerdict(
            result, "unknown",
            "OpenRouter key is not available: environment variable OPENROUTER_API_KEY is not set",
        )
        self.assertNotIn("curl --help", result.detail)
        self.assertNoSecret(fake, result.detail)

    def test_unreadable_key_file_is_unknown_with_a_clear_detail(self) -> None:
        path = str(self.root / "missing.key")
        key = curl(KEY_URL, key_file=path)
        result, _fake = self.evaluate(credit(5, key_file=path), {key: exited(2, CURL_VARIABLE_STDERR)})
        self.assertVerdict(result, "unknown", f"key file {path} is unreadable")

    def test_key_file_source(self) -> None:
        path = str(self.key_file)
        key = curl(KEY_URL, key_file=path)
        result, fake = self.evaluate(credit(5, key_file=path, balance_key_env=self.BALANCE_ENV), {
            key: ok(http(KEY_LIMITED, 200)), self.balance_credits: ok(http(CREDITS, 200)),
        })
        self.assertVerdict(result, "pass", "74.75")
        self.assertEqual(fake.calls, [key, self.balance_credits])
        self.assertNoSecret(fake, result.detail)

    def test_key_never_stored(self) -> None:
        path = str(self.key_file)
        self.add("env-credit", checks=[credit(100)])
        self.add("file-credit", checks=[credit(5, key_file=path, balance_key_env=self.BALANCE_ENV)])
        fake = self.runner({
            self.key: ok(OPENROUTER_401),
            curl(KEY_URL, key_file=path): ok(http(KEY_LIMITED, 200)),
            self.balance_credits: ok(http(CREDITS, 200)),
        })

        result = self.refresh(
            fake, task_ids=("env-credit", "file-credit"), due_only=False, max_calls=None, budget_seconds=None,
        )

        self.assertEqual(
            sorted((item["task_id"], item["status"]) for item in result["evaluated"]),
            [("env-credit", "fail"), ("file-credit", "pass")],
        )
        stored = rows(self.queue, "SELECT * FROM check_results")
        tasks = rows(self.queue, "SELECT * FROM tasks")
        readiness = [self.ready(task_id) for task_id in ("env-credit", "file-credit")]
        self.assertNoSecret(fake, stored, tasks, readiness, result)

    def test_regular_key_without_balance_key_stays_held(self) -> None:
        self.add("no-balance", checks=[credit(5)])
        fake = self.runner({self.key: ok(http(KEY_UNLIMITED_REGULAR, 200))})
        self.refresh(fake, now=NOW)
        self.refresh(fake, now=NOW + 3_000)

        later = NOW + checks.UNKNOWN_GRACE_SECONDS + 100
        held = self.ready("no-balance", now=later)
        self.assertFalse(held["ready"], held)
        self.assertEqual((held["state"], held["hold_reason"]), ("waiting", "check_unknown"))
        self.assertIsNone(self.try_claim("no-balance", now=later))
        self.assertEqual(self.attempt_rows(), [])


class CallIdentityTests(ProbeCase):
    """F2: a planned call is (argv, cwd); one claude server in three checkouts is three calls."""

    def setUp(self) -> None:
        super().setUp()
        self.checkouts = [self.mkdir(name) for name in ("checkout-a", "checkout-b", "checkout-c")]
        for index, checkout in enumerate(self.checkouts):
            self.add(f"mcp-{index}", cwd=str(checkout), checks=[MCP_CLAUDE])

    def test_planned_calls_carry_the_runner_cwd(self) -> None:
        checkout = str(self.checkouts[0])
        self.assertEqual(
            checks.planned_calls(checks.normalize_check(MCP_CLAUDE), checkout),
            ((claude_get(), checkout),),
        )
        self.assertEqual(
            checks.planned_calls(checks.normalize_check(MCP_CODEX_LINEAR), checkout),
            ((CODEX_LIST_ARGV, checkout),),
        )
        self.assertEqual(
            checks.planned_calls(checks.normalize_check(K8S_SERVICE), checkout),
            ((kubectl("k8", "service", "nope-xyz", "default"), None),),
        )

    def test_cap_counts_one_call_per_checkout(self) -> None:
        fake = self.runner({claude_get(): ok(CLAUDE_CONNECTED)})

        result = self.refresh(fake, max_calls=1, budget_seconds=None)

        self.assertEqual(fake.calls, [claude_get()])
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(len(result["evaluated"]), 1)
        self.assertEqual(result["deferred"], 2)

    def test_uncapped_refresh_runs_one_call_per_checkout(self) -> None:
        fake = self.runner({claude_get(): ok(CLAUDE_CONNECTED)})

        result = self.refresh(fake, max_calls=None, budget_seconds=None)

        self.assertEqual(fake.calls, [claude_get()] * 3)
        self.assertEqual(sorted(fake.cwds), sorted(str(path) for path in self.checkouts))
        self.assertEqual(result["tool_calls"], 3)
        self.assertEqual(sorted(item["status"] for item in result["evaluated"]), ["pass"] * 3)


class ProbeHoldingTests(ProbeCase):
    def test_failing_probe_holds_without_attempt_then_releases(self) -> None:
        self.add("needs-svc", checks=[K8S_SERVICE])
        argv = kubectl("k8", "service", "nope-xyz", "default")
        fake = self.runner({argv: [exited(1, KUBECTL_NOT_FOUND_STDERR), ok(KUBECTL_FOUND_SERVICE)]})

        self.refresh(fake, task_ids=("needs-svc",), due_only=False, max_calls=None, budget_seconds=None)

        status = self.ready("needs-svc")
        self.assertEqual((status["state"], status["hold_reason"]), ("waiting", "check_failed"))
        self.assertNotIn("needs-svc", self.eligible_ids())
        self.assertIsNone(self.try_claim("needs-svc"))
        self.assertEqual(self.attempt_rows(), [])

        later = NOW + checks.RETRY_TTL_SECONDS
        tick = checks.refresh(self.queue, runner=fake, now_epoch=later)

        self.assertEqual(tick["tool_calls"], 1)
        released = self.ready("needs-svc", now=later + 10)
        self.assertTrue(released["ready"], released)
        self.assertEqual((released["state"], released["hold_reason"]), ("ready", None))
        self.assertEqual(self.attempt_rows(), [])

    def test_probe_unknown_past_grace_stays_held(self) -> None:
        self.add("probe-unknown", checks=[K8S_SERVICE])
        self.add("issue-unknown", checks=[ISSUE_OPEN_3815])
        fake = self.runner({
            kubectl("k8", "service", "nope-xyz", "default"): checks.CheckToolError("kubectl timed out after 20s"),
            gh_issue(3815): exited(1, GH_OFFLINE_STDERR),
        })
        self.refresh(fake, now=NOW)
        self.refresh(fake, now=NOW + 3_000)

        later = NOW + checks.UNKNOWN_GRACE_SECONDS + 100
        held = self.ready("probe-unknown", now=later)
        self.assertFalse(held["ready"], held)
        self.assertEqual((held["state"], held["hold_reason"]), ("waiting", "check_unknown"))
        self.assertEqual([item["status"] for item in held["checks"]], ["unknown"])
        self.assertNotIn("probe-unknown", self.eligible_ids(now=later))
        self.assertIsNone(self.try_claim("probe-unknown", now=later))
        self.assertEqual(self.queue.attempts(task_id="probe-unknown"), [])

        # Existing types keep the grace period: an unknown issue check turns unverified.
        degraded = self.ready("issue-unknown", now=later)
        self.assertTrue(degraded["ready"], degraded)
        self.assertEqual([item["status"] for item in degraded["checks"]], ["unverified"])


class ProbeCliTests(HermeticEnvironment, ProbeCase):
    def setUp(self) -> None:
        super().setUp()
        self.hermetic(self.mkdir("home"))
        self.cfg = runtime(self.queue.path)
        now_patch = mock.patch.dict(os.environ, {"BONUS_DRAIN_NOW": str(NOW)})
        now_patch.start()
        self.addCleanup(now_patch.stop)

    def test_add_with_non_text_provider_is_invalid_input(self) -> None:
        for provider in ([], {}):
            with self.subTest(provider=provider):
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(cli, "_queue", return_value=(self.cfg, self.queue)),
                    mock.patch.object(checks, "subprocess_runner", self.runner()),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    captured_json() as payloads,
                ):
                    code = cli.main([
                        "add", "--database", str(self.queue.path), "--id", "bad-mcp", "--title", "bad-mcp",
                        "--kind", "oneoff", "--size", "small", "--cwd", str(self.root),
                        "--goal", "complete bad-mcp", "--readiness-review", review_json(goal="complete bad-mcp"),
                        "--check", json.dumps({"type": "mcp_authenticated", "provider": provider, "server": "linear-cn"}),
                    ])
                self.assertEqual(code, 2, (payloads, stdout.getvalue(), stderr.getvalue()))
                self.assertEqual(payloads[-1]["code"], "invalid_input")
                self.assertIsNone(self.queue.task("bad-mcp"))

    def test_add_with_failing_probe_is_refused_and_inserts_nothing(self) -> None:
        fake = self.runner({
            kubectl("k8", "service", "nope-xyz", "default"): exited(1, KUBECTL_NOT_FOUND_STDERR),
        })
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(cli, "_queue", return_value=(self.cfg, self.queue)),
            mock.patch.object(checks, "subprocess_runner", fake),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            captured_json() as payloads,
        ):
            code = cli.main([
                "add", "--database", str(self.queue.path), "--id", "needs-svc", "--title", "needs-svc",
                "--kind", "oneoff", "--size", "small", "--cwd", str(self.root),
                "--goal", "complete needs-svc", "--readiness-review", review_json(goal="complete needs-svc"),
                "--check", json.dumps(K8S_SERVICE),
            ])
        out, err = stdout.getvalue(), stderr.getvalue()

        # The missing service is a precondition that does not exist now and no queued
        # prerequisite will create it: the task cannot start, so it is not queued.
        self.assertEqual(code, 2, (payloads, out, err))
        self.assertEqual(payloads[-1]["code"], "invalid_input")
        self.assertIn("add refused: launch check fails now", payloads[-1]["error"])
        self.assertIn("service/nope-xyz not found in k8/default", payloads[-1]["error"])
        self.assertIn("CHECK FAILED", err)
        self.assertIn(checks.describe(K8S_SERVICE), err)
        self.assertIsNone(self.queue.task("needs-svc"))
        self.assertEqual(self.attempt_rows(), [])
