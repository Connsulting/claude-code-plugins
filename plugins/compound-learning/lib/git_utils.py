"""
Git worktree detection utilities for compound-learning plugin.

Provides functions to resolve worktree paths back to the main repository root.
In a worktree, .git is a file containing "gitdir: /path/to/main/.git/worktrees/name".
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_repo_root(cwd: str) -> str:
    """Return the real repository root, resolving worktrees to the main repo.

    Walks up from *cwd* looking for a .git entry.  If .git is a file (worktree
    marker), reads the gitdir pointer and derives the main repo root.  If .git
    is a directory (normal repo), returns that directory.  If no git repo is
    found, returns *cwd* unchanged.
    """
    check = Path(cwd).resolve()

    while True:
        git_path = check / ".git"

        if git_path.is_file():
            gitdir = _read_gitdir(git_path)
            if gitdir is not None:
                # gitdir looks like /path/to/main-repo/.git/worktrees/name
                marker = os.sep + os.path.join(".git", "worktrees") + os.sep
                idx = gitdir.find(marker)
                if idx != -1:
                    return gitdir[:idx]
            # .git file without valid gitdir; treat directory as repo root
            return str(check)

        if git_path.is_dir():
            return str(check)

        parent = check.parent
        if parent == check:
            break
        check = parent

    return cwd


def resolve_repo_name(cwd: str) -> str:
    """Return the basename of the resolved repo root."""
    return os.path.basename(resolve_repo_root(cwd))


def is_worktree(path: str) -> bool:
    """Return True if *path* is inside a git worktree (not a normal repo)."""
    check = Path(path).resolve()

    while True:
        git_path = check / ".git"

        if git_path.is_file():
            gitdir = _read_gitdir(git_path)
            return gitdir is not None

        if git_path.is_dir():
            return False

        parent = check.parent
        if parent == check:
            break
        check = parent

    return False


def _read_gitdir(git_file: Path) -> str | None:
    """Read a .git worktree file and return the gitdir path, or None."""
    try:
        content = git_file.read_text().strip()
    except OSError:
        return None

    if content.startswith("gitdir: "):
        return content[len("gitdir: "):]

    return None
