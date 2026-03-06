---
description: Re-index all learning markdown files into SQLite
---

# Index Learnings Command

Re-index all learning markdown files into SQLite/sqlite-vec and regenerate the global learnings manifest.

## Usage

```bash
/index-learnings
```

When executing this command, run:

```python
Skill(skill="compound-learning:index-learnings")
```

Then report the output back to the user.

## Purpose

Use `/index-learnings` when:
- New learning files were created (for example after `/compound` or `/pr-learnings`)
- Existing learning files were edited, renamed, or removed
- Search results look stale or incomplete

## Prerequisites

- Python dependencies available: `pysqlite3-binary`, `sqlite-vec`, `sentence-transformers`
  - Normally installed by `hooks/setup.sh` on `SessionStart`
- Learning files exist in one or both scopes:
  - Global: `~/.projects/learnings/`
  - Repo: `[repo]/.projects/learnings/`
- Optional config overrides are set if needed:
  - Env vars: `SQLITE_DB_PATH`, `LEARNINGS_GLOBAL_DIR`, `LEARNINGS_REPO_SEARCH_PATH`
  - Config file: `plugins/compound-learning/.claude-plugin/config.json`

## What The Skill Does

1. Loads config and opens the SQLite database (creates schema if missing)
2. Discovers learning markdown files in global and repo scopes
3. Skips `MANIFEST.md` and upserts each learning into SQLite/sqlite-vec
4. Prunes orphaned DB entries whose source files no longer exist
5. Writes `~/.projects/learnings/MANIFEST.md` with topic summaries

## Expected Outputs

- SQLite index at `~/.claude/compound-learning.db` by default (or configured override)
- Manifest at `~/.projects/learnings/MANIFEST.md`
- Console output similar to:
  - `[OK] Database ready`
  - `[OK] Indexed N files`
  - `[OK] Generated manifest: ~/.projects/learnings/MANIFEST.md`

## Troubleshooting

**No learning files found**
- Verify learnings exist in configured directories
- Run `/compound` to generate learnings, then run `/index-learnings` again

**Database open failure**
- Ensure dependencies are installed (`pip install pysqlite3-binary sqlite-vec sentence-transformers`)
- Confirm DB path is writable (`SQLITE_DB_PATH` or config `sqlite.dbPath`)

**Manifest missing or stale**
- Re-run `/index-learnings` after adding/editing learnings
- Verify `LEARNINGS_GLOBAL_DIR` points to the expected global learnings directory

**Search still misses expected results**
- Confirm the target files were indexed (check command output for per-file errors)
- Re-run `/index-learnings` to refresh embeddings and prune stale rows
