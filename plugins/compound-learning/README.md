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

This also generates **manifest files** summarizing learnings by topic. To regenerate manifests without re-indexing:
```bash
python3 ~/.claude/plugins/compound-learning/skills/index-learnings/index-learnings.py --rebuild-manifest
```

### Learnings Manifest

The manifest provides a summary of available learnings by topic, helping Claude make informed decisions about when to search.

**Manifest locations:**
- `~/.projects/learnings/MANIFEST.md` - Global manifest (all learnings)
- `[repo]/.projects/learnings/MANIFEST.md` - Repo-specific manifests

**Example manifest:**
```markdown
# Learnings Manifest
Generated: 2026-02-03T14:30:00Z

## Global Learnings (47 total, 12 corrections)

| Topic | Count | Sample Keywords |
|-------|-------|-----------------|
| authentication | 12 (3⚠️) | JWT, OAuth, refresh tokens, session |
| error-handling | 8 | retries, timeouts, graceful degradation |
| testing | 7 | mocks, fixtures, integration, edge cases |
| performance | 6 | caching, N+1, lazy loading |
| security | 5 | CORS, XSS, input validation |
| other | 2 | |

## Repo Learnings: my-project (23 total)

| Topic | Count | Sample Keywords |
|-------|-------|-----------------|
| api-integration | 9 | GitHub, Slack, webhook |
| memory-system | 4 | ChromaDB, extraction, indexing |
| other | 2 | |
```

**Correction flagging:** Learnings containing "don't", "never", "avoid", "gotcha", etc. are flagged with ⚠️ to indicate they should be prioritized.

**Adding explicit topics:** You can add a `**Topic:**` field to your learning files:
```markdown
# JWT Cookie Storage Best Practice

**Type:** pattern
**Tags:** jwt, authentication, cookies
**Topic:** authentication

## Problem
...
```

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

Add this to your global `~/.claude/CLAUDE.md` to ensure Claude searches learnings automatically:

```markdown
## Learning Compounding

### Available Learnings

@~/.projects/learnings/MANIFEST.md

The manifest above shows what topics have existing learnings. Use it to decide when to search.

### When to Search (Use Manifest to Decide)

1. **Task start**: Search if manifest shows relevant topic exists for your task
2. **Error/stuck**: Search if manifest has topic matching the error domain
3. **Don't search**: If manifest shows no related topics (saves tokens)

### How to Search Effectively

Use topic name + specific context, not just the task description verbatim:
- Good: "authentication JWT refresh token expiry"
- Bad: "implement the login feature for the app"

**Your FIRST action for non-trivial tasks:**
Skill(skill="compound-learning:search-learnings", args="[topic] [specific context]")

Do this BEFORE reading files, running commands, or planning implementation.

### Mid-Conversation Peek

Peek when you start dealing with a topic that exists in the manifest:
- **Topic shift to manifest topic**: Moving to work on authentication, database, testing, etc. - if manifest shows it, peek for it
- **Error in manifest topic area**: Error occurs in a domain the manifest covers
- **Stuck on manifest topic**: 2+ attempts at a problem in an area the manifest shows
- **User references past**: "we tried this before", "remember when"

**How to peek:**
Skill(skill="compound-learning:search-learnings",
      args="[specific error/topic] --peek --exclude-ids [comma-separated IDs from initial search]")

**ID tracking:**
- After initial search at conversation start, remember the returned learning IDs
- Pass all seen IDs to peek searches via --exclude-ids
- This prevents re-surfacing the same learnings (avoids context bloat)

**Peek output handling:**
- If peek finds NEW learnings: "Found additional relevant learning about X" and apply it
- If peek returns {"status": "empty"}: Continue silently (don't mention the peek)

**Key principle:** Peek often, stay silent when empty. Cost of empty peek is near zero.
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

## Development

The plugin uses upsert operations for indexing, making it safe to re-run indexing without creating duplicates. ChromaDB data persists in the configured `dataDir` across container restarts.
