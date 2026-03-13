# Hooks

This directory contains the Claude hook registration file plus the shell and Python entrypoints used at runtime.

## Registered Events

The plugin registers hooks in [hooks.json](hooks.json):

| Event | Entrypoint | Async | Reads stdin | Purpose |
|-------|------------|-------|-------------|---------|
| `SessionStart` | [setup.sh](setup.sh) | No | No | Bootstrap Python dependencies |
| `UserPromptSubmit` | [auto-peek.sh](auto-peek.sh) | No | Yes | Search for relevant learnings before Claude responds |
| `PreCompact` | [extract-learnings.sh](extract-learnings.sh) | Yes | Yes | Extract learnings before transcript compaction |
| `SessionEnd` | [extract-learnings.sh](extract-learnings.sh) | Yes | Yes | Extract learnings again at session end |

Because `PreCompact` and `SessionEnd` are async in [hooks.json](hooks.json), extraction failures do not block the foreground Claude session. They are visible only through logs.

## Shell Entrypoints

### `setup.sh`

- Trigger: `SessionStart`
- Purpose: idempotent dependency bootstrap for `pysqlite3-binary`, `sqlite-vec`, and `sentence-transformers`
- Behavior:
  - Creates `~/.claude/plugins/compound-learning/activity.log`
  - Runs a quick import check with `python3`
  - Falls back to `pip install --quiet ...` if imports fail
  - Always exits `0` so session start is never blocked

### `auto-peek.sh`

- Trigger: `UserPromptSubmit`
- Expected stdin fields:
  - `prompt`
  - `transcript_path`
  - `cwd`
- Purpose: extract 1-2 search keywords with `claude -p --model haiku`, then call [../scripts/search-learnings.py](../scripts/search-learnings.py) in `--peek` mode
- Behavior:
  - Exits early if `CLAUDE_PLUGIN_ROOT` is unset
  - Exits early if `CLAUDE_SUBPROCESS` is already set, which prevents nested hook recursion
  - Skips short prompts under 10 characters
  - Uses [extract-transcript-context.py](extract-transcript-context.py) to give Haiku recent assistant context
  - Creates an empty temporary MCP config so keyword extraction avoids MCP/LSP startup cost
  - Stores seen result IDs in `~/.claude/plugins/compound-learning/sessions/<transcript-id>.seen`
  - Prunes `.seen` files older than 24 hours
  - Writes a compact search summary plus the full matched learning content to stdout so Claude can use it immediately

### `extract-learnings.sh`

- Triggers: `PreCompact`, `SessionEnd`
- Expected stdin fields:
  - `session_id`
  - `transcript_path`
  - `cwd`
- Purpose: extract 0-3 reusable learnings from the transcript, write markdown files, and index any newly created files
- Behavior:
  - Exits early if `CLAUDE_PLUGIN_ROOT` is unset
  - Exits early if `CLAUDE_SUBPROCESS` is already set
  - Sources [../lib/git-worktree.sh](../lib/git-worktree.sh) so worktree paths resolve back to the main repo root
  - Skips transcripts shorter than 20 lines
  - Uses [extract-transcript-messages.py](extract-transcript-messages.py) to strip tool noise from the JSONL transcript
  - Invokes `claude -p --permission-mode bypassPermissions` with `Write` and `Bash(mkdir:*)` allowed
  - Diffs the learning directories before and after the Claude subprocess to find newly created markdown files
  - Re-indexes each new file with [../skills/index-learnings/index-learnings.py](../skills/index-learnings/index-learnings.py) `--file`

## Python Helpers

### `extract-transcript-context.py`

- Used only by `auto-peek.sh`
- Reads backward through the transcript and extracts assistant text between the last two real user prompts
- Defaults to 3000 characters so the Haiku prompt stays small

### `extract-transcript-messages.py`

- Used only by `extract-learnings.sh`
- Converts transcript JSONL into plain `[USER]` / `[ASSISTANT]` blocks
- Removes tool calls, snapshots, meta entries, and most command/tool-result noise
- Truncates from the end when the transcript exceeds the byte budget

## Recursion And Safety

- `auto-peek.sh` and `extract-learnings.sh` set `CLAUDE_SUBPROCESS=1` before calling `claude -p`.
- Both scripts check for `CLAUDE_SUBPROCESS` at startup and bail immediately when it is already present.
- `setup.sh` is intentionally non-fatal; dependency bootstrap failures are logged instead of breaking the session.
- `extract-learnings.sh` writes only into `~/.projects/learnings` and `[repo]/.projects/learnings` by passing those directories explicitly via `--add-dir`.

## Logs And Troubleshooting

- Shared log file: `~/.claude/plugins/compound-learning/activity.log`
- Typical failure sources:
  - `jq` missing on `PATH`
  - `timeout` missing on `PATH`
  - `claude` unavailable for the nested subprocess calls
  - malformed or missing `transcript_path` in hook stdin
  - missing Python dependencies before `SessionStart` has completed once
- If auto-peek prints `search failed`, inspect the activity log for the captured stderr from the keyword extraction or search subprocess.
