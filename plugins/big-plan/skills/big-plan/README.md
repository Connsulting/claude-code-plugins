# big-plan

Serve markdown project plans as commentable HTML over Tailscale.

The Big Plan skill tells Claude or Codex to save plan-sized files under `<repo>/.projects/`, register the ready-to-review plan, and surface a direct `http://<MagicDNS-name>:8765/<path>` URL (or localhost when Tailscale is unavailable). There is no slash command; this is a persistent service, not a workflow. Before using a POST endpoint, verify the staged service responds locally; start it with `systemctl --user start big-plan.service` if needed, then check again.

When an operator configures `tailscale serve` (`sudo tailscale serve --bg --https=443 http://127.0.0.1:8765`), it terminates TLS on 443 for the node's MagicDNS name and proxies to the local plain-HTTP server. That optional serve config persists across reboots; disable it with `tailscale serve --https=443 off`. Without it, the direct `http://<MagicDNS-name>:8765/` URL works on the tailnet. The plugin installer does not change Tailscale configuration.

## Deriving the review URL

With separately configured Tailscale Serve, the rendered URL is `https://<node>/<relpath-from-~/git/>`; otherwise the direct URL is `http://<node>:8765/<relpath-from-~/git/>`. Derive `<node>`, never hardcode it:

```bash
tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'
```

When the optional HTTPS proxy is configured, its URL carries no port. Otherwise use the direct HTTP URL with port `8765`.

**Heading text must stay stable once a reviewer has commented.** Review comments
flow back into the doc in the next session;
comments anchor on the heading slug, so renaming a heading orphans its comments.

## Architecture

```
<root>/
  some-plan.md                     <- source of truth
  some-plan.md.comments.json       <- sidecar, written by the server when a comment is posted
```

- `render.py` converts markdown to HTML on the fly. Each `##` heading opens a collapsible `<details>` block; every heading gets a stable anchor matching the slugified heading text. Every paragraph, list item, and blockquote gets a `b-<hash>` block anchor derived from its text, so comments can attach to any block.
- `server.py` is a stdlib HTTP server (no Flask) running under `uv run` with inline deps. Serves HTML, static assets, and POST endpoints for comments.
- Comments anchor on heading slugs or block-hash anchors, not line numbers, so they survive reorders and edits as long as text stays stable.
- Sidecar JSON shape: `{"comments": [{"id", "anchor", "type", "author", "timestamp", "resolved", "replies": [...], ...type-specific}]}`

## Change highlighting (diff since last review)

When you revise a plan after the user comments, the changed blocks light up in the normal doc view: **green** for added blocks, **amber** for reworded blocks. No side-by-side; the doc reads as prose with the deltas tinted, which works on a phone. A "N changed" chip plus `hide changes` / `reviewed` controls appear in the topbar only when a diff is active.

How it works:

- The baseline is a single sidecar named `<plan>.md.snapshot` holding the version the user last reviewed. There is exactly one snapshot per plan; it is overwritten on each revision. It does **not** end in `.md`, so agentic `*.md` search, the Markdown index scan, and direct GETs all ignore it -- the canonical `.md` is always the one people see.
- `render.py` diffs current-vs-snapshot at the block level by reusing the existing anchor hashes (unchanged text keeps its anchor and aligns; reworded text gets a new anchor). A `replace` of N old blocks by M new ones is read as N edits (`changed`) plus M-N additions (`added`). Removed blocks have no current element to tag and are not surfaced.
- Comments that get resolved during a revision already disappear from the view (`comments_for_anchor` filters `resolved`), so the right rail clears itself; the highlight is what carries the trace of what changed.

Agent workflow for a revision:

1. **Before editing**, set the baseline to the version the user reviewed -- either copy the file (`cp plan.md plan.md.snapshot`) or `POST /api/snapshot/<rel>`.
2. Edit `plan.md` and resolve the addressed comments in the sidecar as usual.
3. The next render highlights the deltas. The user taps **reviewed** (`POST /api/snapshot/<rel>/clear`) to drop the baseline and return to a clean view, or the next revision's baseline overwrites it.

The green/amber split is best-effort at edit/insert boundaries; when in doubt a block is tinted as a change rather than missed.

### The diff tab (`?view=diff`)

The tint answers "where did something change"; the diff tab answers "what exactly changed". `GET /<plan>.md?view=diff` (the **View Diff** link in the topbar) renders a GitHub-style collapsed unified diff of the *raw markdown*, baseline snapshot -> current:

- Only changed hunks show, with 3 lines of context; the unchanged runs between them collapse behind **Expand N unchanged lines** toggles. Red `-` / green `+` lines with old/new line-number gutters.
- **Word-level highlight**: within a reworded line, `render.py` pairs the old/new lines index-wise and runs a token-level diff (`word_diff`, words + whitespace runs). Only the differing words get a `<span class="wd">` (stronger background + bold), so you see the exact edit, not just the whole line. Because plans write each paragraph as one line, this runs within the paragraph.
- Each hunk has an inline **Reviewed** button (Cursor-accept style) backed by `POST /api/snapshot/<rel>/accept` with `{old_start, old_end, old_b64, new_b64}`. It splices that hunk's new lines into the snapshot so the hunk stops showing while the others remain. The `*_b64` fields are `base64(JSON(list[str]))` (JSON so empty pure-insert ranges round-trip). The server 409s if the snapshot moved under the client (`old_lines` no longer match), and the client reloads.
- Accept is positive-only: it advances the baseline, it never rewrites the current plan. There is no reject/revert.

## Comment types

The sidecar records four comment types. All share `id`, `anchor`, `type`, `author`, `timestamp`, `resolved`. Type-specific fields:

- `text` (default): `text` (string). Free-form comment captured via the "+" button. Stacks (multiple text comments per anchor are allowed).
- `reaction`: `emoji` (one of 👍 👎 🤔). Captured by tapping a reaction button on any anchored block. Stacks. Tap an existing chip to delete it (no confirmation).
- `decision`: `choices` (string[]), optional `question` (string). Captured by tapping options in a `decide` / `decide-multi` block; saved automatically on change. The server replaces any prior unresolved decision on the same anchor, so the sidecar holds at most one open decision per card. Tapping a selected radio again clears the selection (and the sidecar entry).
- `status`: legacy. Task checkbox taps now mutate the source .md directly (see Task checkboxes below), so new sessions won't write status comments. Existing `status` entries in older sidecars are accepted but not rendered.

### Replies (threads)

Not a fifth type: a reply nests under its parent comment as `replies: [{"id", "role", "author", "text", "timestamp"}]`, where `role` is `agent` or `reviewer`. Nesting rather than flattening is deliberate -- a flat `reply` type would need an exclusion added to every filter that already exists (the rail, the unresolved badge, the decision/status replacement passes, the feedback aggregator), and threads are per-comment anyway.

Replies exist because not every comment is an edit request. Some are questions, and before this the agent's only outlets were to fold an answer into the plan prose, answer into a chat session the reviewer is not looking at, or resolve the comment silently. `POST /api/comments/<rel>/<id>/reply` with `{"text": ...}` is the fourth outlet. `role` defaults to `agent` (the curl path); the web UI sends `reviewer`, so a thread reads top to bottom as one conversation.

**An answered comment is never resolved by the agent that answered it.** `resolved` means "addressed in the plan", and resolved comments do not render at all (`comments_for_anchor` filters them), so answer-then-resolve deletes the answer before the reviewer ever sees it. The comment stays open and flips to *answered*: the last reply came from an agent. The reviewer dismisses it with the same `×` as any other comment. The endpoint enforces the edges -- replying into a resolved comment `409`s, and replying to a reaction or status `400`s, since neither renders a thread.

Answered threads surface three ways so a phone reader can find them: a `data-answered="true"` tint on the comment, an `N answered` chip in the topbar that walks them one tap at a time (opening the enclosing section and flashing the target), and a separate `N answered` badge next to the open count on the index.

### Deleting

Every comment surface has a `×` (text/decision/status) or tap-to-toggle (reactions, task checkboxes) delete affordance. Use `POST /api/comments/<rel>/<id>/delete` to remove a comment by id. The legacy `/resolve` endpoint is still served for any script that uses it but the UI no longer surfaces it.

## Authoring conventions (HTML affordances)

Beyond plain markdown, plans can use these block conventions:

- **Task checkboxes**: `- [ ] Set up Postgres` / `- [x] Done`. Rendered as tappable checkboxes. Tapping a checkbox mutates the source .md (flips `[ ]` <-> `[x]` on that line) via `POST /api/task/<rel>` with `{line, checked}`. The marker carries `data-md-line` from render time so the server hits the exact line.
- **Decision blocks**: a fenced block with info-string `decide` (radio) or `decide-multi` (checkbox). First non-list line is the question; bullets are options. Every card automatically appends an "Other..." row with a text input — pick it and type a custom choice, hit Enter (or blur) to save.
  ````markdown
  ```decide
  Which database?
  - Postgres
  - SQLite
  ```
  ````
- **Comparison grids**: raw HTML `<div class="compare">` containing `<div class="compare-col" markdown="1">` children. Renders as a responsive grid with markdown inside each column.

## Copy-feedback round-trip

Each plan page has a `copy feedback as prompt` button at the bottom of the content area that aggregates every open (unresolved) comment into a clipboard-ready prompt the user can paste into the next session. The prompt is organized by anchor, in document order, and labels each entry by type (reaction / decision / text comment).

Each comment carries its own thread and its comment id, so a second round of feedback does not read as a fresh question the agent already answered; an answered thread is explicitly marked `ANSWERED by you, no reviewer reply since`. The prompt closes with the three-way triage (edit the plan / reply and leave it open / leave it for the reviewer's decision) and the literal reply curl, because the pasted text has to stand alone.

## Raw source view

**Raw** in the topbar, between Copy and Download, opens `GET /<plan>.md?view=raw`: the plan source as preformatted text in the same chrome, with a **View Doc** link back. Use it when you want to read the markdown without downloading a file. Unlike Copy and Download it stays visible at mobile width, because a phone is where saving a `.md` is the least convenient.

## Send-feedback round-trip (no copy/paste)

**Send** in the topbar, right of Copy and Download, delivers that same aggregated prompt straight to the session that wrote the plan. It ships from the server `disabled` and only enables once a destination resolves.

The topbar has no room for a session name, so the label stays short and the destination surfaces three other ways: in the tooltip, in a confirm dialog when (and only when) the route is `dispatch` and a press would spend a session that is not already the plan's, and in the flash after the send (`Home Directory Spring Cle… ✓`). Unlike Copy and Download it stays visible at mobile width, because the phone is where the comments actually get written.

### Provenance: `<plan>.md.session`

Routing needs to know who wrote the plan, and nothing else records that. The authoring agent writes a sidecar next to the plan. Like `.md.snapshot`, it deliberately does **not** end in `.md`, so the index and agentic Markdown searches ignore it.

```json
{
  "engine": "claude",
  "sessionId": "ab612b3a-acb9-4038-ada0-474c4324a817",
  "name": "Home Directory Spring Cleaning Audit",
  "cwd": "/home/example/project",
  "recordedAt": "2026-08-14T18:00:00+00:00"
}
```

`engine` is `claude`, `codex`, or `grok`.

Register it with one call rather than hand-writing the file. From a Claude session:

```bash
SID="$CLAUDE_CODE_SESSION_ID"
curl -sX POST "http://localhost:8765/api/session/<rel/path.md>" -H 'Content-Type: application/json' \
  -d "$(~/.local/bin/claude agents --json | jq -c --arg sid "$SID" \
        '.[]|select(.sessionId==$sid)|{engine:"claude",sessionId,name,cwd}')"
```

From Codex, `engine` is `codex` and `sessionId` is the rollout UUID (the trailing UUID in `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`).

From Grok, `engine` is `grok` and `sessionId` is `$GROK_SESSION_ID` (the session directory name under `~/.grok/sessions/<encoded-cwd>/`):

```bash
curl -sX POST "http://localhost:8765/api/session/<rel/path.md>" -H 'Content-Type: application/json' \
  -d "$(jq -n --arg sid "$GROK_SESSION_ID" --arg cwd "$PWD" \
        '{engine:"grok",sessionId:$sid,name:"",cwd:$cwd}')"
```

A Grok-labelled sidecar that was registered as `codex` (the only valid engine before Grok delivery existed) is healed at send time when that id is still a Grok session on disk, so those plans do not mint a new Codex thread.

Missing or corrupt provenance is not an error; it degrades to a fresh dispatch.

### Index publication

The index is deliberately **opt-in**: a Markdown file appears there only when
its adjacent `<plan>.md.big-plan` marker exists. Existing `<plan>.md.session`
provenance registrations also count as already-promoted plans, so the new rule
does not hide plans that were explicitly handed off before markers existed. This
keeps ordinary `.projects/` drafts, research notes, and internal handoffs out of
the phone-facing plan list.

The normal `POST /api/session/<rel>` registration above creates the marker as
part of registering a ready-to-review plan. To publish a plan that intentionally
has no feedback provenance, call:

```bash
curl -sX POST "http://localhost:8765/api/promote/<rel/path.md>"
```

Promotion affects the index only: direct plan, raw Markdown, and PDF URLs keep
working for unpromoted files. Remove the `.md.big-plan` marker to take a plan
back out of the index.

### The five delivery modes

`GET /api/submit/<rel>` previews the decision with no side effects; `POST` (body `{"prompt": "..."}`) performs it.

| Mode | When | Mechanism |
| --- | --- | --- |
| `message` | Claude session is live in the roster | A throwaway `claude -p` courier calls `SendMessage`, waking the idle session in place with its authoring context intact |
| `resume` | Claude session recorded but no longer live | `claude --bg --model opus[1m] --resume <id>` replays its transcript into a fresh worker |
| `codex` | Codex thread | Joins the thread in the local Codex app server, then starts a turn or steers its active turn |
| `grok` | Grok thread | Joins the local Grok leader and prompts that session, loading it first when it is not already resident. With no leader at all, `grok --resume --prompt-file` |
| `dispatch` | No usable provenance | `agent-router run --dir <repo>` starts fresh and owns the engine choice |

There is no Claude CLI verb for cross-session messaging, which is why `message` mode shells a courier: `SendMessage` is a tool, not a command. Codex uses its local app server instead of `codex exec resume`: the service joins the loaded thread through the control socket, starts a turn when idle, and steers a regular turn when active. Grok does the same through the shared leader (`session/prompt`, or `session/load` then `session/prompt`) instead of `grok -p --resume`. Both preserve the authoring session and avoid opening a second writer.

### Failures are reported, not logged and forgotten

A Claude or router send spawns detached, so the HTTP response cannot wait for an agent turn. It does wait `EARLY_FAILURE_WINDOW` (2.5s, `BIG_PLAN_EARLY_FAILURE_WINDOW`) and fails the request with the process's own stderr if the agent died in that window. Codex delivery is synchronous through the local app server and returns its new or steered turn ID. Grok delivery is synchronous through the local leader until the roster shows the session accepted the prompt. Other early failures such as a missing binary or rejected session id still return an error. Without the early watch the endpoint returned `200 sent` for an agent that never started, and the reason sat in an outbox log nobody reads.

If `claude agents --json` cannot be read at all, the route degrades to `dispatch`, never `resume`. Guessing wrong the other way puts a second worker on a live transcript, and the two interleave writes into one JSONL.

### Why the feedback text never reaches a shell

Every command is built as an argv list (no `shell=True`) and the reviewer's comment text is written to an outbox file under `~/.local/state/big-plan/outbox/`; only that path is ever passed to a subprocess. A comment containing backticks or `;rm -rf /` is inert. The one sidecar field that does reach argv, `sessionId`, is validated against a UUID-shaped regex.

The payload the target receives wraps the aggregated comments with the plan path, the comment sidecar path, and the `cp plan.md plan.md.snapshot` baseline step, because a session reading it mid-context will not go re-read this README before editing.

### Cost

A send costs the target session a full turn of its own budget, so this is one press per feedback batch, not one per comment. That is the deliberate exception to the fire-and-forget rule: reviewer feedback arriving is new information the run needs, not a progress poll.

## Deployment

Persistent systemd `--user` service. The packaged unit template lives at `scripts/big-plan.service` and uses `%h` so it works for any user. The installed unit lives at `~/.config/systemd/user/big-plan.service`.

```bash
systemctl --user status big-plan.service          # status
systemctl --user restart big-plan.service         # apply config changes
journalctl --user -u big-plan.service -f          # live logs
```

## Bootstrap on a new machine

```bash
# 1. Prerequisite: uv (https://docs.astral.sh/uv/). Tailscale is optional.
# 2. From the plugin root, stage the runtime and unit:
./install.sh

# 3. Enable, start, and confirm (or use ./install.sh --enable):
systemctl --user daemon-reload
systemctl --user enable big-plan.service
systemctl --user restart big-plan.service
systemctl --user status big-plan.service

# 4. Survive logout (only needed once per user per machine):
sudo loginctl enable-linger $USER

# 5. If Tailscale is connected, derive and verify this node's direct HTTP URL:
ts_node=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
curl -s -o /dev/null -w "%{http_code}\n" "http://$ts_node:8765/"
```

Serves `~/git/` with `--filter plans` on port 8765.

## Index paging

The root index is an explicitly published recent-work queue: it shows the 20
most recently modified promoted Markdown files first, including their UTC update
time. Use **Next** and **Previous** to browse further 20-file pages (`/?page=2`,
`/?page=3`, and so on). Direct plan URLs continue to work regardless of whether
a file is promoted or which index page it appears on.

For ad-hoc serving from a different root:

```bash
~/.local/bin/big-plan /path/to/dir [--filter plans|readmes|all]
```

## Security posture

No auth. The launcher binds to `0.0.0.0` only after a bounded probe confirms a running Tailscale backend, nonempty MagicDNS name, and IPv4 address; otherwise it binds to localhost. The healthy-path bind keeps localhost callbacks, direct tailnet access, and an existing Tailscale Serve proxy working. `0.0.0.0` listens on every interface, so this mode is suitable only on a trusted or firewalled host. The `/assets/` route is allowlisted to `style.css`, `app.js`, `diff.js`, and `mermaid.min.js`. The `/raw/` route only serves files ending in `.md`. Path traversal is blocked via `safe_join`.

`POST /api/submit` raises the stakes of that posture: it spawns a Claude, Codex, or Grok process, so anything that can reach port 8765 can start an agent on this box. It is not an arbitrary-command surface (argv lists only, no shell, prompt text confined to a file, `sessionId` regex-validated, and the target is whatever the plan's own provenance sidecar names), but it is a compute trigger. Same rule, sharper edge: tailnet only.

**CSRF.** "Tailnet only" does not cover the browser: `localhost:8765` is reachable from any page the user has open, tailnet or not, so without a guard any website could have forged a POST that spawned an agent. Every `POST` is therefore refused when it looks cross-origin -- `Sec-Fetch-Site` is anything but `same-origin`/`none`, or `Origin` disagrees with `Host`. Requests carrying neither header are allowed, which is what keeps the `curl` registration flow above working; a browser always sends at least one on a cross-origin POST. The two spawn endpoints additionally require `Content-Type: application/json`, so a `text/plain` "simple request" cannot skip the preflight that we deliberately never answer.

**DNS rebinding.** Comparing `Origin` to `Host` is not sufficient on its own. An attacker who serves a page from their own `:8765` and then rebinds that hostname to this machine gets matching `Origin` and `Host` and a `Sec-Fetch-Site: same-origin`, and every check above passes. `POST` therefore also requires `Host` to be a hostname that is actually ours -- localhost, this node's hostname, and the MagicDNS name plus tailnet IPs read from `tailscale status`. Extend it with `BIG_PLAN_ALLOWED_HOSTS` (comma-separated). The list is printed at startup, because a POST 403ing on an unexpected hostname is otherwise a mystery. A miss re-reads tailscale at most once a minute before rejecting, so a service that started before `tailscaled` heals on first use instead of 403ing until the next restart. `GET` is deliberately not host-gated: reading a plan from a bare IP or an unexpected name should keep working. Verified through `tailscale serve`, which preserves `Host`.

**Destination integrity.** The button labels itself from a `GET` preview but the `POST` re-routes, so it sends `expectMode` and `expectSessionId` and the server `409`s if the route moved in between. The validated route is then handed to `dispatch.submit` rather than recomputed, so the check and the spawn cannot disagree about where the feedback went. A button that promised the authoring session can never quietly spend a fresh one instead.

## Authoring a new plan

Scaffold from `template.md`. Keep the canonical H2 sections (you can add more):

1. `## Goal` -- what changes and why it matters; the outcome, not the steps
2. `## Technical Architecture` -- shape of the solution; stack, components, data flow
3. `## Milestones` -- ordered chunks of work with rough sizing
4. `## Open Questions` -- decisions not yet made, with options

Stable heading text matters. Comments anchor on slug, so do not rename headings after the user has commented. If you must rename, manually move the comment's `anchor` field in the sidecar.

## Reading comments back

In a future session, look for `*.comments.json` sidecars next to plan files. Filter to `resolved: false`. Group by anchor; for each anchor, surface the comments and decide whether to update the plan text, push the decision into a sub-task, or mark the comment resolved.

Answer a question rather than editing the plan for it -- and leave the comment open when you do:

```bash
jq -n --arg t 'your answer' '{text:$t}' | curl -sX POST \
  http://localhost:8765/api/comments/<rel/path.md>/<comment-id>/reply \
  -H 'Content-Type: application/json' -d @-
```

Mark a comment resolved via:

```bash
curl -X POST http://localhost:8765/api/comments/<rel/path.md>/<id>/resolve
```

Or edit the sidecar directly (`"resolved": true` and add `"resolved_at"`).
