# Compound Learning Plugin

A learning compounding system for Claude Code that extracts and indexes knowledge from conversations using local SQLite-vec semantic search. No Docker required.

## Prerequisites

- Python 3.x with pip
- GitHub CLI (`gh`) - optional, required for `/pr-learnings`

Python dependencies (`pysqlite3-binary`, `sqlite-vec`, `sentence-transformers`) are auto-installed on session start via the `SessionStart` hook.

## Installation

### From Remote Repository

```bash
/plugin marketplace add Connsulting/claude-code-plugins
/plugin install compound-learning@connsulting-plugins
```

### Manual Installation

1. Clone or download this plugin to your Claude plugins directory
2. Python dependencies install automatically on first session start, or install manually:
```bash
pip install pysqlite3-binary sqlite-vec sentence-transformers
```

### Post-Installation

Run `/index-learnings` to build the index. The SQLite database is created automatically and the embedding model (~80MB) downloads on first use.

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `LEARNINGS_GLOBAL_DIR` | Global learnings directory | `~/.projects/learnings` |
| `LEARNINGS_REPO_SEARCH_PATH` | Base path to search for repo learnings | `~` |
| `LEARNINGS_DISTANCE_THRESHOLD` | Similarity threshold (0-1, lower = more similar) | `0.5` |
| `LEARNINGS_OBS_ENABLED` | Enable structured observability events (`true`/`false`) | `false` |
| `LEARNINGS_OBS_LEVEL` | Minimum observability level (`debug`, `info`, `warn`, `error`) | `info` |
| `LEARNINGS_OBS_LOG_PATH` | JSONL observability log path | `~/.claude/plugins/compound-learning/observability.jsonl` |
| `LEARNINGS_OBS_CORRELATION_ID` | Correlation ID for linking hook/search/index/DB events | Auto-generated per hook run |
| `LEARNINGS_OBS_SESSION_ID` | Session identifier override used in observability context | Auto-detected from Claude/session input |

**Example in `.claude/settings.json`:**
```json
{
  "env": {
    "LEARNINGS_GLOBAL_DIR": "/home/user/my-learnings",
    "LEARNINGS_REPO_SEARCH_PATH": "/home/user/projects"
  }
}
```

### Config File

Create `.claude-plugin/config.json` in the plugin directory:

```json
{
  "sqlite": {
    "dbPath": "${HOME}/.claude/compound-learning.db"
  },
  "learnings": {
    "globalDir": "${HOME}/.projects/learnings",
    "repoSearchPath": "${HOME}",
    "distanceThreshold": 0.5
  },
  "observability": {
    "enabled": false,
    "level": "info",
    "logPath": "${HOME}/.claude/plugins/compound-learning/observability.jsonl"
  }
}
```

`${HOME}` expands to your home directory.

### Observability Events

When observability is enabled, the plugin writes structured JSONL events for:
- Hooks (`setup`, `auto-peek`, `extract-learnings`) including start/end/skip/subprocess exit/duration
- Search pipeline (keyword parse, repo scope, fan-out, merge/rerank/filter, threshold buckets, final status)
- Index pipeline (discovery, per-file failures, prune summary, manifest generation, total runtime)
- DB operations (connection open, schema init, embeddings, upsert/delete/vector search)

Event fields include: `timestamp`, `level`, `component`, `operation`, `status`, `duration_ms`, `counts`, optional `error`, and correlation/session identifiers when available.
Hooks now export correlation/session context into Python subprocesses so hook events can be joined to search/index/db events in one trace.

## Usage

### Creating Learnings

After a productive session, use the `/compound` command:
```
/compound
Went well: [successes]
Went poorly: [failures]
Recommendations: [improvements]
```

This extracts learnings and commits them to the appropriate scope:
- **Global learnings** (`~/.projects/learnings/`): Applicable across all projects
- **Repo learnings** (`[repo]/.projects/learnings/`): Specific to a repository

### Extracting Learnings from PRs

```
/pr-learnings <PR-URL-or-number> [<PR-URL-or-number> ...]
```

**Examples:**
```bash
/pr-learnings https://github.com/owner/repo/pull/123
/pr-learnings 123 456 789
```

Fetches PR data including reviews, comments, and code changes, then extracts 1-3 meaningful learnings per PR. Run `/index-learnings` afterward to make new learnings searchable.

### Searching Learnings

Learnings are automatically searched at the start of tasks. To manually search:
```
Skill(skill="compound-learning:search-learnings", args="JWT authentication patterns")
```

For local debugging/maintenance, you can run the script directly:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/search-learnings.py" "JWT authentication patterns" "$(pwd)"
```

`search-learnings.py` options:
- `--peek`: return a single merged list (high-confidence first, then fallback results)
- `--exclude-ids "<id1,id2>"`: skip specific learning IDs
- `--threshold <float>`: override high-confidence threshold for this run
- `--max-results <int>`: cap returned results (default `5`)
- `--keywords-json '["keyword1","keyword2"]'`: fan out parallel keyword searches

### Maintenance Scripts

Use `scripts/backfill-topics.py` to add missing `**Topic:**` lines to learning files using existing `**Tags:**`.

Default behavior is dry-run against `~/.projects/learnings`:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/backfill-topics.py"
```

Explicit dry-run for a custom directory:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/backfill-topics.py" --dry-run --dir ~/.projects/learnings
```

Apply changes in-place:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/backfill-topics.py" --apply --dir ~/.projects/learnings
```

Notes:
- The scan includes `*.md` files in the target directory and skips `MANIFEST.md`
- `--dry-run` is the default; use `--apply` to write changes

### Rebuilding the Index

```
/index-learnings
```

Also generates a manifest at `~/.projects/learnings/MANIFEST.md` summarizing learnings by topic.

### Test Gap Finder

Use the deterministic test-gap report to prioritize under-tested Python modules/functions in:
`lib/`, `scripts/`, `hooks/`, and `skills/`.

Generate a baseline report without line coverage:

```bash
python3 plugins/compound-learning/scripts/test-gap-finder.py \
  --plugin-root plugins/compound-learning \
  --output plugins/compound-learning/reports/test-gap-report.baseline.json
```

Generate `coverage.xml` and include line-coverage evidence:

```bash
cd plugins/compound-learning
coverage run --rcfile .coveragerc -m pytest tests/test_test_gap_finder.py tests/test_observability.py
coverage xml -o coverage.xml
python3 scripts/test-gap-finder.py --coverage-xml coverage.xml --output reports/test-gap-report.coverage.json
```

Report contract highlights:
- `summary`: module/test counts, direct-test counts, and overall coverage summary when available
- `prioritized_gaps.modules`: ranked module gaps with `coverage_pct`, `tested_by`, `missing_symbols`, and `suggested_next_tests`
- `prioritized_gaps.functions`: ranked callable gaps with line-range evidence and suggested next tests

Current limitations:
- Static heuristics for imports/symbol references can miss dynamic execution paths
- Branch coverage is not scored
- Shell/markdown assets are intentionally excluded from gap scoring

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

### Hook Lifecycle

Configured hooks and script mappings:

| Hook event | Script | Purpose |
|------------|--------|---------|
| `SessionStart` | `hooks/setup.sh` | Verifies/installs Python dependencies used by the plugin |
| `UserPromptSubmit` | `hooks/auto-peek.sh` | Extracts keywords and injects relevant learnings before Claude responds |
| `PreCompact` | `hooks/extract-learnings.sh` | Asynchronously extracts learnings before context compaction |
| `SessionEnd` | `hooks/extract-learnings.sh` | Asynchronously performs a final extraction at session end |

Extraction flow for `PreCompact` and `SessionEnd`:
1. `extract-learnings.sh` reads hook input (`transcript_path`, `cwd`, session context)
2. It skips trivial transcripts (<20 lines), then invokes `claude -p` to write 0-3 learning files
3. Newly created files are indexed immediately via `skills/index-learnings/index-learnings.py --file`

Operational files:
- Hook activity log: `~/.claude/plugins/compound-learning/activity.log`
- Hook observability log: `~/.claude/plugins/compound-learning/observability.jsonl`
- Auto-peek seen-ID cache: `~/.claude/plugins/compound-learning/sessions/*.seen`

**Note:** Extraction uses minimal permissions (`Write`, `Bash(mkdir:*)`) and runs asynchronously for `PreCompact` and `SessionEnd`.

## Architecture

### Components

- **Commands:**
  - `/compound`: Extracts learnings from conversations and commits to appropriate scope
  - `/pr-learnings`: Extracts learnings from GitHub PR reviews, comments, and code changes
  - `/index-learnings`: Re-indexes all learning files into SQLite-vec
  - `/consolidate-learnings`: Finds and merges duplicate or overlapping learnings

- **Agents:**
  - `learning-writer`: Analyzes conversations and extracts learnings
  - `pr-learning-extractor`: Analyzes GitHub PRs and extracts learnings from reviews

- **Skills:**
  - `search-learnings`: Queries SQLite-vec for relevant learnings with hierarchical scoping
  - `index-learnings`: Indexes all learning markdown files into SQLite-vec
  - `consolidate-discovery`: Finds consolidation candidates
  - `consolidate-actions`: Executes consolidation actions (merge, archive, delete)

- **Hooks:**
  - `SessionStart` -> `hooks/setup.sh`: Auto-installs/checks Python dependencies
  - `UserPromptSubmit` -> `hooks/auto-peek.sh`: Auto-searches and injects relevant learnings
  - `PreCompact` -> `hooks/extract-learnings.sh`: Auto-extracts learnings before compaction
  - `SessionEnd` -> `hooks/extract-learnings.sh`: Auto-extracts learnings at session end

### Learning Scopes

1. **Global** (`~/.projects/learnings/`): Security patterns, general best practices, cross-project knowledge
2. **Repo** (`[repo]/.projects/learnings/`): Repository-specific gotchas, patterns, architecture decisions

The search skill automatically detects which repository you're working in and includes both global and repo-scoped learnings.

## Recommended CLAUDE.md Configuration

Add this to your global `~/.claude/CLAUDE.md`:

```markdown
## Learning Compounding

@~/.projects/learnings/MANIFEST.md

**When to search:** If manifest shows a topic matching your task, search for it.
**When to skip:** If no relevant topic in manifest, don't search.

**Search:** `Skill(skill="compound-learning:search-learnings", args="[topic] [context]")`
**Peek:** Add `--peek --exclude-ids [seen-ids]` when shifting to a new manifest topic mid-conversation.

Use topic + context: "authentication JWT refresh" not "implement login feature"
```

## Troubleshooting

**No learnings found:**
- Verify learning files exist in configured paths
- Run `/index-learnings` to re-index
- Check config paths in `.claude-plugin/config.json`

**Search returns no results:**
- Check `distanceThreshold` setting (try increasing to 0.7)
- Run `/index-learnings` to ensure learnings are indexed

**Hook activity log:**
- Hook activity is logged to `~/.claude/plugins/compound-learning/activity.log`

**Observability log:**
- Structured events are logged to `~/.claude/plugins/compound-learning/observability.jsonl` (or `LEARNINGS_OBS_LOG_PATH`)
- Tail recent events with: `tail -f ~/.claude/plugins/compound-learning/observability.jsonl`
