# work-log

Auto-log substantive Claude Code sessions to a Notion Work Log database via Notion MCP.

On session end, a background subprocess evaluates whether the session produced meaningful work. If so, it writes a structured entry to a daily note in Notion, organized by project.

## Setup

1. Install the plugin: `/plugin install work-log@connsulting-plugins`
2. Create a Notion database with a single `Date` (title) property under your Work Log page
3. Create `~/.claude/plugins/work-log/config.json` with your settings (see below)

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
    "mcpServerName": "notion"
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
| `notion.mcpServerName` | MCP server name for Notion tools | `notion` |
| `sourcePrefix` | Prefix for session tags (e.g., `cc:a1b2c3d4`) | `cc` |
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
