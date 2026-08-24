---
name: big-plan
description: Author, publish, and revise substantial Markdown plans in a repository's .projects directory using the local Big Plan review server. Use when the user asks for a big plan, commentable plan, phone review, decision cards, or Big Plan feedback round-trip.
---

# Big Plan

Use Big Plan for substantial plans that benefit from structured review, anchored comments, task toggles, comparisons, or explicit decisions. The server is persistent; this skill authors and publishes documents for it.

## Author and publish

1. Write the plan under `<repo>/.projects/<descriptive-name>.md`. Start from `template.md` when useful.
2. Keep headings stable after publication. Heading slugs and block text anchor feedback; renaming a commented heading or rewriting a commented block can orphan its anchor.
3. Use ordinary Markdown plus the supported affordances documented in `README.md`: task checkboxes, `decide`, `decide-multi`, `.compare` grids, Mermaid fences, and GitHub-style callouts.
4. Determine the server root from `BIG_PLAN_ROOT`, defaulting to `$HOME/git`, and calculate the plan path relative to that root.
5. Before any POST, verify `http://127.0.0.1:${BIG_PLAN_PORT:-8765}/` responds. If the staged unit is inactive, start it with `systemctl --user start big-plan.service`, then verify the endpoint again. Do not POST to an unavailable service.
6. Register provenance with `POST /api/session/<relative-path>` when the current engine and session ID are available. Registration also creates `<plan>.md.big-plan`, making the plan visible in the index. If there is intentionally no session provenance, publish it with `POST /api/promote/<relative-path>` instead.
7. Return the direct plan URL, not only the index URL.

For Claude, send `{engine:"claude", sessionId:$CLAUDE_CODE_SESSION_ID, name, cwd}`. For Grok, use `{engine:"grok", sessionId:$GROK_SESSION_ID, name, cwd}`. For Codex, use the current rollout UUID as `sessionId` and `engine:"codex"`. Never invent a session ID; promote without provenance when it is unavailable.

The local API base defaults to `http://127.0.0.1:${BIG_PLAN_PORT:-8765}`. URL-encode the relative path before using it in an API or browser URL.

## Derive the review URL

Never hardcode a machine or tailnet hostname. The launcher uses a bounded five-second status probe and selects `0.0.0.0` only when `BackendState` is `Running`, `Self.DNSName` is nonempty, and `Self.TailscaleIPs` contains an IPv4 address; otherwise it binds to `127.0.0.1`. The healthy-path bind preserves localhost callbacks, direct tailnet access, and an existing Tailscale Serve proxy. Because `0.0.0.0` listens on every interface, it is suitable only on a trusted or firewalled host. Derive the node name at handoff time. The direct URL is `http://<Self.DNSName-without-trailing-dot>:${BIG_PLAN_PORT:-8765}/<relative-path>`. Use HTTPS only when the operator separately configured Tailscale Serve; otherwise use direct HTTP. When Tailscale is unavailable, use `http://127.0.0.1:${BIG_PLAN_PORT:-8765}/<relative-path>`.

## Revise from feedback

Before editing a reviewed plan, create its baseline with `POST /api/snapshot/<relative-path>` or copy the plan to `<plan>.md.snapshot`. Then:

1. Read `<plan>.md.comments.json` and triage every unresolved entry.
2. Edit the plan for actionable feedback, preserving stable headings when possible.
3. Reply to questions with `POST /api/comments/<relative-path>/<comment-id>/reply`; do not resolve a question merely because it was answered.
4. Leave reviewer-owned decisions open.
5. Give the reviewer the same dynamically derived plan URL. The UI highlights changes against the snapshot, and the reviewer can accept individual hunks or clear the snapshot.

The Send button may dispatch a full agent turn through the session sidecar. Treat it as one send per feedback batch.
