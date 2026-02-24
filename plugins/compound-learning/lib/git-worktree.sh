#!/bin/bash

# Shared git worktree detection utilities for compound-learning hooks.
# Source this file from hook scripts:
#   source "${CLAUDE_PLUGIN_ROOT}/lib/git-worktree.sh"

# resolve_repo_root <dir>
#   Returns the real repository root, resolving worktrees back to the main repo.
#   If <dir> is inside a worktree, follows the .git file's gitdir pointer to
#   find the main repo. If <dir> is a normal repo, returns that repo root.
#   If not inside any git repo, returns <dir> unchanged.
resolve_repo_root() {
  local dir="${1:-.}"
  dir="$(cd "$dir" 2>/dev/null && pwd)" || return 1

  local check="$dir"
  while [ "$check" != "/" ]; do
    if [ -e "$check/.git" ]; then
      if [ -f "$check/.git" ]; then
        # Worktree: .git is a file containing "gitdir: /path/to/main/.git/worktrees/name"
        local gitdir_line
        gitdir_line=$(head -1 "$check/.git" 2>/dev/null)
        local gitdir_path="${gitdir_line#gitdir: }"
        # Strip trailing whitespace/newline
        gitdir_path="${gitdir_path%"${gitdir_path##*[![:space:]]}"}"

        if [ -z "$gitdir_path" ] || [ "$gitdir_path" = "$gitdir_line" ]; then
          # .git file does not contain a valid gitdir line; treat as normal repo
          echo "$check"
          return 0
        fi

        # Extract main repo root from gitdir path
        # gitdir looks like: /path/to/main-repo/.git/worktrees/worktree-name
        local main_root="${gitdir_path%%/.git/worktrees/*}"
        if [ "$main_root" != "$gitdir_path" ]; then
          echo "$main_root"
        else
          # gitdir path does not contain /.git/worktrees/; fall back to repo root
          echo "$check"
        fi
        return 0
      elif [ -d "$check/.git" ]; then
        # Normal repo: .git is a directory
        echo "$check"
        return 0
      fi
    fi
    check="$(dirname "$check")"
  done

  # Not inside any git repo; return original dir
  echo "$dir"
}

# resolve_repo_name <dir>
#   Returns the basename of the resolved repo root.
resolve_repo_name() {
  local root
  root="$(resolve_repo_root "$1")"
  basename "$root"
}

# is_git_worktree <dir>
#   Returns 0 (true) if <dir> is inside a git worktree, 1 otherwise.
is_git_worktree() {
  local dir="${1:-.}"
  dir="$(cd "$dir" 2>/dev/null && pwd)" || return 1

  local check="$dir"
  while [ "$check" != "/" ]; do
    if [ -f "$check/.git" ]; then
      local gitdir_line
      gitdir_line=$(head -1 "$check/.git" 2>/dev/null)
      local gitdir_path="${gitdir_line#gitdir: }"
      gitdir_path="${gitdir_path%"${gitdir_path##*[![:space:]]}"}"
      if [ -n "$gitdir_path" ] && [ "$gitdir_path" != "$gitdir_line" ]; then
        return 0
      fi
      return 1
    elif [ -d "$check/.git" ]; then
      return 1
    fi
    check="$(dirname "$check")"
  done

  return 1
}
