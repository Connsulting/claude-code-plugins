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

Multi-account providers require a verified activation adapter on every account. A durable
SQLite lease is created only for a matching dispatch claim. Committed `activating` and
`releasing` transition states bracket external PIN changes; an interrupted side effect remains
durable, blocks dispatch, and is reported for reconciliation. Same-account holders share one
active lease, cross-account switches are rejected, ambiguous outcomes retain their lease, and
only the last terminal holder releases. Terminal recording and lease deletion commit only after
a verified release; a failure leaves the `releasing` marker instead of silently unlocking.

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

The generic Bonus-only viewer reads persisted cache and SQLite only. It does not run usage
readers, activation commands, router classification, or refresh subprocesses in an HTTP
request.

The installed service template currently runs the copied original combined jobs viewer to
preserve its UI exactly. Its request handlers also read only disk cache and SQLite, while its
historical background thread refreshes the copied legacy collectors. Unlike the generic
viewer, it retains the original queue activation and force-run POST endpoints and does not
consume the generic viewer's session policy. It must remain bound to `127.0.0.1` and be used
only directly on the host or through tailnet-authenticated Tailscale Serve. It emits no CORS
headers and its UI uses JSON POSTs, but this compatibility surface is not approved for a LAN,
public reverse proxy, or unauthenticated port-forward. The migration to fully config-derived
controls and the generic security policy is tracked in
[VIEWER_FOLLOW_UP.md](VIEWER_FOLLOW_UP.md).

The Bonus-only viewer defaults to:

- bind `127.0.0.1`;
- read-only routes;
- no session or mutation endpoint beyond remote sign-in;
- no CORS headers;
- request bodies capped at 4096 bytes;
- `no-store`, restrictive CSP, no-referrer, nosniff, and frame-deny headers.

Binding a local-mode viewer to a non-loopback address is rejected.

## Explicit external mode

External mode is never inferred from the bind address. It always requires `--remote`, exact
`allowed_hosts[]`, and exact HTTPS `allowed_origins[]`. It has two deliberately separate
security profiles.

### Application-authenticated mode

This mode additionally requires:

- an external `auth_secret_ref` resolved at startup;
- loopback behind Tailscale Serve, or `https_terminated: true` for an intentionally
  non-loopback bind behind a reviewed HTTPS terminator.

The resolved secret is reduced to a one-way verifier and is not retained in config, logs,
or a cookie. Successful sign-in issues a random, bounded-lifetime in-memory session cookie
with `Secure; HttpOnly; SameSite=Strict`. Remote reads require the cookie and exact Host;
an Origin header, when present, must match exactly. Mutations additionally require an exact
Origin and a session-bound `X-CSRF-Token`. There is no wildcard host/origin, suffix match,
origin reflection, or CORS response.

Failed sign-ins are throttled in memory with a bounded five-minute window. Behind a local
HTTPS proxy such as Tailscale Serve, attempts may share the proxy peer address, so repeated
failures can temporarily lock out all remote sign-ins; this is intentionally fail-closed.

Mutations remain absent unless `mutations_enabled` is explicitly true. Enabling them does
not weaken session, CSRF, Host, Origin, HTTPS, body-size, task-ID, or queue-layer checks.

### Trusted loopback proxy mode

Set `trusted_loopback_proxy: true` only when an authenticated local proxy, such as
tailnet-only Tailscale Serve, is the access boundary. This mode deliberately has no Bonus
Drain application password or session. Startup fails unless all of these remain true:

- the viewer binds to a loopback address;
- `mutations_enabled` is false;
- exact Host and HTTPS Origin allowlists are configured;
- `auth_secret_ref` is absent.

The listener therefore remains unavailable from the LAN and public network. Tailscale
authenticates tailnet membership before proxying to loopback. A generic unauthenticated
port-forward is not an acceptable proxy for this mode.

### Tailscale Serve example

For a read-only tailnet viewer, keep `bind` as `127.0.0.1`, leave mutations disabled,
configure `trusted_loopback_proxy: true` plus the exact MagicDNS Host/Origin, and start the
viewer in remote mode. Then front it with HTTPS:

```sh
sudo tailscale serve --bg --https=8766 http://127.0.0.1:8766
```

Open `https://EXACT_NODE_NAME:8766/`; Tailscale Serve must preserve that Host. Use the
application-authenticated profile instead if the proxy is not itself an adequate access
boundary or if viewer mutations are ever enabled. Disable the proxy with:

```sh
sudo tailscale serve --https=8766 off
```

Do not expose the loopback HTTP listener through a generic port-forward, bind `0.0.0.0`
without reviewed TLS termination, or place an auth secret directly in the JSON/unit.

## Installation and removal

Install versions under `~/.local/lib/bonus-drain/<version>` and switch an atomic `current`
symlink. The stable wrapper and systemd templates are hashed in an ownership record.
Install refuses foreign files/symlinks. Uninstall preflights every owned version, wrapper,
and unit; it refuses unknown or changed material and never removes XDG config, cache, queue,
or operator state.
