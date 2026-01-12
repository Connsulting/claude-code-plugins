# Connsulting Claude Code Plugins

A marketplace of Claude Code plugins from Connsulting.

## Installation

```
/plugin marketplace add Connsulting/claude-code-plugins
```

## Available Plugins

### compound-learning

Learning compounding system that extracts knowledge from conversations and makes it searchable via ChromaDB vector database.

**Features:**
- Automated learning extraction from conversations
- ChromaDB-backed vector search
- Hierarchical scope (global + repo-specific learnings)
- Configurable via environment variables

**Install:**
```
/plugin install compound-learning@connsulting-plugins
```

See [plugins/compound-learning/README.md](plugins/compound-learning/README.md) for setup and configuration.

## Development

This marketplace is structured for standalone distribution. Each plugin in `plugins/` has its own `.claude-plugin/` directory with plugin.json.
