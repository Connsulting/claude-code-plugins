import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = PLUGIN_ROOT / "lib" / "git_utils.py"

_spec = importlib.util.spec_from_file_location("git_utils", MODULE_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules["git_utils"] = mod
_spec.loader.exec_module(mod)


def test_resolve_repo_root_reads_worktree_gitdir_without_stderr(tmp_path, capsys):
    worktree_root = tmp_path / "worktree"
    worktree_root.mkdir()
    main_root = tmp_path / "main-repo"
    gitdir = main_root / ".git" / "worktrees" / "feature"
    (worktree_root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    repo_root = mod.resolve_repo_root(str(worktree_root))
    captured = capsys.readouterr()

    assert repo_root == str(main_root)
    assert captured.err == ""


def test_resolve_repo_root_logs_unreadable_gitdir_marker(tmp_path, monkeypatch, capsys):
    worktree_root = tmp_path / "worktree"
    worktree_root.mkdir()
    git_file = worktree_root / ".git"
    git_file.write_text("gitdir: /tmp/main-repo/.git/worktrees/feature\n", encoding="utf-8")

    original_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == git_file:
            raise OSError("permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    repo_root = mod.resolve_repo_root(str(worktree_root))
    captured = capsys.readouterr()

    assert repo_root == str(worktree_root)
    assert "[git_utils] failed to read gitdir marker" in captured.err
    assert str(git_file) in captured.err

