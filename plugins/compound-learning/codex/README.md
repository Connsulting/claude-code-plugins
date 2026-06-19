# Codex support

These wrappers let the compound-learning plugin run under **Codex** (OpenAI's
CLI) in addition to Claude Code. Codex sessions then both read from and
contribute to the same learnings corpus and SQLite index the Claude side uses.

The plugin's own scripts (`hooks/auto-peek.sh`, `hooks/extract-learnings.sh`,
`hooks/setup.sh`) are reused unchanged — these wrappers only adapt Codex's
runtime to what those scripts expect.

## How it maps

| Plugin behavior | Claude hook | Codex hook | Wrapper |
|---|---|---|---|
| Bootstrap Python deps | `SessionStart` | `SessionStart` | `setup.sh` |
| Inject relevant learnings | `UserPromptSubmit` | `UserPromptSubmit` | `peek.sh` |
| Extract new learnings | `SessionEnd` / `PreCompact` | `Stop` | `extract.sh` |

Two Codex constraints shape the design:

1. **No `SessionEnd`/`PreCompact` and no async hooks.** Extraction hangs off
   `Stop` (fires once per `codex exec`, per-turn when interactive). `extract.sh`
   debounces (skips sessions under 20 messages; in interactive runs re-extracts
   only every ~12 new messages) and self-backgrounds the generator with
   `setsid` so the hook returns immediately.
2. **Different transcript format.** Codex writes rollout JSONL
   (`type:response_item`, `payload.role`, `payload.content[].text`); the plugin
   parsers expect Claude transcript JSONL (`type:user/assistant`,
   `message.content`). `rollout-to-transcript.py` converts between them, so the
   plugin parsers run unchanged.

`UserPromptSubmit` hook stdout is injected into the model context by Codex,
which is what makes learning injection work with no plugin changes.

## Activation

Codex does not load Claude plugins, so you register these hooks in your Codex
config (`~/.codex/config.toml`) by hand. Replace `PLUGIN_DIR` with this plugin's
installed path (e.g. `~/.claude/plugins/compound-learning`):

```toml
[features]
hooks = true

[[hooks.SessionStart]]
[[hooks.SessionStart.hooks]]
type = "command"
command = "bash PLUGIN_DIR/codex/setup.sh"

[[hooks.UserPromptSubmit]]
[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "bash PLUGIN_DIR/codex/peek.sh"

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "bash PLUGIN_DIR/codex/extract.sh"
```

Then **trust the hooks once**: Codex skips untrusted hooks in `codex exec`, so
run `codex` interactively once and approve them (this persists `trusted_hash`
entries under `[hooks.state]`). To smoke-test before trusting, pass
`--dangerously-bypass-hook-trust` to a `codex exec` run.

State (converted transcripts, extraction snapshots, debounce markers) lives
under `~/.claude/state/codex-compound-learning/`; a wrapper activity log is at
`~/.claude/plugins/compound-learning/codex-activity.log`.

## Known limitation

Learning *generation* (and keyword extraction for injection) still shells out to
`claude` (`claude -p` / `claude --model haiku`). This is cross-engine on purpose
— a Claude subprocess cannot recurse into Codex `Stop` hooks — but it means a
Codex session run while the Claude limit is exhausted will no-op extraction
(logged, harmless). A pure-Codex generator is a possible follow-up.
