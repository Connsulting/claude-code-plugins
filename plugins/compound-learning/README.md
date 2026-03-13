# Compound Learning Plugin

A learning compounding system for Claude Code that extracts reusable knowledge from conversations and PR feedback, stores it locally in SQLite plus `sqlite-vec`, and surfaces relevant learnings automatically through hooks. No Docker required.

## What It Includes

- Slash commands:
  - `/compound` to write learnings from the current conversation
  - `/pr-learnings` to extract learnings from GitHub PR feedback
  - `/index-learnings` to rebuild the SQLite index and manifest
  - `/consolidate-learnings` to find and clean up duplicate or outdated learnings
- Hooks:
  - `SessionStart` bootstraps Python dependencies
  - `UserPromptSubmit` auto-peeks for relevant learnings
  - `PreCompact` and `SessionEnd` extract new learnings asynchronously
- Shared helpers for SQLite access, worktree-aware repo detection, and topic inference

## Requirements

- `python3` and `pip`
- `jq`
- `timeout` on `PATH`
- `git`
- `gh` on `PATH` if you want to use `/pr-learnings`

Python dependencies (`pysqlite3-binary`, `sqlite-vec`, `sentence-transformers`) are auto-installed on `SessionStart` by [hooks/setup.sh](hooks/setup.sh). Shell tooling such as `jq` and `timeout` is not auto-installed.

## Installation

### From Remote Repository

```bash
/plugin marketplace add Connsulting/claude-code-plugins
/plugin install compound-learning@connsulting-plugins
```

### Manual Installation

1. Clone or copy this plugin into your Claude plugins directory.
2. Start a Claude session once so the `SessionStart` hook can bootstrap Python dependencies.
3. If you prefer to install them yourself, run:

```bash
pip install pysqlite3-binary sqlite-vec sentence-transformers
```

### Post-Installation

Run `/index-learnings` after you have at least one learning file. The SQLite database is created automatically, and the embedding model downloads on first use.

## Configuration

Configuration loading order is:

1. Environment variables
2. [.claude-plugin/config.json](.claude-plugin/config.json)
3. Built-in defaults from [lib/db.py](lib/db.py)

### Environment Overrides

| Variable | Description | Default |
|----------|-------------|---------|
| `SQLITE_DB_PATH` | SQLite database path | `~/.claude/compound-learning.db` |
| `LEARNINGS_GLOBAL_DIR` | Global learnings directory | `~/.projects/learnings` |
| `LEARNINGS_REPO_SEARCH_PATH` | Base path to search for repo learnings | `~` |

**Example in `.claude/settings.json`:**
```json
{
  "env": {
    "SQLITE_DB_PATH": "/home/user/.claude/compound-learning.db",
    "LEARNINGS_GLOBAL_DIR": "/home/user/my-learnings",
    "LEARNINGS_REPO_SEARCH_PATH": "/home/user/projects"
  }
}
```

The older `LEARNINGS_DISTANCE_THRESHOLD` env var and `distanceThreshold` config key are stale on the current `main` branch and are not read by the search code.

### Config Keys

| Key | Used by | Default |
|-----|---------|---------|
| `sqlite.dbPath` | All SQLite readers and writers | `${HOME}/.claude/compound-learning.db` |
| `learnings.globalDir` | Global learning storage and manifest generation | `${HOME}/.projects/learnings` |
| `learnings.repoSearchPath` | Repo discovery for indexing and scoped search | `${HOME}` |
| `learnings.archiveDir` | Consolidation backups and archive moves | `${HOME}/.projects/archive/learnings` |
| `learnings.highConfidenceThreshold` | High-confidence search cutoff | `0.40` |
| `learnings.possiblyRelevantThreshold` | Secondary search cutoff used for peek backfill | `0.55` |
| `learnings.keywordBoostWeight` | Hybrid reranking weight for keyword overlap | `0.65` |
| `consolidation.duplicateThreshold` | Duplicate detection cutoff | `0.25` |
| `consolidation.scopeKeywords` | Keywords used when deciding scope-sensitive merges | Built-in list in [lib/db.py](lib/db.py) |
| `consolidation.outdatedKeywords` | Markers used to flag stale learnings | Built-in list in [lib/db.py](lib/db.py) |

`${HOME}` expands to the user's home directory inside [.claude-plugin/config.json](.claude-plugin/config.json).

## Usage

### Create Learnings

After a productive session, use the `/compound` command:
```
/compound
Went well: [successes]
Went poorly: [failures]
Recommendations: [improvements]
```

The learning writer stores files in one of two scopes:

- Global learnings in `~/.projects/learnings/`
- Repo-specific learnings in `[repo]/.projects/learnings/`

### Extract Learnings From PRs

```
/pr-learnings <PR-URL-or-number> [<PR-URL-or-number> ...]
```

This command shells out to `gh`, fetches review threads plus code changes, and writes 0-3 learning files per PR. Run `/index-learnings` afterward to rebuild the index and manifest.

### Rebuild The Index

```
/index-learnings
```

The indexer scans global and repo learnings, upserts them into SQLite, prunes orphaned rows whose files were removed on disk, and regenerates `~/.projects/learnings/MANIFEST.md`.

### Manual Search And Validation

There is no packaged `search-learnings` skill on the current `main` branch. Auto-peek and manual validation both use [scripts/search-learnings.py](scripts/search-learnings.py):

```bash
PLUGIN_DIR=/path/to/compound-learning
CLAUDE_PLUGIN_ROOT="$PLUGIN_DIR" \
python3 "$PLUGIN_DIR/scripts/search-learnings.py" "jwt refresh token" "$PWD"
```

Useful flags:

- `--peek` to return the compact auto-peek output shape
- `--exclude-ids` to hide session-local duplicates
- `--threshold` to override only the high-confidence cutoff
- `--max-results` to bound output size
- `--keywords-json` to search multiple extracted keywords in parallel

### Learnings Manifest

The manifest helps Claude decide when to search by showing what topics have learnings:

```markdown
# Learnings Manifest
Generated: 2026-02-03T14:30:00Z

## Global Learnings (47 total, 3 gotchas)

| Topic | Count | Keywords |
|-------|-------|----------|
| authentication | 12 (2⚠️) | jwt, oauth, refresh, session |
| error-handling | 8 | retry, timeout, fallback |

## Repo: my-project (23 total)

| Topic | Count | Keywords |
|-------|-------|----------|
| api-integration | 9 | github, slack, webhook |
```

Topics come from `**Topic:**` and `**Tags:**` fields in learning files. `**Type:** gotcha` learnings are flagged with ⚠️.

## Hook Lifecycle

| Event | Entrypoint | Async | Purpose |
|-------|------------|-------|---------|
| `SessionStart` | [hooks/setup.sh](hooks/setup.sh) | No | Import-checks and installs Python dependencies if needed |
| `UserPromptSubmit` | [hooks/auto-peek.sh](hooks/auto-peek.sh) | No | Extracts 1-2 keywords and injects relevant learnings into context |
| `PreCompact` | [hooks/extract-learnings.sh](hooks/extract-learnings.sh) | Yes | Extracts learnings before compaction drops context |
| `SessionEnd` | [hooks/extract-learnings.sh](hooks/extract-learnings.sh) | Yes | Extracts learnings again at session end to catch late insights |

Important behavior:

- Auto-peek reads `prompt`, `transcript_path`, and `cwd` from hook stdin, calls `claude -p --model haiku` to extract keywords, and then calls [scripts/search-learnings.py](scripts/search-learnings.py) with `--peek`.
- Auto-peek records seen result IDs in `~/.claude/plugins/compound-learning/sessions/*.seen` and prunes files older than 24 hours.
- Extraction reads `session_id`, `transcript_path`, and `cwd`, skips transcripts shorter than 20 lines, and resolves worktree paths back to the main repo root before writing repo-scoped learnings.
- Newly created learning files are indexed immediately by calling [skills/index-learnings/index-learnings.py](skills/index-learnings/index-learnings.py) with `--file`.
- Both hook scripts that invoke `claude -p` set `CLAUDE_SUBPROCESS=1` and exit early if that variable is already present, which prevents hook recursion.

## Runtime Files And State

| Path | Purpose |
|------|---------|
| `~/.claude/compound-learning.db` | SQLite database containing `learnings`, `vec_learnings`, and `fts_learnings` |
| `~/.claude/plugins/compound-learning/activity.log` | Shared hook activity log |
| `~/.claude/plugins/compound-learning/sessions/*.seen` | Auto-peek dedupe state per transcript/session |
| `~/.projects/learnings/` | Global learning files |
| `[repo]/.projects/learnings/` | Repo-scoped learning files |
| `~/.projects/archive/learnings/YYYY-MM-DD/` | Consolidation backups and archives |
| `~/.projects/learnings/MANIFEST.md` | Topic summary generated by the indexer |
| `~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2` | Default embedding model cache |

## Repository Layout

- `commands/*.md` contains the slash-command orchestration docs.
- `agents/*.md` contains the writing and PR extraction agent prompts.
- [hooks/README.md](hooks/README.md) documents hook triggers, stdin payloads, recursion guards, and troubleshooting.
- [lib/README.md](lib/README.md) documents the shared Python and shell helper modules.
- [scripts/README.md](scripts/README.md) documents the operator-facing search and topic-backfill scripts.
- [skills/README.md](skills/README.md) indexes the packaged skills used for indexing and consolidation.
- [tests/test_search_relevance.py](tests/test_search_relevance.py) contains the current SQLite search tests.

On the current `main` branch, dependency bootstrap still lives in [hooks/setup.sh](hooks/setup.sh); there is no shared `lib/bootstrap.py` module in this plugin yet.

## Recommended CLAUDE.md Configuration

Add this to your global `~/.claude/CLAUDE.md`:

```markdown
## Learning Compounding

@~/.projects/learnings/MANIFEST.md

**When to search:** If manifest shows a topic matching your task, search for it.
**When to skip:** If no relevant topic in manifest, don't search.

**Search:** `python3 [plugin-path]/scripts/search-learnings.py "[topic] [context]" "$PWD"`
**Peek:** Add `--peek --exclude-ids [seen-ids]` when shifting to a new manifest topic mid-conversation.

Use topic + context: "authentication JWT refresh" not "implement login feature"
```

## Troubleshooting

- If hooks appear silent, inspect `~/.claude/plugins/compound-learning/activity.log` first.
- If search returns nothing, run `/index-learnings`, confirm your files are under the configured learning directories, and then check `highConfidenceThreshold` / `possiblyRelevantThreshold` rather than the stale `distanceThreshold` key.
- If `UserPromptSubmit` auto-peek fails immediately, verify that `jq`, `timeout`, `claude`, and `python3` are on `PATH`.
- If `/pr-learnings` fails, confirm `gh --version` and `gh auth status`.
