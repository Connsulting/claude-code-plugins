# Git Rebase Conflict Resolution Strategy

**Type:** pattern
**Topic:** git-workflow
**Tags:** git, rebase, conflicts, branch-management, merge, workflow

## Problem

Long-lived feature branches accumulated dozens of commits and rebasing onto main produced cascading merge conflicts. Developers resolved the same conflict repeatedly across sequential commits, wasting hours and sometimes introducing subtle regressions by mis-resolving a conflict in one commit that only became visible several commits later.

## Solution

Use `git rerere` (reuse recorded resolution) to cache conflict resolutions so they apply automatically when the same conflict recurs. For branches with many small commits, squash related changes into logical units before rebasing to reduce the number of conflict points. When a rebase still produces more than three conflict rounds, abort, merge main into the branch once to capture resolutions, then use `git rebase --onto` to replay the cleaned-up history.

## Why

Repeated manual conflict resolution is error-prone and discourages frequent integration. `rerere` eliminates the repetition, and pre-squashing reduces the surface area for conflicts. The merge-then-rebase-onto escape hatch preserves a clean history without forcing developers to suffer through an unmanageable rebase.
