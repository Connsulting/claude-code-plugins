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

Claims are made transactionally before routing. A known launch failure releases the claim;
a terminal record closes it; requeue removes the matching terminal rows and claim in one
transaction. An ambiguous router response keeps the claim closed to prevent a duplicate but
immediately releases account activation. `doctor` reports the reconciliation requirement.

## Viewer defaults

The Bonus-only viewer and combined jobs viewer read persisted cache and SQLite only. They do
not run usage readers, activation commands, router classification, or refresh subprocesses
in an HTTP request.

The Bonus-only viewer defaults to:

- bind `127.0.0.1`;
- read-only routes;
- no session or mutation endpoint beyond remote sign-in;
- no CORS headers;
- request bodies capped at 4096 bytes;
- `no-store`, restrictive CSP, no-referrer, nosniff, and frame-deny headers.

Binding a local-mode viewer to a non-loopback address is rejected.

## Explicit remote mode

Remote mode is never inferred from the bind address. It requires all of:

- `--remote` (or the equivalent explicit service configuration);
- an external `auth_secret_ref` resolved at startup;
- exact `allowed_hosts[]` values, including the port when the HTTP Host includes it;
- exact HTTPS `allowed_origins[]` values;
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

### Tailscale Serve example

Keep `bind` as `127.0.0.1`, configure the exact MagicDNS Host/Origin and external auth
reference, and start the viewer in remote mode. Then front it with HTTPS:

```sh
sudo tailscale serve --bg --https=8766 http://127.0.0.1:8766
```

Open `https://EXACT_NODE_NAME:8766/login`; the credential is submitted only to the exact
configured HTTPS origin. Tailscale Serve must preserve that Host. Disable the proxy with:

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
