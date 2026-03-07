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

### privacy-policy-checker

Deterministic static checker for machine-readable privacy-policy claims tagged in a policy markdown file.

**Features:**
- Enforces tagged claims like `[privacy-claim:no_pii_logging]`
- CI-friendly exit codes (`0` pass, `1` violations, `2` errors)
- JSON or human-readable reports with `claim_id`, file, line, and evidence

**Install:**
```
/plugin install privacy-policy-checker@connsulting-plugins
```

See [plugins/privacy-policy-checker/README.md](plugins/privacy-policy-checker/README.md) for usage and configuration.

## Development

This marketplace is structured for standalone distribution. Each plugin in `plugins/` has its own `.claude-plugin/` directory with plugin.json.
