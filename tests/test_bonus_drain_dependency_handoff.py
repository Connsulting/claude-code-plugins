"""Local Git handoff and end-to-end dependency recovery contracts."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
CLI = SKILL_ROOT / "bin" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import config as config_module
from bonus_drain import db, dispatcher, handoff

NOW = 2_000_000_000
KEY = "alpha-account/alpha-weekly/2000001000"
BASE_FIELDS = {"base_oid", "branch_ref", "target_ref", "parent_ids"}


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def task(task_id: str, cwd: Path, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": task_id,
        "title": task_id,
        "kind": "oneoff",
        "priority": 2,
        "cwd": str(cwd),
        "goal": f"complete {task_id}",
        "done_when": f"proof for {task_id} is retained",
        "created_at": iso(NOW - 60),
        "active": True,
        "size": "small",
    }
    value.update(changes)
    return value


def failed_outcome(signature: str = "tests:failed") -> dict[str, object]:
    return {
        "reason": {
            "code": "retryable",
            "detail": "the local test command failed",
            "signature": signature,
        },
    }


def verified_outcome(
    repository: dict[str, object], evidence: str,
) -> dict[str, object]:
    return {
        "reason": {
            "code": "done_when_verified",
            "detail": "the done-when was checked",
            "signature": "done_when_verified:git-fixture",
        },
        "completion": {
            "verified": True,
            "mechanism": "command",
            "evidence": [evidence],
        },
        "repository": repository,
    }


def runtime(database: Path, router: Path | str = "/bin/true") -> config_module.RuntimeConfig:
    return config_module.RuntimeConfig(
        schema_version=1,
        source_path=None,
        database=database,
        record_command=(str(CLI), "record"),
        secret_refs=(),
        adapters=(
            config_module.AdapterConfig(
                "router", "agent-router", (str(router),),
                timeout_seconds=10, max_output_bytes=65_536,
            ),
        ),
        providers=(
            config_module.ProviderConfig(
                "alpha", config_module.DispatchBinding("router", "alpha"),
                frozenset(), "single",
            ),
        ),
        plans=(config_module.PlanConfig("alpha-plan", "alpha"),),
        accounts=(
            config_module.AccountConfig(
                "alpha-account", "alpha", "alpha-plan",
            ),
        ),
        limits=(
            config_module.LimitConfig(
                "alpha-weekly", "alpha-plan", 604_800, 95, 20_000, 6,
            ),
        ),
        viewer={},
        pr_exceptions=(),
        usage_max_age_seconds=3_600,
        cache_dir=database.parent / "cache",
    )


class RepositoryHandoffCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queue = db.QueueDB(self.root / "queue.db")
        self.queue.initialize()

    def git(self, cwd: Path, *argv: str) -> str:
        return subprocess.run(
            ["git", *argv], cwd=cwd, text=True, capture_output=True, check=True,
        ).stdout.strip()

    def repository(self, name: str) -> tuple[Path, Path, str]:
        bare = self.root / f"{name}.git"
        work = self.root / f"{name}-work"
        subprocess.run(
            ["git", "init", "--bare", str(bare)], check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "clone", str(bare), str(work)], check=True, capture_output=True,
        )
        self.git(work, "config", "user.email", "fixture@example.test")
        self.git(work, "config", "user.name", "Fixture")
        (work / "base.txt").write_text("base\n", encoding="utf-8")
        self.git(work, "add", "base.txt")
        self.git(work, "commit", "-m", "base")
        self.git(work, "branch", "-M", "main")
        self.git(work, "push", "-u", "origin", "main")
        return bare.resolve(), work, self.git(work, "rev-parse", "HEAD")

    def branch(
        self, work: Path, name: str, start: str, filename: str, content: str,
    ) -> str:
        self.git(work, "switch", "-C", name, start)
        (work / filename).write_text(content, encoding="utf-8")
        self.git(work, "add", filename)
        self.git(work, "commit", "-m", name)
        self.git(work, "push", "-f", "origin", f"HEAD:refs/heads/{name}")
        return self.git(work, "rev-parse", "HEAD")

    @staticmethod
    def handoff(
        remote: Path,
        target_base_oid: str,
        branch: str,
        head_oid: str,
        *,
        integration_state: str = "unmerged",
        receipt: dict[str, object] | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "remote": str(remote),
            "target_ref": "refs/heads/main",
            "target_base_oid": target_base_oid,
            "branch_ref": f"refs/heads/{branch}",
            "head_oid": head_oid,
            "integration_state": integration_state,
        }
        if receipt is not None:
            value["merge_receipt"] = receipt
        return value

    def complete_parent(
        self, task_id: str, work: Path, repository: dict[str, object],
    ) -> None:
        self.queue.add_task(task(task_id, work))
        key = f"manual/{task_id}"
        attempt = self.queue.claim(
            task_id, key, "alpha", "alpha-account", now_epoch=NOW,
        )
        self.assertIsNotNone(attempt)
        self.queue.record(
            task_id,
            key,
            attempt_id=attempt.id,
            status="done",
            outcome=verified_outcome(repository, f"git:{repository['head_oid']}"),
            provider_id="alpha",
            account_id="alpha-account",
            timestamp=iso(NOW),
            now_epoch=NOW,
            summary=f"verified {task_id}",
        )

    def launch(self, task_id: str, work: Path, parents: list[str]) -> str:
        self.queue.add_task(task(task_id, work, depends_on=parents))
        result = dispatcher.dispatch(
            runtime(self.queue.path),
            self.queue,
            task_id=task_id,
            eligibility_key=f"manual/{task_id}",
            requested_provider="alpha",
            router_call=lambda *_args, **_kwargs: {
                "provider": "alpha",
                "dispatch": {"job_id": f"job-{task_id}", "launched": True},
            },
        )
        return result.prompt

    def dependency_base(self, prompt: str) -> dict[str, object]:
        match = re.search(r"(?m)^DEPENDENCY_BASE(?:=|\s+)(\{[^\n]+\})$", prompt)
        self.assertIsNotNone(match, prompt)
        value = json.loads(match.group(1))
        self.assertEqual(set(value), BASE_FIELDS)
        return value

    def assert_rejected(
        self, child: str, work: Path, parent: str, expected_hold: str,
    ) -> None:
        self.queue.add_task(task(child, work, depends_on=[parent]))
        readiness = self.queue.readiness(child, now_epoch=NOW)
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["hold_reason"], expected_hold)
        router = mock.Mock()
        with self.assertRaises(dispatcher.DispatchError):
            dispatcher.dispatch(
                runtime(self.queue.path),
                self.queue,
                task_id=child,
                eligibility_key=f"manual/{child}",
                requested_provider="alpha",
                router_call=router,
            )
        router.assert_not_called()

    def test_unmerged_merge_receipt_squash_receipt_and_receipt_free_equivalence(self) -> None:
        remote, work, initial = self.repository("selection")

        unmerged_head = self.branch(
            work, "task/unmerged", initial, "unmerged.txt", "unmerged\n",
        )
        self.complete_parent(
            "unmerged",
            work,
            self.handoff(remote, initial, "task/unmerged", unmerged_head),
        )
        self.assertEqual(
            self.dependency_base(self.launch("from-unmerged", work, ["unmerged"])),
            {
                "base_oid": unmerged_head,
                "branch_ref": "refs/heads/task/unmerged",
                "target_ref": "refs/heads/main",
                "parent_ids": ["unmerged"],
            },
        )

        self.git(work, "switch", "main")
        self.git(work, "merge", "--no-ff", "task/unmerged", "-m", "merge parent")
        self.git(work, "push", "origin", "main")
        merged_tip = self.git(work, "rev-parse", "HEAD")
        self.complete_parent(
            "merged",
            work,
            self.handoff(
                remote,
                initial,
                "task/unmerged",
                unmerged_head,
                integration_state="merged",
                receipt={"kind": "merge", "result_oid": merged_tip},
            ),
        )
        self.assertEqual(
            self.dependency_base(self.launch("from-merge", work, ["merged"])),
            {
                "base_oid": merged_tip,
                "branch_ref": "refs/heads/main",
                "target_ref": "refs/heads/main",
                "parent_ids": ["merged"],
            },
        )

        equivalent_head = self.branch(
            work, "task/equivalent", merged_tip, "equivalent.txt", "same bytes\n",
        )
        self.git(work, "switch", "main")
        self.git(work, "checkout", "task/equivalent", "--", "equivalent.txt")
        self.git(work, "add", "equivalent.txt")
        self.git(work, "commit", "-m", "equivalent content")
        self.git(work, "push", "origin", "main")
        equivalent_tip = self.git(work, "rev-parse", "HEAD")
        self.complete_parent(
            "equivalent",
            work,
            self.handoff(
                remote,
                merged_tip,
                "task/equivalent",
                equivalent_head,
                integration_state="merged",
            ),
        )
        self.assertEqual(
            self.dependency_base(self.launch("from-equivalent", work, ["equivalent"])),
            {
                "base_oid": equivalent_tip,
                "branch_ref": "refs/heads/main",
                "target_ref": "refs/heads/main",
                "parent_ids": ["equivalent"],
            },
        )

        squash_head = self.branch(
            work, "task/squash", equivalent_tip, "squash.txt", "squashed bytes\n",
        )
        self.git(work, "switch", "main")
        self.git(work, "checkout", "task/squash", "--", "squash.txt")
        self.git(work, "add", "squash.txt")
        self.git(work, "commit", "-m", "squashed equivalent")
        self.git(work, "push", "origin", "main")
        squash_tip = self.git(work, "rev-parse", "HEAD")
        self.complete_parent(
            "squashed",
            work,
            self.handoff(
                remote,
                equivalent_tip,
                "task/squash",
                squash_head,
                integration_state="merged",
                receipt={"kind": "squash", "result_oid": squash_tip},
            ),
        )
        self.assertEqual(
            self.dependency_base(self.launch("from-squash", work, ["squashed"])),
            {
                "base_oid": squash_tip,
                "branch_ref": "refs/heads/main",
                "target_ref": "refs/heads/main",
                "parent_ids": ["squashed"],
            },
        )

    def test_recorded_ancestor_selects_target_after_generated_index_changes(self) -> None:
        remote, work, base = self.repository("generated-index")
        head = self.branch(
            work, "task/parent", base, "adr-index.md", "ADR 1\n",
        )
        self.complete_parent(
            "parent", work, self.handoff(remote, base, "task/parent", head),
        )
        self.git(work, "switch", "main")
        self.git(work, "merge", "--ff-only", "task/parent")
        (work / "adr-index.md").write_text("ADR 1\nADR 2\n", encoding="utf-8")
        self.git(work, "add", "adr-index.md")
        self.git(work, "commit", "-m", "Regenerate ADR index")
        self.git(work, "push", "origin", "main")
        target = self.git(work, "rev-parse", "HEAD")

        self.assertEqual(
            self.dependency_base(self.launch("child", work, ["parent"])),
            {
                "base_oid": target,
                "branch_ref": "refs/heads/main",
                "target_ref": "refs/heads/main",
                "parent_ids": ["parent"],
            },
        )

    def test_verified_merge_and_squash_resolve_target_after_source_branch_deletion(self) -> None:
        remote, work, base = self.repository("deleted-integrated-branches")
        merged_head = self.branch(
            work, "task/merged-deleted", base, "merged.txt", "merged\n",
        )
        self.git(work, "switch", "main")
        self.git(work, "merge", "--no-ff", "task/merged-deleted", "-m", "merge deleted branch")
        self.git(work, "push", "origin", "main")
        merged_target = self.git(work, "rev-parse", "HEAD")
        self.git(work, "push", "origin", "--delete", "task/merged-deleted")
        self.complete_parent(
            "merged-deleted", work,
            self.handoff(
                remote, base, "task/merged-deleted", merged_head,
                integration_state="merged",
                receipt={"kind": "merge", "result_oid": merged_target},
            ),
        )
        self.queue.add_task(task(
            "merged-deleted-child", work, depends_on=["merged-deleted"],
        ))
        self.assertEqual(
            self.queue.dependency_base("merged-deleted-child"),
            {
                "base_oid": merged_target,
                "branch_ref": "refs/heads/main",
                "target_ref": "refs/heads/main",
                "parent_ids": ["merged-deleted"],
            },
        )

        squash_head = self.branch(
            work, "task/squash-deleted", merged_target, "squash-deleted.txt", "squash\n",
        )
        self.git(work, "switch", "main")
        self.git(work, "checkout", "task/squash-deleted", "--", "squash-deleted.txt")
        self.git(work, "add", "squash-deleted.txt")
        self.git(work, "commit", "-m", "squash deleted branch")
        self.git(work, "push", "origin", "main")
        squash_target = self.git(work, "rev-parse", "HEAD")
        self.git(work, "push", "origin", "--delete", "task/squash-deleted")
        self.complete_parent(
            "squash-deleted", work,
            self.handoff(
                remote, merged_target, "task/squash-deleted", squash_head,
                integration_state="merged",
                receipt={"kind": "squash", "result_oid": squash_target},
            ),
        )
        self.queue.add_task(task(
            "squash-deleted-child", work, depends_on=["squash-deleted"],
        ))
        self.assertEqual(
            self.queue.dependency_base("squash-deleted-child"),
            {
                "base_oid": squash_target,
                "branch_ref": "refs/heads/main",
                "target_ref": "refs/heads/main",
                "parent_ids": ["squash-deleted"],
            },
        )

    def test_rename_receipt_rejects_target_that_retains_the_deleted_source(self) -> None:
        remote, work, base = self.repository("rename-equivalence")
        self.git(work, "config", "diff.renames", "true")
        self.git(work, "switch", "-C", "task/rename", base)
        self.git(work, "mv", "base.txt", "renamed.txt")
        self.git(work, "commit", "-m", "rename source")
        self.git(work, "push", "-f", "origin", "HEAD:refs/heads/task/rename")
        head = self.git(work, "rev-parse", "HEAD")

        self.git(work, "switch", "main")
        (work / "renamed.txt").write_text("base\n", encoding="utf-8")
        self.git(work, "add", "renamed.txt")
        self.git(work, "commit", "-m", "copy without deleting source")
        self.git(work, "push", "origin", "main")
        target = self.git(work, "rev-parse", "HEAD")
        self.complete_parent(
            "false-rename-receipt", work,
            self.handoff(
                remote, base, "task/rename", head, integration_state="merged",
                receipt={"kind": "squash", "result_oid": target},
            ),
        )

        self.assert_rejected(
            "false-rename-child", work, "false-rename-receipt",
            "dependency_integration_ambiguous",
        )

    def test_false_receipt_and_remote_ref_object_or_branch_identity_mismatch_hold(self) -> None:
        remote, work, base = self.repository("rejections")
        head = self.branch(work, "task/parent", base, "parent.txt", "parent delta\n")
        other = self.branch(work, "task/other", base, "other.txt", "other delta\n")

        cases: list[tuple[str, dict[str, object], str]] = []
        false_receipt = self.handoff(
            remote,
            base,
            "task/parent",
            head,
            integration_state="merged",
            receipt={"kind": "squash", "result_oid": base},
        )
        cases.append(("false-receipt", false_receipt, "dependency_integration_ambiguous"))

        wrong_remote = self.handoff(remote, base, "task/parent", head)
        wrong_remote["remote"] = str(self.root / "similarly-named.git")
        cases.append(("wrong-remote", wrong_remote, "dependency_ref_unavailable"))

        missing_target = self.handoff(remote, base, "task/parent", head)
        missing_target["target_ref"] = "refs/heads/similar-main"
        cases.append(("missing-target-ref", missing_target, "dependency_ref_unavailable"))

        missing_object = self.handoff(remote, base, "task/parent", "f" * 40)
        cases.append(("missing-object", missing_object, "dependency_ref_unavailable"))

        mismatched_branch = self.handoff(remote, base, "task/parent", other)
        cases.append(
            ("mismatched-branch-head", mismatched_branch, "dependency_integration_ambiguous")
        )

        for name, repository, hold in cases:
            with self.subTest(case=name):
                self.complete_parent(name, work, repository)
                self.assert_rejected(f"{name}-child", work, name, hold)

    def test_compatible_parent_heads_choose_descendant_and_divergent_heads_hold(self) -> None:
        remote, work, base = self.repository("multiple")
        first = self.branch(work, "task/first", base, "first.txt", "first\n")
        descendant = self.branch(
            work, "task/descendant", first, "descendant.txt", "descendant\n",
        )
        self.complete_parent(
            "first", work, self.handoff(remote, base, "task/first", first),
        )
        self.complete_parent(
            "descendant",
            work,
            self.handoff(remote, base, "task/descendant", descendant),
        )
        self.assertEqual(
            self.dependency_base(
                self.launch("compatible-child", work, ["descendant", "first"]),
            ),
            {
                "base_oid": descendant,
                "branch_ref": "refs/heads/task/descendant",
                "target_ref": "refs/heads/main",
                "parent_ids": ["descendant", "first"],
            },
        )

        left = self.branch(work, "task/left", base, "left.txt", "left\n")
        right = self.branch(work, "task/right", base, "right.txt", "right\n")
        self.complete_parent("left", work, self.handoff(remote, base, "task/left", left))
        self.complete_parent("right", work, self.handoff(remote, base, "task/right", right))
        self.queue.add_task(task("divergent-child", work, depends_on=["left", "right"]))
        readiness = self.queue.readiness("divergent-child", now_epoch=NOW)
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["hold_reason"], "integration_required")
        router = mock.Mock()
        with self.assertRaises(dispatcher.DispatchError):
            dispatcher.dispatch(
                runtime(self.queue.path),
                self.queue,
                task_id="divergent-child",
                eligibility_key="manual/divergent-child",
                requested_provider="alpha",
                router_call=router,
            )
        router.assert_not_called()

    def test_multi_parent_target_move_between_resolutions_requires_integration(self) -> None:
        remote, work, base = self.repository("moving-target")
        first = self.branch(work, "task/first", base, "first.txt", "first\n")
        descendant = self.branch(
            work, "task/descendant", first, "descendant.txt", "descendant\n",
        )
        self.git(work, "switch", "main")
        (work / "target.txt").write_text("target moved\n", encoding="utf-8")
        self.git(work, "add", "target.txt")
        self.git(work, "commit", "-m", "move target")
        self.git(work, "push", "origin", "main")
        moved_target = self.git(work, "rev-parse", "HEAD")

        self.complete_parent(
            "first-moving-target", work,
            self.handoff(remote, base, "task/first", first),
        )
        self.complete_parent(
            "descendant-moving-target", work,
            self.handoff(remote, base, "task/descendant", descendant),
        )
        self.queue.add_task(task(
            "moving-target-child", work,
            depends_on=["first-moving-target", "descendant-moving-target"],
        ))

        real_remote_ref_oid = handoff._remote_ref_oid
        observed_targets = iter((base, moved_target))

        def resolve_with_target_move(cwd: Path, remote_name: str, ref: str) -> str:
            if ref == "refs/heads/main":
                return next(observed_targets)
            return real_remote_ref_oid(cwd, remote_name, ref)

        with mock.patch.object(
            handoff, "_remote_ref_oid", side_effect=resolve_with_target_move,
        ):
            readiness = self.queue.readiness("moving-target-child", now_epoch=NOW)

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["hold_reason"], "integration_required")
        self.assertIn("do not share one exact target", readiness["reason"])


class FullLocalDependencyJourney(RepositoryHandoffCase):
    def write_router(self) -> tuple[Path, Path]:
        router = self.root / "agent-router"
        log = self.root / "router.jsonl"
        router.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"log = pathlib.Path({str(log)!r})\n"
            "argv = sys.argv[1:]\n"
            "if argv and argv[0] == 'status':\n"
            "    print(json.dumps({'rows': []}))\n"
            "    raise SystemExit(0)\n"
            "prompt = ''\n"
            "if '--prompt-file' in argv:\n"
            "    prompt = pathlib.Path(argv[argv.index('--prompt-file') + 1]).read_text()\n"
            "else:\n"
            "    for value in argv:\n"
            "        if value.startswith('--prompt-file='):\n"
            "            prompt = pathlib.Path(value.split('=', 1)[1]).read_text()\n"
            "            break\n"
            "    if not prompt and '--json' in argv and argv.index('--json') + 1 < len(argv):\n"
            "        prompt = argv[argv.index('--json') + 1]\n"
            "rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []\n"
            "job_id = f'local-job-{len(rows) + 1}'\n"
            "rows.append({'job_id': job_id, 'argv': argv, 'prompt': prompt})\n"
            "log.write_text(''.join(json.dumps(row) + '\\n' for row in rows))\n"
            "print(json.dumps({'provider': 'alpha', 'dispatch': "
            "{'job_id': job_id, 'launched': True}}))\n",
            encoding="utf-8",
        )
        router.chmod(0o755)
        return router, log

    def write_config(self, router: Path) -> Path:
        config = self.root / "config.json"
        config.write_text(
            json.dumps({
                "schema_version": 1,
                "database": str(self.queue.path),
                "cache_dir": str(self.root / "cache"),
                "record_command": [str(CLI), "record"],
                "secret_refs": [],
                "adapters": [{
                    "id": "router",
                    "kind": "agent-router",
                    "argv": [str(router)],
                    "timeout_seconds": 10,
                    "max_output_bytes": 65_536,
                }],
                "providers": [{
                    "id": "alpha",
                    "account_mode": "single",
                    "dispatch": {"adapter_id": "router", "provider": "alpha"},
                }],
                "plans": [{"id": "alpha-plan", "provider_id": "alpha"}],
                "accounts": [{
                    "id": "alpha-account",
                    "provider_id": "alpha",
                    "plan_id": "alpha-plan",
                }],
                "limits": [{
                    "id": "alpha-weekly",
                    "plan_id": "alpha-plan",
                    "window_seconds": 604_800,
                    "ceiling_percent": 95,
                    "lead_seconds": 20_000,
                    "batch_size": 6,
                }],
                "viewer": {},
                "pr_exceptions": [],
            }, sort_keys=True),
            encoding="utf-8",
        )
        return config

    def write_usage(self, captured_at: int) -> None:
        cache = self.root / "cache" / "usage" / "alpha"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "alpha-account.json").write_text(
            json.dumps({
                "provider_id": "alpha",
                "account_id": "alpha-account",
                "captured_at": captured_at,
                "limits": {
                    "alpha-weekly": {
                        "used_percent": 20,
                        "resets_at": NOW + 1_000,
                    },
                },
            }),
            encoding="utf-8",
        )

    def cli(self, config: Path, *argv: str) -> dict[str, object]:
        completed = subprocess.run(
            [
                str(CLI),
                *argv,
                "--config", str(config),
                "--database", str(self.queue.path),
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"CLI failed: {completed.stdout}\n{completed.stderr}",
        )
        return json.loads(completed.stdout.strip().splitlines()[-1])

    @staticmethod
    def sql_rows(
        database: Path, sql: str, parameters: tuple[object, ...] = (),
    ) -> list[dict[str, object]]:
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, parameters)]

    def test_real_cli_scout_git_and_fake_router_prove_a_b_recovery_c_handoff(self) -> None:
        remote, work, target_base = self.repository("journey")
        a_head = self.branch(work, "task/a", target_base, "a.txt", "A\n")
        router, router_log = self.write_router()
        config = self.write_config(router)
        for value in (
            task("a", work),
            task("b", work, depends_on=["a"]),
            task("c", work, depends_on=["b"]),
        ):
            self.queue.add_task(value)

        a_dispatch = self.cli(config, "dispatch", "a", "alpha", "--now", str(NOW))[
            "dispatch"
        ]
        a_claim = self.queue.claim_for("a")
        self.assertIsNotNone(a_claim)
        self.queue.record(
            "a",
            a_dispatch["eligibility_key"],
            attempt_id=a_claim.attempt_id,
            status="done",
            outcome=verified_outcome(
                self.handoff(remote, target_base, "task/a", a_head), f"git:{a_head}",
            ),
            provider_id="alpha",
            account_id="alpha-account",
            timestamp=iso(NOW),
            now_epoch=NOW,
            summary="A verified",
        )

        b_dispatch = self.cli(config, "dispatch", "b", "alpha", "--now", str(NOW))[
            "dispatch"
        ]
        self.assertEqual(
            self.dependency_base(b_dispatch["prompt"]),
            {
                "base_oid": a_head,
                "branch_ref": "refs/heads/task/a",
                "target_ref": "refs/heads/main",
                "parent_ids": ["a"],
            },
        )
        b_first = self.queue.claim_for("b")
        self.assertIsNotNone(b_first)
        self.queue.record(
            "b",
            b_dispatch["eligibility_key"],
            attempt_id=b_first.attempt_id,
            status="failed",
            outcome=failed_outcome(),
            provider_id="alpha",
            account_id="alpha-account",
            timestamp=iso(NOW),
            now_epoch=NOW,
            summary="tests failed",
        )

        self.write_usage(NOW + 300)
        recovery_report = self.cli(config, "scout", "--now", str(NOW + 300))
        self.assertEqual(
            [item["task_id"] for item in recovery_report["dispatched"]], ["b"],
        )
        b_recovery = recovery_report["dispatched"][0]
        self.assertEqual(
            self.dependency_base(b_recovery["prompt"]),
            {
                "base_oid": a_head,
                "branch_ref": "refs/heads/task/a",
                "target_ref": "refs/heads/main",
                "parent_ids": ["a"],
            },
        )
        b_retry = self.queue.claim_for("b")
        self.assertIsNotNone(b_retry)
        self.assertNotEqual(b_retry.attempt_id, b_first.attempt_id)

        b_head = self.branch(work, "task/b", a_head, "b.txt", "B\n")
        self.queue.record(
            "b",
            b_recovery["eligibility_key"],
            attempt_id=b_retry.attempt_id,
            status="done",
            outcome=verified_outcome(
                self.handoff(remote, target_base, "task/b", b_head), f"git:{b_head}",
            ),
            provider_id="alpha",
            account_id="alpha-account",
            timestamp=iso(NOW + 301),
            now_epoch=NOW + 301,
            summary="B recovered and verified",
        )

        self.write_usage(NOW + 301)
        child_report = self.cli(config, "scout", "--now", str(NOW + 301))
        self.assertEqual([item["task_id"] for item in child_report["dispatched"]], ["c"])
        self.assertEqual(
            self.dependency_base(child_report["dispatched"][0]["prompt"]),
            {
                "base_oid": b_head,
                "branch_ref": "refs/heads/task/b",
                "target_ref": "refs/heads/main",
                "parent_ids": ["b"],
            },
        )

        attempts = self.sql_rows(
            self.queue.path,
            "SELECT ordinal,state FROM task_attempts WHERE task_id='b' ORDER BY ordinal",
        )
        self.assertEqual(attempts, [{"ordinal": 1, "state": "failed"}, {"ordinal": 2, "state": "done"}])
        self.assertEqual(
            [row["status"] for row in self.sql_rows(
                self.queue.path,
                "SELECT status FROM runs WHERE task='b' ORDER BY rowid_pk",
            )],
            ["dispatched", "failed", "dispatched", "done"],
        )
        router_rows = [
            json.loads(line)
            for line in router_log.read_text(encoding="utf-8").splitlines()
        ]
        job_ids = [row["job_id"] for row in router_rows]
        self.assertEqual(job_ids, ["local-job-1", "local-job-2", "local-job-3", "local-job-4"])
        self.assertEqual(len(job_ids), len(set(job_ids)))
        self.assertEqual(
            [
                self.dependency_base(router_rows[index]["prompt"])["base_oid"]
                for index in (1, 2, 3)
            ],
            [a_head, a_head, b_head],
        )


if __name__ == "__main__":
    unittest.main()
