# Bonus Drain security model

## Trust boundaries

Bonus Drain accepts configuration and autonomous task content, reads usage through
the bounded proxy management reader, writes normalized cache and SQLite state, and asks `t3-thread` to
launch a concrete provider. It does not execute a provider directly.

Config rejects inline credential-shaped values, shell interpolation, relative or unsafe
adapter executables, duplicate/dangling graph IDs, traversal, and dispatch adapters other
than `t3-thread`. Adapter subprocesses use argv arrays with `shell=False`, an allowlisted
environment, time and output caps, and redacted diagnostics. Cache replacement is atomic,
mode `0600`, and only a coherent normalized record replaces the last good value.

The proxy reader resolves one configured private dotenv file and sends its management
credential only to the configured CLIProxyAPI origin. Redirects are refused. Account inventory
is sanitized before caching; tokens, credential fields, Authorization headers and raw response
bodies never enter logs or cache. Usage requests permit exactly the Anthropic OAuth usage URL
and the ChatGPT WHAM usage URL, with the proxy's literal token placeholder. Other providers
are unmetered and receive no usage request.

File secret references are opened without following symlinks and must be current-user-owned
mode-0600 regular files no larger than 65536 bytes. Diagnostics report validity, never content.

Claims are made transactionally before routing and carry an immutable attempt ID. A terminal
record may close only the exact matching attempt, claim ownership; a late or
mismatched attempt cannot release a successor. A proved-not-launched failure retains an `aborted`
attempt and releases only its matching ownership. An ambiguous thread launcher response or timeout retains
the exact attempt and claim to prevent a duplicate launch.
Ordinary requeue schedules recovery without deleting terminal rows or attempts, and managed goal
tasks remain protected by their public requeue guard. `doctor` reports reconciliation requirements.
Authority-required, permanent, unknown-launch, and no-progress recovery holds cannot trigger a
silent retry. Further work requires a fresh, explicit follow-up task under reviewed policy and
authority; the retained hold is not converted through the existing edit path.

Structured outcome files live beneath the configured private state directory and are accepted
only as current-user-owned, non-symlink, mode-0600 bounded JSON objects. New `done` outcomes require
reason code `done_when_verified`, `completion.verified: true`, a mechanism of `command`, `artifact`,
`operator_receipt`, or `goal_acceptance`, and nonempty bounded evidence. Repository and PR fields
cannot grant completion or external authority. Same-thread completion uses the exact stable package
CLI command embedded in the original prompt, creates no claim or thread launch, and compare-and-swap
refuses changed contracts, ambiguous ownership, and active or queued successors. It may append the
verified attempt without a preexisting recovery projection; when one exists, only the exact match
is consumed.

`requeue --now`, `dispatch --now`, and `scout --now` are trusted operator inputs. Worker-facing
`record` and `recover-complete` take their receipt and admission clocks from the service, so worker
timestamps remain historical evidence and cannot shorten recovery backoff or bypass admission.

## Viewer defaults

Bonus Drain preserves the established two-tab background-jobs viewer. Request threads read
persisted usage snapshots and SQLite, and inspect local systemd timer metadata; they never call
a provider API or thread launcher for a GET. The separate
`bonus-drain-refresh.timer` owns bounded usage refresh. Run now is always an explicit button
action. Concrete and `auto` Run now both delegate to the shared T3 thread kickoff service.

The viewer defaults to:

1. bind `127.0.0.1`;
1. no application authentication, identity session, or cookie;
1. mutations disabled unless explicitly enabled;
1. no CORS headers;
1. request bodies capped at 4096 bytes;
1. `no-store`, same-origin referrer, nosniff, and frame-deny headers.

Binding a local-mode viewer to a non-loopback address is rejected.

The installed unit runs the established `services/jobs-viewer/server.py` on loopback port
8766, with `BONUS_DRAIN_CONFIG` for the JSON graph and `Wants=`/`After=` the refresh timer.
That ordering does not make an absent cache fresh; gates remain fail-closed.

## Tailnet-only external mode

The viewer has one remote profile. `trusted_loopback_proxy: true` means tailnet-only Tailscale
Serve is the access boundary. Bonus Drain deliberately has no application password, login
route, authentication cookie, identity session, or viewer secret.

Startup fails unless all of these remain true:

1. the viewer binds to a loopback address;
1. exact Host and HTTPS Origin allowlists are configured;
1. the trusted loopback proxy flag is true;
1. configuration contains no application-auth field.

Every tailnet member that can reach the exact viewer URL is therefore an operator. This is
the intended trust model, not a second authentication layer.

Mutations remain absent unless `mutations_enabled` is explicitly true. When enabled,
enable/disable and Run now require exact Host, exact HTTPS Origin, JSON body limits, safe task
and provider IDs. Exact Origin plus JSON-only request handling prevents a public page from
submitting a simple cross-site browser request without altering the established frontend.
Run now then uses the shared T3 thread kickoff path and still enforces active state,
compatibility, and atomic claims.

### Tailscale Serve example

Keep `bind` as `127.0.0.1`, configure `trusted_loopback_proxy: true` plus the exact MagicDNS
Host/Origin, explicitly choose whether controls are enabled, and front the viewer with HTTPS:

```sh
sudo tailscale serve --bg --https=8766 http://127.0.0.1:8766
```

Open `https://EXACT_NODE_NAME:8766/`; Tailscale Serve must preserve that Host. Set
`mutations_enabled: true` for enable/disable and Run now. No login step is required. Disable the
proxy with:

```sh
sudo tailscale serve --https=8766 off
```

Do not expose the loopback HTTP listener through a generic port-forward or bind `0.0.0.0`.

## Installation and removal

Install versions under `~/.local/lib/bonus-drain/<version>` and switch an atomic `current`
symlink. The stable wrapper and systemd templates are hashed in an ownership record.
Install refuses foreign files/symlinks. Uninstall preflights every owned version, wrapper,
and unit; it refuses unknown or changed material and never removes XDG config, cache, queue,
or operator state.

## Async work preview and dependencies

The viewer provides monitoring and execution controls, with no contract editor or HTTP edit
endpoint. Request bodies remain capped at 4 KiB. Planning threads edit contracts through the
CLI. Dependency changes are validated in the same write transaction as the contract update.
Claims recheck dependencies, automatic execution policy, and the selected contract before
routing.

`viewer.preview: true` displays a copied-queue banner and rejects dispatch both in the viewer
and the shared dispatcher. Preview setup additionally replaces all external adapters with
rejecting executables and retargets database, cache, and proxy secret references to a private copy.
Requeueing there changes only that copy. No runtime installation or automatic scheduler is
implied by running a preview.
