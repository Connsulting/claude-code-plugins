# Bonus Drain security model

## Trust boundaries

Bonus Drain accepts configuration and low-priority task content, reads usage through
bounded adapters, writes normalized cache and SQLite state, and asks `agent-router` to
launch a concrete provider. It does not execute a provider directly.

Config rejects inline credential-shaped values, shell interpolation, relative or unsafe
adapter executables, duplicate/dangling graph IDs, traversal, and dispatch adapters other
than `agent-router`. Adapter subprocesses use argv arrays with `shell=False`, an allowlisted
environment, time and output caps, and redacted diagnostics. Cache replacement is atomic,
mode `0600`, and only a coherent normalized record replaces the last good value.

The account-snapshot adapter reads exactly one configured file and validates explicit
provider, account, source-label, source-window, destination-limit, and freshness values. The
Grok normalizer invokes only one absolute collector with the literal `--direct-collect` flag,
never the generic usage command, so configured collection cannot recurse.

The optional activation adapter accepts no credentials. PIN and active-label paths must be
absolute and may not traverse symlinks. Activation writes a mode-0600 pin atomically, invokes
an optional rotator with `shell=False` and a minimal environment, verifies the requested label
when configured, and restores/removes its pin on failure. Release removes only a pin whose
current contents still match that adapter's literal label. Account identity mismatches and
successful-but-unswitched rotations fail closed.

Activation and release serialize on a private per-PIN lock. Label reads use no-follow file
descriptors and accept only bounded regular files. Collector and rotator subprocesses start
in isolated process groups; timeout kills the group, including descendants. Activation never
receives unrelated account secret references.

Launch-scoped activation is permitted only by explicit account configuration. It releases a
pin after both the router job identity and dispatched row are durable while retaining the task
claim. Run scope remains the default. A failed post-launch release leaves a committed
`releasing` lease and ambiguous claim for reconciliation rather than pretending the pin moved.

Multi-account providers require a verified activation adapter on every account. A durable
SQLite lease is created only for a matching dispatch claim. Committed `activating` and
`releasing` transition states bracket external PIN changes; an interrupted side effect remains
durable, blocks dispatch, and is reported for reconciliation. Same-account holders share one
active lease, cross-account switches are rejected, ambiguous outcomes retain their lease, and
only the last holder releases. Run-scoped holders release at terminal; launch-scoped holders
release after a proven dispatch. Lease deletion commits only after a verified release; a failure
leaves the `releasing` marker instead of silently unlocking.

Normalized usage responses must explicitly state provider ID, account ID, and capture time.
Direct Grok collection is configuration-limited to one account identity, and an omitted usage
percentage is unknown capacity. File secret references are opened without following symlinks
and must be current-user-owned mode-0600 regular files no larger than 65536 bytes; diagnostics
report only validity, never content.

Claims are made transactionally before routing. A known launch failure releases the claim;
a terminal record closes it; requeue removes the matching terminal rows and claim in one
transaction. An ambiguous router response or timeout keeps both the claim and runtime account
lease closed to prevent a duplicate or account switch. `doctor` reports the reconciliation
requirement.

## Viewer defaults

Bonus Drain preserves the established two-tab background-jobs viewer. Request threads read
persisted usage snapshots and SQLite, and inspect local systemd timer metadata; they never call
a provider API, activation command, or router for a GET. The separate
`bonus-drain-refresh.timer` owns bounded usage refresh. Force is always an explicit button
action. Concrete and `auto` Force both delegate to the shared router-only kickoff service.

The viewer defaults to:

- bind `127.0.0.1`;
- no application authentication, identity session, or cookie;
- mutations disabled unless explicitly enabled;
- no CORS headers;
- request bodies capped at 4096 bytes;
- `no-store`, same-origin referrer, nosniff, and frame-deny headers.

Binding a local-mode viewer to a non-loopback address is rejected.

The installed unit runs the established `services/jobs-viewer/server.py` on loopback port
8766, with `BONUS_DRAIN_CONFIG` for the JSON graph and `Wants=`/`After=` the refresh timer.
That ordering does not make an absent cache fresh; gates remain fail-closed.

## Tailnet-only external mode

The viewer has one remote profile. `trusted_loopback_proxy: true` means tailnet-only Tailscale
Serve is the access boundary. Bonus Drain deliberately has no application password, login
route, authentication cookie, identity session, or viewer secret.

Startup fails unless all of these remain true:

- the viewer binds to a loopback address;
- exact Host and HTTPS Origin allowlists are configured;
- the trusted loopback proxy flag is true;
- configuration contains no application-auth field.

Every tailnet member that can reach the exact viewer URL is therefore an operator. This is
the intended trust model, not a second authentication layer.

Mutations remain absent unless `mutations_enabled` is explicitly true. When enabled,
enable/disable and Force require exact Host, exact HTTPS Origin, JSON body limits, safe task
and provider IDs. Exact Origin plus JSON-only request handling prevents a public page from
submitting a simple cross-site browser request without altering the established frontend.
Force then uses the shared router-only kickoff path and still enforces active state,
compatibility, and atomic claims.

### Tailscale Serve example

Keep `bind` as `127.0.0.1`, configure `trusted_loopback_proxy: true` plus the exact MagicDNS
Host/Origin, explicitly choose whether controls are enabled, and front the viewer with HTTPS:

```sh
sudo tailscale serve --bg --https=8766 http://127.0.0.1:8766
```

Open `https://EXACT_NODE_NAME:8766/`; Tailscale Serve must preserve that Host. Set
`mutations_enabled: true` for enable/disable and Force. No login step is required. Disable the
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
