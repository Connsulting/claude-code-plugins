# Connsulting Claude Code Plugins

A marketplace of Claude Code plugins from Connsulting.

## Installation

```
/plugin marketplace add Connsulting/claude-code-plugins
```

## Available Plugins

### compound-learning

Learning compounding system that extracts reusable knowledge from Claude sessions and PR feedback, indexes it locally in SQLite plus `sqlite-vec`, and surfaces relevant learnings automatically through Claude hooks.

**Features:**
- `SessionStart` dependency bootstrap for Python search/indexing dependencies
- `UserPromptSubmit` auto-peek with keyword extraction and hybrid SQLite search
- `PreCompact` and `SessionEnd` extraction into global and repo-scoped learning files
- Local SQLite, `sqlite-vec`, and FTS5 storage with no Docker requirement
- Slash commands for compounding, PR learnings, indexing, and consolidation

**Install:**
```
/plugin install compound-learning@connsulting-plugins
```

See [plugins/compound-learning/README.md](plugins/compound-learning/README.md) for setup, configuration, hook lifecycle, and maintainer docs.

## Development

This marketplace is structured for standalone distribution. Each plugin in `plugins/` has its own `.claude-plugin/` directory with plugin.json.
