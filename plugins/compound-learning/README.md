# Compound Learning Plugin

A learning compounding system for Claude Code that extracts and indexes knowledge from conversations using ChromaDB for semantic search.

## Overview

The compound learning plugin enables Claude to learn from previous conversations by:
- Extracting key learnings from sessions via the `/compound` command
- Indexing learnings into ChromaDB with semantic embeddings
- Automatically searching relevant past learnings when starting new tasks
- Supporting hierarchical scoping (global and repository-specific learnings)

## Prerequisites

- Python 3.x
- Docker (for ChromaDB container)
- Python packages: `chromadb`

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

## Architecture

### Components

- **Commands:**
  - `/compound`: Extracts learnings from conversations and commits to appropriate scope

- **Skills:**
  - `search-learnings`: Queries ChromaDB for relevant learnings with hierarchical scoping
  - `start-learning-db`: Starts ChromaDB container using Docker
  - `index-learnings`: Indexes all learning markdown files into ChromaDB

### Learning Scopes

1. **Global** (`~/.projects/learnings/`): Security patterns, general best practices, cross-project knowledge
2. **Repo** (`[repo]/.projects/learnings/`): Repository-specific gotchas, patterns, architecture decisions

The search skill automatically detects which repository you're working in and includes both global learnings and learnings from parent directories in the hierarchy.

## Recommended CLAUDE.md Configuration

Add this to your global `~/.claude/CLAUDE.md` to ensure Claude searches learnings automatically:

```markdown
## Learning Compounding

**Query learnings FIRST before any non-trivial task.**

When the user provides a task (not a greeting or typo fix), your FIRST action MUST be:
Skill(skill="compound-learning:search-learnings", args="[task description]")

Do this BEFORE reading files, running commands, or planning implementation.
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
