# Compound Learning Plugin

A learning compounding system for Claude Code that extracts and indexes knowledge from conversations using ChromaDB for semantic search.

## Overview

The compound learning plugin enables Claude to learn from previous conversations by:
- Extracting key learnings from sessions via the `/compound` command
- Extracting learnings from GitHub PR reviews via the `/pr-learnings` command
- Indexing learnings into ChromaDB with semantic embeddings
- Automatically searching relevant past learnings when starting new tasks
- Supporting hierarchical scoping (global and repository-specific learnings)

## Prerequisites

- Python 3.x
- Docker (for ChromaDB container)
- Python packages: `chromadb`
- GitHub CLI (`gh`) - optional, required for `/pr-learnings` command

## Installation

### From Remote Repository

```bash
# Add the marketplace
/plugin marketplace add Connsulting/claude-code-plugins

# Install the plugin
/plugin install compound-learning@connsulting-plugins
```

### Manual Installation

1. Clone or download this plugin to your Claude plugins directory
2. Install Python dependencies:
```bash
pip install chromadb
```

### Post-Installation Setup

Start the ChromaDB database:
```
/start-learning-db
```

## Configuration

Configuration uses environment variables and an optional config file. The plugin works with sensible defaults out of the box.

### Environment Variables

Set these in your shell profile or `.claude/settings.json`:

| Variable | Description | Default |
|----------|-------------|---------|
| `CHROMADB_HOST` | ChromaDB server hostname | `localhost` |
| `CHROMADB_PORT` | ChromaDB server port | `8000` |
| `CHROMADB_DATA_DIR` | Persistent storage location | `~/.claude/chroma-data` |
| `LEARNINGS_GLOBAL_DIR` | Global learnings directory | `~/.projects/learnings` |
| `LEARNINGS_REPO_SEARCH_PATH` | Base path to search for repo learnings | `~` |
| `LEARNINGS_DISTANCE_THRESHOLD` | Similarity threshold (0-1, lower = more similar) | `0.5` |

**Example in `.claude/settings.json`:**
```json
{
  "env": {
    "LEARNINGS_GLOBAL_DIR": "/home/user/my-learnings",
    "LEARNINGS_REPO_SEARCH_PATH": "/home/user/projects"
  }
}
```

### Config File Override

For more complex configuration, create `.claude-plugin/config.json` in the plugin directory:

```json
{
  "chromadb": {
    "host": "localhost",
    "port": 8000,
    "dataDir": "${HOME}/.claude/chroma-data"
  },
  "learnings": {
    "globalDir": "${HOME}/.projects/learnings",
    "repoSearchPath": "${HOME}",
    "distanceThreshold": 0.5
  }
}
```

`${HOME}` is expanded to your home directory.

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

Extract learnings from GitHub pull request reviews and feedback:

```
/pr-learnings <PR-URL-or-number> [<PR-URL-or-number> ...]
```

**Examples:**
```bash
# Single PR by URL
/pr-learnings https://github.com/owner/repo/pull/123

# Multiple PRs
/pr-learnings 123 456 789

# Mix of URLs and numbers
/pr-learnings https://github.com/owner/repo/pull/123 456
```

This command:
1. Fetches PR data including reviews, comments, and code changes using GitHub CLI
2. Analyzes reviewer feedback for patterns and insights
3. Extracts 1-3 meaningful learnings per PR
4. Saves learnings in the same format as `/compound`

**Use cases:**
- **Individual developers**: Learn from feedback on your PRs
- **Team leads**: Extract learnings from team PRs and share across the organization
- **Code review insights**: Identify patterns in common mistakes

After running `/pr-learnings`, remember to run `/index-learnings` to make the new learnings searchable.

### Searching Learnings

Learnings are automatically searched at the start of tasks. To manually search:
```
Skill(skill="search-learnings", args="JWT authentication patterns")
```

### Rebuilding the Index

To re-index all learning files:
```
/index-learnings
```

This also generates a **manifest** at `~/.projects/learnings/MANIFEST.md` summarizing learnings by topic.

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
| other | 2 | |

## Repo: my-project (23 total)

| Topic | Count | Keywords |
|-------|-------|----------|
| api-integration | 9 | github, slack, webhook |
```

- Topics and keywords come from `**Topic:**` and `**Tags:**` fields in learning files
- `**Type:** gotcha` learnings are flagged with ⚠️

### Auto-Extraction via Hooks

The plugin automatically extracts learnings at key moments without manual intervention:

- **PreCompact**: Before context compaction (when conversation gets long), learnings are extracted to preserve insights that might otherwise be lost
- **Stop**: When Claude finishes responding, learnings are extracted from the session

**How it works:**
1. Hooks trigger `extract-learnings.sh` which invokes `claude -p` to analyze the transcript
2. Claude reads the conversation transcript and identifies 0-3 meaningful learnings
3. Learning files are written directly to the appropriate scope (global or repo)
4. A session tracking file (`~/.claude/compound-processed-sessions`) prevents duplicate extraction

**Debug log:** Check `~/.claude/compound-hook-debug.log` to see when hooks fire.

**Note:** The extraction uses minimal permissions (`Read`, `Write`, `Bash(mkdir:*)`) and skips trivial sessions (<20 transcript lines).

## Architecture

### Components

- **Commands:**
  - `/compound`: Extracts learnings from conversations and commits to appropriate scope
  - `/pr-learnings`: Extracts learnings from GitHub PR reviews, comments, and code changes
  - `/index-learnings`: Re-indexes all learning files into ChromaDB
  - `/start-learning-db`: Starts ChromaDB Docker container
  - `/consolidate-learnings`: Finds and merges duplicate or overlapping learnings

- **Agents:**
  - `learning-writer`: Analyzes conversations and extracts learnings
  - `pr-learning-extractor`: Analyzes GitHub PRs and extracts learnings from reviews
  - `consolidation-analyzer`: Analyzes learning overlap and recommends merge/keep actions

- **Skills:**
  - `search-learnings`: Queries ChromaDB for relevant learnings with hierarchical scoping
  - `start-learning-db`: Starts ChromaDB container using Docker
  - `index-learnings`: Indexes all learning markdown files into ChromaDB
  - `consolidate-discovery`: Finds consolidation candidates in ChromaDB
  - `consolidate-actions`: Executes consolidation actions (merge, archive, delete)

- **Hooks:**
  - `PreCompact`: Auto-extracts learnings before context compaction
  - `Stop`: Auto-extracts learnings when Claude finishes responding

### Learning Scopes

1. **Global** (`~/.projects/learnings/`): Security patterns, general best practices, cross-project knowledge
2. **Repo** (`[repo]/.projects/learnings/`): Repository-specific gotchas, patterns, architecture decisions

The search skill automatically detects which repository you're working in and includes both global learnings and learnings from parent directories in the hierarchy.

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

**ChromaDB won't start:**
- Ensure Docker is running
- Check if port 8000 is already in use
- View logs: `docker logs claude-learning-db`

**No learnings found:**
- Verify learning files exist in configured paths
- Run `/index-learnings` to re-index
- Check config paths in `.claude-plugin/config.json`

**Search returns no results:**
- Check `distanceThreshold` setting (try increasing to 0.7)
- Verify ChromaDB is running
- Ensure learnings have been indexed

**Hook activity log:**
- Hook activity is logged to `~/.claude/plugins/compound-learning/activity.log`
- Use this to verify hooks are running and debug extraction issues

## Development

The plugin uses upsert operations for indexing, making it safe to re-run indexing without creating duplicates. ChromaDB data persists in the configured `dataDir` across container restarts.
