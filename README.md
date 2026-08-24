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

### big-plan

Serve Markdown plans in the unchanged Big Plan commentable review UI, including anchored comments, reactions, decisions, task toggles, snapshots, diffs, Mermaid diagrams, and feedback delivery to the authoring session.

**Install with Claude Code:**

```text
/plugin install big-plan@connsulting-plugins
```

**Install with Codex:**

```sh
codex plugin add big-plan@connsulting-plugins
```

Plugin installation does not start a service or change Tailscale. Run the plugin's `install.sh --enable` only when ready to activate its stable local runtime. After a healthy bounded Tailscale probe the launcher binds to `0.0.0.0`, preserving localhost callbacks and tailnet access; otherwise it binds to localhost. Because `0.0.0.0` listens on every interface, use it only on a trusted or firewalled host. Direct remote URLs use `http://<MagicDNS-name>:8765/`, while HTTPS requires separately configured Tailscale Serve. See [plugins/big-plan/README.md](plugins/big-plan/README.md) for the portable defaults and lifecycle.

## Development

This marketplace is structured for standalone distribution. Each plugin in `plugins/` has
its own `.claude-plugin/` directory. Plugins that support Codex also include a
`.codex-plugin/plugin.json` manifest and an entry in `.agents/plugins/marketplace.json`.
