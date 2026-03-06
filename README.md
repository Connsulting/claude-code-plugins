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

### schema-evolution-advisor

Deterministic static analysis of SQL migration changes that flags risky schema evolution patterns before merge.

**Features:**
- Scans changed migration SQL files from git diff or explicit paths
- Detects destructive and constraint-tightening patterns with severity rankings
- Emits both human-readable and machine-readable (JSON) reports with mitigation hints

**Install:**
```bash
/plugin install schema-evolution-advisor@connsulting-plugins
```

See [plugins/schema-evolution-advisor/README.md](plugins/schema-evolution-advisor/README.md) for usage and configuration.

## Development

This marketplace is structured for standalone distribution. Each plugin in `plugins/` has its own `.claude-plugin/` directory with plugin.json.
