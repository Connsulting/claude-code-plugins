# Connsulting Claude Code and Codex Plugins

A marketplace of Claude Code and Codex plugins from Connsulting.

## Installation

Claude Code:

```
/plugin marketplace add Connsulting/claude-code-plugins
```

Codex, from a local checkout:

```sh
codex plugin marketplace add /absolute/path/to/claude-code-plugins
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

### bonus-drain

A standalone queue, capacity planner, usage-cache refresher, scout, dispatcher, and secure
localhost viewer for opportunistic low-priority work. Providers, plans, accounts, limits,
resets, and adapters are generic JSON configuration; dispatch goes through
`agent-router`.

**Install with Claude Code:**

```text
/plugin install bonus-drain@connsulting-plugins
```

**Install with Codex:**

```sh
codex plugin add bonus-drain@connsulting-plugins
```

Installing the plugin does not activate services, migrate a database, or switch live
routing. See [plugins/bonus-drain/README.md](plugins/bonus-drain/README.md) for dependencies,
configuration, staging, security, migration, rollback, and manual cutover.

## Development

This marketplace is structured for standalone distribution. Each plugin in `plugins/` has
its own `.claude-plugin/` directory. Plugins that support Codex also include a
`.codex-plugin/plugin.json` manifest and an entry in `.agents/plugins/marketplace.json`.
