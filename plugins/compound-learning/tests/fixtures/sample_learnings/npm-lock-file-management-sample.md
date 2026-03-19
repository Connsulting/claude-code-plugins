# NPM Lock File Conflicts and Dependency Resolution

**Type:** gotcha
**Topic:** dependency-management
**Tags:** dependency-management, lock-files, lock-file, npm, package-lock, dependencies

## Problem

Merging two feature branches that each added a different dependency produced a massive `package-lock.json` conflict (thousands of lines). Manual resolution was impractical, and blindly accepting one side left the other branch's dependency unresolved, causing runtime `MODULE_NOT_FOUND` errors in production.

## Solution

Never manually resolve lock file conflicts. Instead, accept either side of the conflict (e.g., `git checkout --theirs package-lock.json`), then regenerate the lock file with `npm install` (or `npm ci` followed by `npm install` if the accepted side's lock is stale). Verify with `npm ls --all` that no peer dependency warnings remain. Commit the regenerated lock file as the resolution.

## Why

Lock files are generated artifacts, not hand-authored code. Attempting a line-by-line merge introduces inconsistencies between the dependency tree and the lock file's integrity hashes. Regeneration from a consistent `package.json` is the only reliable resolution strategy.
