# Bonus Drain runtime

Bonus Drain queues low-priority work and spends only configured provider capacity
that would otherwise expire. It is a Python-standard-library runtime with one stable
`bonus-drain` command. Providers, accounts, plans, limits, usage readers, and optional
account activators are data in the validated JSON config; the planner contains no
provider-name branches.

Every launch goes through an `agent-router` adapter. `auto` is classification only:
the router is queried with a dry run, Bonus Drain validates the result, then claims and
launches once with a concrete provider and account. Missing, malformed, resetless, or
stale cache data closes only the affected account. Leaving work queued is expected.

Scout reserves concrete compatible task IDs in nearest-reset order before dispatch. A task
can occupy only one batch per tick, so a portable task consumed by an earlier reset cannot
also consume a later provider slot, while exclusive work remains available to a provider
with the required capability. Legacy-exclusive tasks drain before portable tasks regardless
of provider batch order; normal queue priority remains authoritative within each class.

Manual dispatch without an explicit account reuses an account already leased by the selected
provider; if no lease exists, it selects the single account whose configured active-account
marker matches. This permits compatible concurrent launches without switching credentials
beneath running jobs. If a multi-account provider has neither an active marker nor a lease,
manual dispatch fails closed and requires `--account ACCOUNT_ID`. An explicit account hint
remains authoritative and fails closed on a conflict.

Account activation defaults to `activation_scope: "run"`, which holds the selected credential
through the terminal event. Providers that safely reread switched credentials between calls may
instead configure every account in that provider with `activation_scope: "launch"`. That scope
releases the external pin only after the router job identity and dispatched row are durable, so
an independent token rotator can apply its own five-hour threshold while the job continues. The
recorded account is the launch account; later handoffs belong to the rotator's decision log.

Bonus Drain has no five-hour pacing reserve. Once an account enters its configured drain
window, it can launch up to its batch cap until it reaches the weekly ceiling.

## Requirements

- Python 3.10 or newer with the standard `sqlite3` module.
- An executable `agent-router` configured as the dispatch adapter.
- One executable usage adapter for each account. The shipped stdlib-only
  `bonus-drain-account-usage` reads one explicitly named account snapshot and can map one or
  more source windows to configured limit IDs. `bonus-drain-grok-usage` invokes the shipped
  Grok collector in direct-collection mode and normalizes one configured weekly limit.
- The optional Grok collector requires Bash, `jq`, `curl`, and GNU `date`/`tac`. The historical
  shell wrappers remain compatibility assets, but configured calls require explicit provider
  and account IDs; positional account selection is not supported by the standalone runtime.
- Optional activation adapters for installations that switch among accounts.
- `ai-token-rotator` is not required; when used, configure it as an activation adapter and
  let `doctor` report whether its executable is present.
- systemd user services only if hourly scout and ten-minute refresh timers are wanted.
- Tailnet-only Tailscale Serve if the viewer is used remotely. Tailscale is the viewer's only
  access boundary; Bonus Drain adds no password or identity session.

There is no runtime dependency on `uv`, `bg-schedule`, `codex-bg-thread`, a provider CLI,
or a shell evaluator. Provider-specific readers may have their own documented external
dependencies, but they are adapters rather than Bonus Drain internals.

## Paths

The defaults follow XDG and can be changed by their standard XDG environment variables:

| Purpose | Default |
| --- | --- |
| validated config | `~/.config/bonus-drain/config.json` |
| SQLite queue | `~/.local/state/bonus-drain/queue.db` |
| normalized usage cache | `~/.cache/bonus-drain/` |
| versions | `~/.local/lib/bonus-drain/<version>/` |
| active version | `~/.local/lib/bonus-drain/current` |
| stable command | `~/.local/bin/bonus-drain` |

The JSON graph owns a configured runtime's database and cache. `BONUS_DB` (and `BONUSDB`) is
deprecated queue-only compatibility for graph-free legacy commands; it cannot retarget the
viewer, refresher, scout, planner, or dispatch graph. A graph command rejects a divergent
`--database` value.

## Configuration

Start from `config.example.json` and validate before installing services:

```sh
install -d -m 0700 "${XDG_CONFIG_HOME:-$HOME/.config}/bonus-drain"
install -m 0600 config.example.json \
  "${XDG_CONFIG_HOME:-$HOME/.config}/bonus-drain/config.json"
BONUS_DRAIN_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/bonus-drain/config.json" \
  bonus-drain doctor --json
```

The configuration graph is versioned and uses IDs for all relationships:

- `adapters[]` declares argv arrays for `agent-router`, usage, optional separate reset,
  and optional activation adapters.
- `providers[]` binds a provider ID to the router adapter and its router-facing name;
  `account_mode: single` explicitly constrains providers backed by one global identity.
- `plans[]`, `accounts[]`, and `limits[]` describe independent capacity windows.
- `secret_refs[]` names externally injected values. Credential-shaped inline fields,
  shell interpolation, dangling references, duplicate IDs, and direct-provider dispatch
  are rejected.
- `pr_exceptions[]` is the only place to grant repository-specific push behavior.
- `viewer` binds to loopback. Remote access requires a secretless trusted loopback proxy,
  exact Host/HTTPS Origin values, and an explicit mutations flag. See
  [SECURITY.md](SECURITY.md).

The installed viewer preserves the existing `background jobs` frontend unchanged, including
the Bonus Drain and scheduled tabs. Its Force buttons delegate to the shared router-only kickoff service.
There is no application-auth configuration or login flow.

After installation, expand the operator home and replace every illustrative
`/ABSOLUTE/PATH/TO` prefix in `config.example.json`. Adapter executables should use the
stable `.../.local/lib/bonus-drain/current` path, never a marketplace cache path. The
`agent-router` and account-store paths are host-owned dependencies and must also be absolute.

The example shows four plans across three provider labels, account snapshots for personal
and business plans, direct Grok collection, and optional rotator-backed activation. These
labels are examples only. Add one usage adapter per account so its snapshot path, source
label, source-window mapping, provider ID, account ID, and limit IDs are all literal or safe
`{provider_id}`/`{account_id}` substitutions. The snapshot adapter requires a fresh
`captured_at`, validates any embedded provider/account/source label, and emits all mapped
limits atomically; omitted, null, stale, or mismatched data fails that account closed.

Providers that are allowed to run migrated `claude_only` tasks or legacy-exclusive model
rows must explicitly declare the reserved `legacy-exclusive` capability. The name is a
compatibility semantic, not a provider identity: any configured provider may declare it.
Explicit `allowed_providers` and `required_capabilities` continue to apply independently.

The optional `bonus-drain-account-activation` adapter takes a literal expected account ID,
rotator label, absolute PIN path, optional rotator executable, and optional active-label
file. It atomically writes a mode-0600 pin, runs the rotator without a shell or inherited
credential environment, verifies the active label when configured, rolls back a failed
switch, and releases only its own matching label.

Every account of a provider with more than one configured account must use the shipped,
verified activation adapter form with a literal expected account and one shared PIN,
active-label, rotator domain, and activation scope. Dispatch claims acquire durable SQLite activation leases.
Committed `activating` and `releasing` states bracket external PIN changes. Concurrent
launches on one account share the proven activation; a different account is blocked until
every run-scoped holder is terminal or every launch-scoped holder has a proven dispatch. Ambiguous
router outcomes retain both claim and lease. The last holder commits a `releasing` marker before
removing the PIN and commits the lease deletion afterward; an interruption leaves durable
reconciliation evidence. An ambiguous outcome never relinquishes its activation lease immediately.

CLI `auto` is a non-launching, pre-claim `agent-router --dry-run`; its uncertainty is
retry-safe. After a concrete router launch has been attempted, any uncertainty is
post-launch ambiguity: the claim and activation lease remain fail-closed until explicit
reconciliation and no caller retries automatically.

Normalized usage always includes explicit `provider_id`, `account_id`, and `captured_at`.
The shipped direct Grok collector is bound to one configured account adapter. If Grok omits
its utilization percentage, capacity stays unknown and the account closes; omission is never
converted to zero usage.

File-backed secret references must be current-user-owned, non-symlink regular files with
mode `0600` and size at most 65536 bytes. `doctor` validates those properties without printing
the value. Activation adapters do not inherit account usage-secret bindings.

Do not put a credential value in JSON, a unit file, an argv, or a repository. Supply the
named external secret to the service environment or credential manager at runtime. Config
validation resolves references, not secret values.

## Install and operate

From the skill directory, install a version and the five self-owned unit templates:

```sh
./install.sh
~/.local/bin/bonus-drain status --json
~/.local/bin/bonus-drain doctor --json
```

Installation does not enable or start anything. After placing the validated config and
injecting its external secrets, initialize and perform one cache refresh manually:

```sh
bonus-drain init
bonus-drain refresh --json
bonus-drain gates --json
bonus-drain plan --json
```

Only after those commands report the intended DB, accounts, and closed/open gates should
an operator enable scheduling:

```sh
systemctl --user daemon-reload
systemctl --user enable --now bonus-drain-refresh.timer bonus-drain-scout.timer
systemctl --user list-timers 'bonus-drain-*'
```

The refresher is the only component that runs configured usage adapters. Scout, `gates`,
`plan`, and viewer requests read normalized cache and SQLite only. The viewer's gated,
nearest-reset drain summary represents this tick's paced scout allocation. Its remaining
queue is the complete eligible provider/capability inventory, so stale or closed cache gates
do not hide concrete Force targets. Force is manual and bypasses pacing only; it still checks
active state, compatibility, atomic claims, and the configured `agent-router` path.

To run the remote service after its profile is reviewed:

```sh
systemctl --user enable --now bonus-drain-viewer.service
```

The service waits for the refresh timer to be scheduled, not for a fresh cache. Missing or
stale cache must still close the automated drain summary.

Useful read-only checks:

```sh
bonus-drain status --json
bonus-drain doctor --json
bonus-drain queue --json
bonus-drain runs --json
journalctl --user -u bonus-drain-refresh.service -u bonus-drain-scout.service --since today
```

## Verification from a source checkout

These are the complete source checks. Run them in a disposable HOME/XDG environment when
validating lifecycle behavior; none requires provider credentials:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tests/verify_distribution.py \
  --manifest distribution-manifest.json --source .
for test_file in test-*.sh; do bash "$test_file"; done
python3 -m compileall -q bonus_drain
BONUS_DRAIN_CONFIG=config.example.json bin/bonus-drain doctor --json
systemd-analyze verify systemd/*.service systemd/*.timer
```

Treat static unit verification, HTTP boundary verification, and browser verification as
separate evidence. Exact unit text plus `systemd-analyze verify` proves the direct viewer
server command and refresh-timer ordering. HTTP contracts prove exact Host/HTTPS Origin,
JSON-only mutations, body limits, and router-only delegation. A disposable Chrome test loads
the frozen frontend through the real handler and proves the controls render without a login;
it does not exercise systemd, product TLS, or Tailscale.

Provider adapter integration should use fixture executables and a disposable DB/cache.
Proof must include a failed sibling refresh retaining its last good cache, a stale account
closing independently, one atomic claim under a race, and no adapter call from a viewer or
scout request path.

The normalized usage adapter contract is:

```json
{"provider_id":"provider-a","account_id":"account-a","captured_at":1800000000,"limits":{"plan-a-weekly":{"used_percent":42.5,"resets_at":1800007200}}}
```

Every limit configured for the account plan must be present in one coherent response. The
refresh service is the only intended caller; viewer requests never execute these adapters.

The source and installed wrappers start Python in isolated mode, insert only their owned
runtime path, and ignore caller `PYTHONPATH` and cwd packages. Collector and rotator timeouts
terminate their isolated process groups so descendants cannot survive a failed refresh or
activation.

## Migration and removal

There are two separate operations:

- `bonus-drain import-legacy --backlog FILE --runs FILE` imports the old markdown/jsonl
  queue format into a selected DB. The deprecated `migrate.py` delegates only to this
  importer and never controls units.
- `bonus-drain migrate --from-db FILE --from-units DIR --destination DIR --dry-run`
  inspects an old DB/unit installation and emits a manual cutover report. Code refuses
  apply and rollback.

Read [MIGRATION.md](MIGRATION.md) for the required stop/mask, writer proof, backup,
manual apply, viewer-profile inventory, verification, and rollback sequence. Do not run old
and new writers against one queue.

To remove runtime-owned files while preserving config, DB, cache, and operator state:

```sh
systemctl --user disable --now \
  bonus-drain-scout.timer bonus-drain-refresh.timer bonus-drain-viewer.service
./uninstall.sh
```

Uninstall verifies hashes and refuses if an installed version, wrapper, or unit has been
changed or contains an unknown file. Interpreter-generated `__pycache__` bytecode is
recognized only when its cache-tagged name maps to a manifest-owned Python source file;
unknown source, executable, configuration, and arbitrary files still fail ownership.
Move operator additions elsewhere and retry; do not delete the state directories as part
of runtime removal.
