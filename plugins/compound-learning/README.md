# Compound Learning Plugin

A learning compounding system for Claude Code that extracts and indexes knowledge from conversations using local SQLite-vec semantic search. No Docker required.

## Prerequisites

- Python 3.x
- `sentence-transformers` and `sqlite-vec` Python packages
- GitHub CLI (`gh`) - optional, required for `/pr-learnings`

## Installation

### From Remote Repository

```bash
/plugin marketplace add Connsulting/claude-code-plugins
/plugin install compound-learning@connsulting-plugins
```

### Manual Installation

1. Clone or download this plugin to your Claude plugins directory
2. Install Python dependencies:
```bash
pip install sentence-transformers sqlite-vec
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
  }
}
```

`${HOME}` expands to your home directory.

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
4. A session tracking file (`~/.claude/compound-processed-sessions`) prevents duplicate extraction

**Debug log:** `~/.claude/compound-hook-debug.log`

**Note:** Extraction uses minimal permissions (`Read`, `Write`, `Bash(mkdir:*)`) and skips trivial sessions (<20 transcript lines).

## MCP Server (Codex CLI / Cross-Tool)

The plugin includes an MCP server that exposes search and index functionality to any MCP-compatible client. This is the recommended way to use compound-learning with **OpenAI Codex CLI** or other tools that support MCP but lack Claude Code's native hooks.

### Setup

1. Install dependencies:
```bash
pip install mcp sentence-transformers sqlite-vec
```

2. Configure your client:

**Codex CLI** (`~/.codex/config.toml`):
```toml
[mcp_servers.compound-learning]
type = "stdio"
command = "python3"
args = ["/path/to/plugins/compound-learning/mcp-server/server.py"]
```

**Claude Code** (`~/.claude/settings.json`) — if you prefer MCP over native hooks:
```json
{
  "mcpServers": {
    "compound-learning": {
      "command": "python3",
      "args": ["/path/to/plugins/compound-learning/mcp-server/server.py"]
    }
  }
}
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `search_learnings` | Semantic search across all learnings. Supports parallel multi-keyword search, hybrid re-ranking, peek mode, and ID exclusion. |
| `index_learnings` | Re-index all learning markdown files. Discovers global and repo-scoped files, generates embeddings, prunes orphans, and regenerates the manifest. |
| `index_file` | Index a single `.md` file into the database. |
| `get_stats` | Get knowledge base statistics: total count, topic breakdown, scope distribution. |

### Codex + Claude Code Side-by-Side

The MCP server and native Claude Code hooks share the same SQLite database. You can use both simultaneously — learnings extracted by Claude Code hooks are immediately searchable via the MCP server in Codex, and vice versa.

### Limitations vs Native Hooks

| Feature | Native Hooks (Claude Code) | MCP Server (Codex) |
|---------|---------------------------|---------------------|
| Auto-extract on session end | Yes | No — use `index_learnings` tool manually |
| Auto-peek on every prompt | Yes | No — agent must call `search_learnings` explicitly |
| Pre-compaction extraction | Yes | No |
| Search learnings | Yes (auto + manual) | Yes (manual via tool call) |
| Index learnings | Yes (auto + `/index-learnings`) | Yes (`index_learnings` tool) |

### Recommended Codex Instructions

Add to your Codex instructions file (or `AGENTS.md`):

```
## Knowledge Base

You have access to a compound-learning MCP server with a searchable knowledge base.

Before starting a task, call search_learnings with 1-2 keywords related to the task.
After finishing work, remind the user to run index_learnings if new learning files were created.

Use topic + context: "authentication JWT refresh" not "implement login feature".
```

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
  - `PreCompact`: Auto-extracts learnings before context compaction
  - `Stop`: Auto-extracts learnings when Claude finishes responding

- **MCP Server:**
  - `mcp-server/server.py`: Exposes search/index as MCP tools for Codex CLI and other clients

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
