# big-plan

Serve markdown project plans as commentable HTML over Tailscale.

The Big Plan skill tells Claude or Codex to save plans under `${XDG_STATE_HOME:-$HOME/.local/state}/big-plan/plans/<repo-name>/`, register the plan for review, and share its verified `https://<MagicDNS-name>/<path>` URL when Tailscale Serve proxies HTTPS on port 443. There is no slash command; this is a persistent service, not a workflow. Before using a POST endpoint, verify the staged service responds locally over HTTP on port `8765`; start it with `systemctl --user start big-plan.service` if needed, then check again.

When an operator configures `tailscale serve` (`sudo tailscale serve --bg --https=443 http://127.0.0.1:8765`), it terminates TLS on 443 for the node's MagicDNS name and proxies to the local plain-HTTP server. That optional serve config persists across reboots; disable it with `tailscale serve --https=443 off`. Without it, the direct `http://<MagicDNS-name>:8765/` URL works on the tailnet. The plugin installer does not change Tailscale configuration.

## Deriving the review URL

Derive `<node>` from the current Tailscale status, never hardcode it:

```bash
ts_node=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
tailscale serve status --json
```

Check that the Serve result has `TCP["443"].HTTPS` set to true and `Web["<node>:443"].Handlers["/"].Proxy` set to `http://127.0.0.1:8765`. Then use `https://<node>/<encoded-path-relative-to-the-plan-root>`, with no explicit port. Before handing it to a reviewer, make a normal HTTPS GET to that exact plan URL and require HTTP 200. For example, run `curl -sS -o /dev/null -w '%{http_code}\n' "https://$ts_node/<encoded-path>"` with the actual encoded plan path. A HEAD request is insufficient. If the mapping exists but the GET fails, investigate before sharing the URL.

When Serve is not configured, use the direct `http://<node>:8765/<encoded-path-relative-to-the-plan-root>` URL. When Tailscale is unavailable, use `http://127.0.0.1:8765/<encoded-path-relative-to-the-plan-root>`. The local API also uses plain HTTP on port `8765` for registration and other POST requests.

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
- `decision`: `choices` (string[]), optional `question` (string), and optional `note` (string). Captured by tapping options in a `decide` / `decide-multi` block; saved automatically on change. A note can explain the selected option(s), condition the decision, or name follow-up work. The server replaces any prior unresolved decision on the same anchor, so the sidecar holds at most one open decision per card. Tapping a selected radio again clears the selection (and the sidecar entry).
- `status`: legacy. Task checkbox taps now mutate the source .md directly (see Task checkboxes below), so new sessions won't write status comments. Existing `status` entries in older sidecars are accepted but not rendered.

### Replies (threads)

Not a fifth type: a reply nests under its parent comment as `replies: [{"id", "role", "author", "text", "timestamp"}]`, where `role` is `agent` or `reviewer`. Nesting rather than flattening is deliberate -- a flat `reply` type would need an exclusion added to every filter that already exists (the rail, the unresolved badge, the decision/status replacement passes, the feedback aggregator), and threads are per-comment anyway.

Replies exist because not every comment is an edit request. Some are questions, and before this the agent's only outlets were to fold an answer into the plan prose, answer into a chat session the reviewer is not looking at, or resolve the comment silently. `POST /api/comments/<rel>/<id>/reply` with `{"text": ...}` is the fourth outlet. `role` defaults to `agent` (the curl path); the web UI sends `reviewer`, so a thread reads top to bottom as one conversation.

**An answered comment is never resolved by the agent that answered it.** `resolved` means "addressed in the plan", and resolved comments do not render at all (`comments_for_anchor` filters them), so answer-then-resolve deletes the answer before the reviewer ever sees it. The comment stays open and flips to *answered*: the last reply came from an agent. The reviewer dismisses it with the same `×` as any other comment. The endpoint enforces the edges -- replying into a resolved comment `409`s, and replying to a reaction or status `400`s, since neither renders a thread.

Answered threads surface three ways so a phone reader can find them: a `data-answered="true"` tint on the comment, an `N answered` chip in the topbar that walks them one tap at a time (opening the enclosing section and flashing the target), and a separate `N answered` badge next to the open count on the index.

### Deleting

Every text/decision/status comment has a `✓` resolve button and a `×` delete button. `✓` marks it resolved and preserves it in the sidecar, while resolved comments stop rendering; `×` removes it from the sidecar. Reactions and task checkboxes retain their tap-to-toggle behavior. Use `POST /api/comments/<rel>/<id>/resolve` or `/delete` respectively.

## Authoring conventions (HTML affordances)

Beyond plain markdown, plans can use these block conventions:

- **Task checkboxes**: `- [ ] Set up Postgres` / `- [x] Done`. Rendered as tappable checkboxes. Tapping a checkbox mutates the source .md (flips `[ ]` <-> `[x]` on that line) via `POST /api/task/<rel>` with `{line, checked}`. The marker carries `data-md-line` from render time so the server hits the exact line.
- **Decision blocks**: a fenced block with info-string `decide` (radio) or `decide-multi` (checkbox). First non-list line is the question; bullets are options. Every card automatically appends an "Other..." row with a text input and an optional note field. Select the option(s), then blur the note field or press Ctrl/Cmd+Enter to save the choice and note together.
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

## Send-feedback round-trip

The topbar delivers the aggregated open feedback through T3. It starts disabled and enables once the server resolves the destination. **Send** delivers to the authoring T3 thread; **New thread** starts a T3 thread in the plan's repository and asks for confirmation first. The tooltip names the destination, and the button confirms delivery afterward.

### Provenance: `<plan>.md.session`

Registration records the T3 thread that authored the plan, plus its repository working directory. The sidecar does not end in `.md`, so the index and Markdown searches ignore it.

```json
{
  "threadId": "mcp:ab612b3a-acb9-4038-ada0-474c4324a817",
  "name": "Home Directory Spring Cleaning Audit",
  "cwd": "/home/example/project",
  "recordedAt": "2026-10-13T18:00:00+00:00"
}
```

In a T3 agent session, call `t3_thread_list` without a project ID and use its `currentThreadId`, or use `orchestrator_capabilities.parentThreadId`. These are app thread IDs, including prefixes such as `mcp:`. T3 does not currently expose a thread-ID environment variable. Do not use a Claude session ID, Codex rollout UUID, or Grok session ID.

Pass that tool result to registration, along with the actual repository directory:

```bash
# THREAD_ID is the exact currentThreadId returned by the T3 tool above.
jq -n --arg tid "$THREAD_ID" --arg cwd "$PWD" \
  '{threadId:$tid,name:"Plan review",cwd:$cwd}' | \
  curl -sS -X POST "http://localhost:8765/api/session/<encoded-relative-path>" \
    -H 'Content-Type: application/json' -d @-
```

Outside T3, register `{threadId:null,name,cwd}`. This records the repository so Send can start a new thread. Never invent a thread ID. Existing sidecars that record only a provider session ID use the new-thread route, retaining their repository `cwd`.

In-repository plans can infer the repository from their Git ancestor. Plans in external storage require registration with an existing absolute repository `cwd`, even when they have no authoring thread.

### Index publication

The index is deliberately **opt-in**: a Markdown file appears there only when
its adjacent `<plan>.md.big-plan` marker exists. Existing `<plan>.md.session`
provenance registrations also count as already-promoted plans, so the new rule
does not hide plans that were explicitly handed off before markers existed. This
keeps ordinary unpublished drafts, research notes, and internal handoffs out of
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

### The two delivery routes

`GET /api/submit/<rel>` previews the destination. `POST` with `{prompt,expectMode,expectSessionId}` delivers feedback. The preview's `sessionId` is the T3 thread ID, or null for a new thread. The endpoint returns HTTP 409 with a new preview when either expectation changed. An unavailable T3 server produces an error; it does not silently select another destination.

1. `thread`: a recorded, unarchived T3 thread receives a queued follow-up. Idle threads start immediately; busy threads receive the feedback after their active turn.
2. `new_thread`: absent, invalid, deleted, archived, or non-T3 provenance starts a thread on the registered repository. The prompt includes the plan path, comments sidecar, and instructions for handling feedback.

### Supported T3 transport

Install the `t3-thread` skill from claude-settings at `~/.claude/skills/t3-thread`. New threads use its `t3-thread --project <repo> --prompt - --json` launcher. Existing threads use the same skill's authentication, version check, and WebSocket RPC modules to submit T3's `message.dispatch` command in queue mode. There is no separate delivery transport. Node 22 or newer must be on PATH or installed under `~/.nvm/versions/node`.

The transport mints a short-lived token through the T3 CLI and revokes it on exit. No existing agent credential is copied. Feedback is passed on stdin, never argv or a shell. A durable payload copy is kept under `~/.local/state/big-plan/outbox/`. The payload includes the snapshot baseline step and reply endpoint so the receiving agent can process the feedback without reconstructing the review context.

### Failures reach the HTTP response

The server waits for T3 to acknowledge delivery and checks the feedback run for startup failure over 2.5 seconds. Missing binaries, unreachable servers, rejected commands, malformed transport responses, and startup failures return an error instead of HTTP 200. A timeout has an uncertain outcome; inspect T3 before retrying. A successful response confirms delivery, not completion of the agent's work.

Treat Send as one agent turn per feedback batch.

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

# 5. If Tailscale is connected, derive this node's name and inspect its Serve mapping:
ts_node=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
tailscale serve status --json
# If HTTPS on 443 proxies to 127.0.0.1:8765, verify the review page with a normal GET:
curl -sS -o /dev/null -w "%{http_code}\n" "https://$ts_node/<encoded-path>"
```

Serves `${XDG_STATE_HOME:-$HOME/.local/state}/big-plan/plans` with `--filter plans` on port 8765.

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

`POST /api/submit` starts or queues a T3 agent turn, so anything that can reach port 8765 can trigger compute. Feedback travels on stdin, and the destination comes from the plan's provenance or repository directory. Keep the service on the tailnet.

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
