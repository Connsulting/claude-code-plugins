# Compound Learning Plugin

A learning compounding system for Claude Code that extracts and indexes knowledge from conversations using local SQLite-vec semantic search. No Docker required.

## Privacy Policy

- Privacy policy: [PRIVACY_POLICY.md](PRIVACY_POLICY.md)
- Automated consistency checker: `python3 scripts/check-privacy-policy.py`

## Prerequisites

- Python 3.x with pip
- GitHub CLI (`gh`) - optional, required for `/pr-learnings`

Python dependencies (`pysqlite3-binary`, `sqlite-vec`, `sentence-transformers`) are auto-installed on session start via the `SessionStart` hook.
Runtime dependency declarations are tracked in `requirements-runtime.txt`.

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
pip install -r requirements-runtime.txt
```

### Post-Installation

Run `/index-learnings` to build the index. The SQLite database is created automatically and the embedding model (~80MB) downloads on first use.

## SessionStart Bootstrap

`hooks/setup.sh` is manifest-driven and cache-aware:

1. Reads runtime dependencies from `requirements-runtime.txt`
2. Builds a cache key from Python version + manifest SHA256
3. Uses a warm stamp in `~/.claude/plugins/compound-learning/cache/` to fast-path startup
4. Validates imports on cache hits and auto-invalidates stale stamps when modules are missing
5. On cache miss or stale hit, installs only missing packages and writes a fresh stamp

Force a refresh when debugging stale environments:

```bash
LEARNINGS_SETUP_FORCE_REFRESH=true bash hooks/setup.sh
```

Equivalent legacy alias: `COMPOUND_LEARNING_SETUP_FORCE_REFRESH=true`.

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `LEARNINGS_GLOBAL_DIR` | Global learnings directory | `~/.projects/learnings` |
| `LEARNINGS_REPO_SEARCH_PATH` | Base path to search for repo learnings | `~` |
| `LEARNINGS_HIGH_CONFIDENCE_THRESHOLD` | High-confidence similarity threshold (0-1, lower = more similar) | `0.4` |
| `LEARNINGS_POSSIBLY_RELEVANT_THRESHOLD` | Possibly-relevant similarity threshold (0-1) | `0.55` |
| `LEARNINGS_KEYWORD_BOOST_WEIGHT` | Hybrid rerank keyword boost weight (0-1) | `0.65` |
| `LEARNINGS_DISTANCE_THRESHOLD` | Legacy single-threshold fallback, used only when new tiered keys are not set | Legacy compatibility |
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
    "highConfidenceThreshold": 0.4,
    "possiblyRelevantThreshold": 0.55,
    "keywordBoostWeight": 0.65
  },
  "observability": {
    "enabled": false,
    "level": "info",
    "logPath": "${HOME}/.claude/plugins/compound-learning/observability.jsonl"
  }
}
```

`${HOME}` expands to your home directory.

Threshold precedence and backward compatibility:
- New tiered env vars (`LEARNINGS_HIGH_CONFIDENCE_THRESHOLD`, `LEARNINGS_POSSIBLY_RELEVANT_THRESHOLD`, `LEARNINGS_KEYWORD_BOOST_WEIGHT`) override config file values.
- New tiered config keys (`highConfidenceThreshold`, `possiblyRelevantThreshold`, `keywordBoostWeight`) override legacy `distanceThreshold`.
- Legacy `LEARNINGS_DISTANCE_THRESHOLD` overrides legacy `distanceThreshold` when tiered keys are not explicitly set.
- Invalid or out-of-range threshold values are ignored (with a warning), then the next fallback source is used.
- If thresholds are misordered (`possiblyRelevantThreshold < highConfidenceThreshold`), runtime aligns `possiblyRelevantThreshold` to `highConfidenceThreshold` and logs a warning.

### Observability Events

When observability is enabled, the plugin writes structured JSONL events for setup/auto-peek/extract hooks plus search/index/db Python flows.

Canonical event contract:
- Required: `timestamp`, `level`, `component`, `operation`, `status`
- Optional (standard): `duration_ms`, `counts`, `message`, `error`, `session_id`, `correlation_id`
- Optional (context/detail): additional emitted fields are preserved for per-event detail

Canonical status set:
- `start`
- `success`
- `error`
- `skipped`
- `empty`
- `degraded`

Operation names are normalized to snake_case. Shared aliases now collapse lifecycle/search variants at emit time:
- `hook_start` / `hook_end` -> `hook`
- `extract_start` / `extract_complete` -> `extract`
- `search_request` / `search_complete` / `search_result` -> `search`

Migration and backward compatibility:
- When an alias is applied, the original token is preserved as `operation_alias` and/or `status_alias`.
- Existing detail fields (`counts`, command metadata, scoped context) are retained unchanged.
- Hooks continue exporting correlation/session context to Python subprocesses so events remain joinable in one trace.

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
Skill(skill="search-learnings", args="JWT authentication patterns")
```

### Rebuilding the Index

```
/index-learnings
```

Also generates a manifest at `~/.projects/learnings/MANIFEST.md` summarizing learnings by topic.

### Detecting Knowledge Silos

```
/knowledge-silo-detector
```

This runs `scripts/detect-knowledge-silos.py` against indexed learnings and reports topic concentration risks.

What counts as a silo:
- A topic has at least `minTopicSamples` indexed learnings (default: `4`)
- One repo holds at least `repoDominanceThreshold` share of that topic (default: `0.70`)
  - Repo scope is used as a team/domain proxy when explicit team metadata is unavailable
- And/or one contributor holds at least `authorDominanceThreshold` share (default: `0.65`)

Machine-readable output for automation:

```bash
python3 scripts/detect-knowledge-silos.py --format json
```

Typical workflow:
1. Run `/index-learnings` to refresh the SQLite index
2. Run `/knowledge-silo-detector` to review risk-ranked findings
3. Address recommendations (cross-repo propagation, contributor backup coverage)
4. Re-run `/index-learnings` and detector to verify risk reduction

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

### Auto-Extraction via Hooks

The plugin automatically extracts learnings at key moments:

- **PreCompact**: Before context compaction to preserve insights
- **Stop**: When Claude finishes responding

How it works:
1. Hooks trigger `extract-learnings.sh` which invokes `claude -p` to analyze the transcript
2. Claude reads the conversation transcript and identifies 0-3 meaningful learnings
3. Learning files are written to the appropriate scope (global or repo)
4. Session tracking files (`~/.claude/plugins/compound-learning/sessions/*.seen`) prevent duplicate surfacing within a session window

**Debug log:** `~/.claude/plugins/compound-learning/activity.log`

**Note:** Extraction uses minimal permissions (`Write`, `Bash(mkdir:*)`) and skips trivial sessions (<20 transcript lines).

## Architecture

### Components

- **Commands:**
  - `/compound`: Extracts learnings from conversations and commits to appropriate scope
  - `/pr-learnings`: Extracts learnings from GitHub PR reviews, comments, and code changes
  - `/index-learnings`: Re-indexes all learning files into SQLite-vec
  - `/knowledge-silo-detector`: Detects topic concentration risk across repos and contributors
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
  - `PreCompact`: Auto-extracts learnings before context compaction
  - `Stop`: Auto-extracts learnings when Claude finishes responding

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
- Check threshold settings (`highConfidenceThreshold` / `possiblyRelevantThreshold`) and try increasing `possiblyRelevantThreshold` (for example to `0.7`)
- If using legacy config, check `distanceThreshold` or `LEARNINGS_DISTANCE_THRESHOLD`
- Run `/index-learnings` to ensure learnings are indexed

**Hook activity log:**
- Hook activity is logged to `~/.claude/plugins/compound-learning/activity.log`

**Stale bootstrap cache or dependency drift:**
- Force a refresh once: `LEARNINGS_SETUP_FORCE_REFRESH=true bash hooks/setup.sh`
- Or remove cache stamps manually: `rm -f ~/.claude/plugins/compound-learning/cache/setup-*.stamp`

**Observability log:**
- Structured events are logged to `~/.claude/plugins/compound-learning/observability.jsonl` (or `LEARNINGS_OBS_LOG_PATH`)
- Tail recent events with: `tail -f ~/.claude/plugins/compound-learning/observability.jsonl`
