import importlib.util
import subprocess
import sys
from argparse import Namespace
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location(
    "semantic_diff",
    PLUGIN_ROOT / "scripts" / "semantic-diff.py",
)
SEMANTIC_DIFF = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SEMANTIC_DIFF
SPEC.loader.exec_module(SEMANTIC_DIFF)


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], repo)
    _run(["git", "config", "user.name", "Test User"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)

    (repo / "base.txt").write_text("line-1\n", encoding="utf-8")
    _run(["git", "add", "base.txt"], repo)
    _run(["git", "commit", "-m", "initial"], repo)
    return repo


def test_parse_range_expression_variants():
    two_dot = SEMANTIC_DIFF.parse_range_expression("main..feature")
    three_dot = SEMANTIC_DIFF.parse_range_expression("main...feature")
    single_ref = SEMANTIC_DIFF.parse_range_expression("HEAD~2")

    assert two_dot.range_expr == "main..feature"
    assert two_dot.base_ref == "main"
    assert two_dot.head_ref == "feature"
    assert two_dot.operator == ".."

    assert three_dot.range_expr == "main...feature"
    assert three_dot.base_ref == "main"
    assert three_dot.head_ref == "feature"
    assert three_dot.operator == "..."

    assert single_ref.range_expr == "HEAD~2..HEAD"
    assert single_ref.base_ref == "HEAD~2"
    assert single_ref.head_ref == "HEAD"
    assert single_ref.operator == ".."


def test_resolve_selection_rejects_conflicting_modes():
    args = Namespace(range="main..HEAD", staged=True, working_tree=False)
    try:
        SEMANTIC_DIFF.resolve_selection(args)
        assert False, "Expected conflicting mode validation to fail."
    except SEMANTIC_DIFF.ArgumentError as exc:
        assert "either --range or --staged/--working-tree" in str(exc)


def test_invalid_range_ref_returns_actionable_error(tmp_path):
    repo = _init_repo(tmp_path)
    selection = SEMANTIC_DIFF.parse_range_expression("not-a-ref..HEAD")

    error = SEMANTIC_DIFF.validate_selection(str(repo), selection)
    assert error is not None
    assert error["status"] == "error"
    assert error["code"] == "invalid_ref"
    assert "not-a-ref" in error["message"]


def test_no_diff_returns_empty_status(tmp_path):
    repo = _init_repo(tmp_path)
    selection = SEMANTIC_DIFF.DiffSelection(mode="workspace")

    payload = SEMANTIC_DIFF.collect_semantic_diff(
        repo_root=str(repo),
        selection=selection,
    )

    assert payload["status"] == "empty"
    assert payload["summary"]["files_changed"] == 0
    assert payload["files"] == []
    assert "stage" in payload["message"]


def test_rename_and_numstat_are_captured_for_staged_changes(tmp_path):
    repo = _init_repo(tmp_path)
    original = repo / "base.txt"
    renamed = repo / "renamed.txt"
    original.rename(renamed)

    _run(["git", "add", "-A"], repo)

    payload = SEMANTIC_DIFF.collect_semantic_diff(
        repo_root=str(repo),
        selection=SEMANTIC_DIFF.DiffSelection(mode="staged"),
    )

    assert payload["status"] == "ok"
    assert payload["summary"]["files_changed"] == 1
    assert payload["summary"]["renamed_files"] == 1
    assert payload["summary"]["status_counts"]["R"] == 1

    file_row = payload["files"][0]
    assert file_row["status"] == "R"
    assert file_row["old_path"] == "base.txt"
    assert file_row["path"] == "renamed.txt"
    assert file_row["patch"].startswith("diff --git")


def test_patch_payload_is_truncated_when_over_limit(tmp_path):
    repo = _init_repo(tmp_path)
    large = repo / "large.txt"
    large.write_text("\n".join(f"old-{idx}" for idx in range(120)) + "\n", encoding="utf-8")
    _run(["git", "add", "large.txt"], repo)
    _run(["git", "commit", "-m", "add large"], repo)

    large.write_text("\n".join(f"new-{idx}" for idx in range(120)) + "\n", encoding="utf-8")
    _run(["git", "add", "large.txt"], repo)

    payload = SEMANTIC_DIFF.collect_semantic_diff(
        repo_root=str(repo),
        selection=SEMANTIC_DIFF.DiffSelection(mode="staged"),
        max_patch_chars=260,
    )

    assert payload["status"] == "ok"
    assert payload["summary"]["truncated_patches"] == 1
    file_row = payload["files"][0]
    assert file_row["patch_truncated"] is True
    assert file_row["truncated_chars"] > 0
    assert "[truncated " in file_row["patch"]
    assert len(file_row["patch"]) <= 260


def test_output_schema_is_stable_for_agent_consumption(tmp_path):
    repo = _init_repo(tmp_path)
    tracked = repo / "base.txt"
    tracked.write_text("line-1\nline-2\n", encoding="utf-8")

    payload = SEMANTIC_DIFF.collect_semantic_diff(
        repo_root=str(repo),
        selection=SEMANTIC_DIFF.DiffSelection(mode="working_tree"),
    )

    required_top_level = {
        "schema_version",
        "status",
        "mode",
        "range",
        "repo_root",
        "generated_at",
        "limits",
        "summary",
        "files",
        "untracked_files",
        "notes",
    }
    assert required_top_level.issubset(payload.keys())
    assert payload["schema_version"] == "semantic-diff.v1"
    assert payload["mode"] == "working_tree"

    summary_keys = {
        "files_changed",
        "files_included",
        "files_omitted",
        "insertions",
        "deletions",
        "renamed_files",
        "binary_files",
        "truncated_patches",
        "status_counts",
    }
    assert summary_keys.issubset(payload["summary"].keys())

    assert len(payload["files"]) == 1
    file_keys = {
        "path",
        "status",
        "change_type",
        "raw_status",
        "old_path",
        "similarity",
        "additions",
        "deletions",
        "is_binary",
        "patch",
        "patch_truncated",
        "patch_chars",
        "truncated_chars",
    }
    assert file_keys.issubset(payload["files"][0].keys())
