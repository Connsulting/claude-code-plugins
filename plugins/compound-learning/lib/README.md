# Library Modules

This directory contains the shared Python and shell helpers used by the plugin runtime. Python entrypoints import `lib.*`, while shell hooks source [git-worktree.sh](git-worktree.sh).

## Module Ownership

| Module | Used by | Responsibility |
|--------|---------|----------------|
| [db.py](db.py) | `scripts/`, `skills/`, tests | Config loading, SQLite connection setup, sqlite-vec loading, embeddings, schema creation, CRUD helpers, and scoped search |
| [git_utils.py](git_utils.py) | Python scripts and skills | Resolve worktree paths and repo names in Python |
| [git-worktree.sh](git-worktree.sh) | Hook shell scripts | Resolve worktree paths and repo names in shell |
| [topic_mapping.py](topic_mapping.py) | Indexer and topic backfill script | Infer `**Topic:**` values from `**Tags:**` |
| [__init__.py](__init__.py) | Python import machinery | Marks `lib` as an importable package |

There is no shared `bootstrap.py` module on the current `main` branch. Dependency bootstrap is still implemented directly in [../hooks/setup.sh](../hooks/setup.sh).

## `db.py`

`db.py` is the central runtime module.

- Config precedence: environment variables -> [.claude-plugin/config.json](../.claude-plugin/config.json) -> built-in defaults
- Environment overrides:
  - `SQLITE_DB_PATH`
  - `LEARNINGS_GLOBAL_DIR`
  - `LEARNINGS_REPO_SEARCH_PATH`
- Managed defaults:
  - DB path: `~/.claude/compound-learning.db`
  - Global learnings: `~/.projects/learnings`
  - Archive: `~/.projects/archive/learnings`
  - High-confidence threshold: `0.40`
  - Possibly-relevant threshold: `0.55`
  - Keyword boost weight: `0.65`
  - Duplicate threshold: `0.25`
- SQLite schema:
  - `learnings`
  - `vec_learnings`
  - `fts_learnings`
- Embeddings:
  - Model: `sentence-transformers/all-MiniLM-L6-v2`
  - Cache path: `~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2`

Search behavior is intentionally hybrid:

- Vector candidates come from `sqlite-vec`
- FTS5 adds a small boost for exact keyword matches
- Keyword overlap reduces adjusted distance further
- Scope filtering always includes global learnings plus any detected repo scopes

## Worktree Helpers

The plugin keeps separate implementations for shell and Python:

- [git_utils.py](git_utils.py) is imported by Python code such as [../scripts/search-learnings.py](../scripts/search-learnings.py) and [../skills/index-learnings/index-learnings.py](../skills/index-learnings/index-learnings.py).
- [git-worktree.sh](git-worktree.sh) is sourced by [../hooks/extract-learnings.sh](../hooks/extract-learnings.sh).

Both implementations do the same thing:

- Walk upward until they find `.git`
- Treat `.git` files as worktree markers
- Convert `/path/to/repo/.git/worktrees/<name>` back to the main repository root
- Fall back to the original directory when no repo is detected

## Topic Mapping

[topic_mapping.py](topic_mapping.py) provides a single `TOPIC_TAG_MAP` and `infer_topic_from_tags()` helper so the indexer and maintenance scripts infer topics consistently.

Current callers:

- [../skills/index-learnings/index-learnings.py](../skills/index-learnings/index-learnings.py)
- [../scripts/backfill-topics.py](../scripts/backfill-topics.py)

## Runtime Files And Directories

The modules in this directory create or rely on these paths:

| Path | Owner |
|------|-------|
| `~/.claude/compound-learning.db` | `db.get_connection()` |
| `~/.claude/` | created implicitly when the DB directory is created |
| `~/.cache/huggingface/...all-MiniLM-L6-v2` | `db.get_embedding()` |
| `~/.projects/archive/learnings/` | configured in `db.load_config()`, used by consolidation actions |

If you change config keys or defaults here, update [../README.md](../README.md) and [.claude-plugin/config.json](../.claude-plugin/config.json) in the same change.
