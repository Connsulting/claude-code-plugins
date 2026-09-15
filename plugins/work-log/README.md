# work-log

Auto-log substantive Claude, Grok, and Codex sessions to a Notion Work Log database via Notion MCP.

On session end (Claude and Grok) or Stop (Codex), a background subprocess evaluates whether the session produced meaningful work. If so, it writes a structured entry to a daily note in Notion, organized by project. Tags identify the engine: `cc:` Claude, `gx:` Grok, `cx:` Codex.

## Setup

### Claude

1. Install the plugin: `/plugin install work-log@connsulting-plugins`
2. Create a Notion database with a single `Date` (title) property under your Work Log page
3. Create `~/.claude/plugins/work-log/config.json` with your settings (see below)

Claude's SessionEnd plugin hook is enough. The Notion write still uses `claude -p` and the Claude.ai Notion MCP for every engine.

### Grok

Grok does not run untrusted plugin hooks, so register the wrapper in `~/.grok/config.toml` (this repo: `claude-settings/grok/config.toml`):

```toml
[[hooks.SessionEnd]]
hooks = [
  { type = "command", command = "bash /absolute/path/to/plugins/work-log/grok/session-end.sh", timeout = 10 },
]
```

The wrapper returns immediately. Grok's SessionEnd budget is about 10 seconds; the Notion write runs detached.

### Codex

Codex has no SessionEnd and no async hooks. Register a Stop hook in `~/.codex/config.toml` (this repo: `claude-settings/codex/config.toml`):

```toml
[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "bash /absolute/path/to/plugins/work-log/codex/work-log-stop.sh"
```

The wrapper debounces short or unchanged interactive turns, then detaches. Trust the new hook once in an interactive Codex session (`codex exec` skips untrusted hooks).

## Configuration

The plugin ships with generic defaults in `.claude-plugin/config.json`. To customize, create a user config at `~/.claude/plugins/work-log/config.json` with only the fields you want to override. This file survives plugin reinstalls.

Example user config:

```json
{
  "notion": {
    "databaseId": "your-database-id-here"
  },
  "timezone": "America/New_York",
  "defaultProject": "personal",
  "projectMappings": {
    "old-folder-name": "preferred-name"
  }
}
```

All available fields (bundled defaults):

```json
{
  "notion": {
    "databaseId": "YOUR_DATABASE_ID_HERE",
    "mcpServerName": "claude_ai_Notion"
  },
  "sourcePrefix": "cc",
  "minTranscriptLines": 40,
  "timezone": "UTC",
  "defaultProject": "personal",
  "projectPattern": ".*/git/([^/]+).*",
  "projectMappings": {}
}
```

| Field | Description | Default |
|-------|-------------|---------|
| `notion.databaseId` | Notion database ID for daily notes | (required) |
| `notion.mcpServerName` | MCP server name for Notion tools | `claude_ai_Notion` |
| `sourcePrefix` | Prefix for Claude session tags (e.g., `cc:a1b2c3d4`). Grok uses `gx`, Codex uses `cx`. | `cc` |
| `minTranscriptLines` | Skip sessions shorter than this | `40` |
| `timezone` | Timezone for timestamps and date boundaries | `UTC` |
| `defaultProject` | Project name when pattern does not match | `personal` |
| `projectPattern` | Regex with capture group to extract project from cwd | `.*/git/([^/]+).*` |
| `projectMappings` | Rename extracted projects (e.g., `{"old-name": "new-name"}`) | `{}` |

### Project detection

The plugin extracts a project name from the working directory using `projectPattern`. The default pattern captures the folder name after `git/` in the path:

```
~/git/acme-corp/my-repo  ->  acme-corp
~/git/personal/dotfiles  ->  personal
```

To use the repo name instead, change the pattern:

```json
"projectPattern": ".*/([^/]+)$"
```

Use `projectMappings` to rename projects after extraction:

```json
"projectMappings": {
  "old-folder-name": "preferred-name"
}
```

## Notion page structure

Each day gets a database row titled `YYYY-MM-DD`. Inside each daily page:

- **H2** per project
- **Toggle** per session, labeled `{sourcePrefix}:{first 8 chars of session ID}`
- **Paragraph** inside toggle with timestamped summary

Resumed sessions (same session ID) append inside the existing toggle.

Ticket references (JIRA, Linear, GitHub issues) found in the transcript are included in the summary.

## Logs

Activity is logged to `~/.claude/plugins/work-log/activity.log`.
