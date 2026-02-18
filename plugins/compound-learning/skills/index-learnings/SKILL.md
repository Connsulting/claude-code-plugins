---
name: index-learnings
description: Index learning files into SQLite and generate topic manifest
---

# Index Learnings

Indexes all learning markdown files into SQLite and generates a manifest summarizing topics.

## Usage

```
/index-learnings
```

## What It Does

1. Discovers `.md` files in `~/.projects/learnings/` (global) and `[repo]/.projects/learnings/` (repo)
2. Extracts `**Topic:**` and `**Tags:**` from each file
3. Indexes into SQLite
4. Generates `~/.projects/learnings/MANIFEST.md`

## Manifest Format

```markdown
# Learnings Manifest
Generated: 2026-02-03T14:30:00Z

## Global Learnings (47 total, 3 gotchas)

| Topic | Count | Keywords |
|-------|-------|----------|
| authentication | 12 (2⚠️) | jwt, oauth, refresh, session |
| error-handling | 8 | retry, timeout, fallback |
| testing | 7 | mock, fixture, integration |
| other | 2 | |

## Repo: my-project (23 total)

| Topic | Count | Keywords |
|-------|-------|----------|
| api-integration | 9 | github, slack, webhook |
| other | 2 | |
```

- Topics and keywords come from explicit `**Topic:**` and `**Tags:**` fields (written by Claude)
- `**Type:** gotcha` learnings are flagged with ⚠️
- Falls back to "other" if no Topic specified

## Learning File Format

```markdown
# Title

**Type:** pattern | gotcha | security
**Topic:** authentication
**Tags:** jwt, oauth, refresh tokens

## Problem
...

## Solution
...
```

## Notes

- SQLite database auto-initialized on first run
- Safe to run multiple times (upsert)
- Skips MANIFEST.md files
