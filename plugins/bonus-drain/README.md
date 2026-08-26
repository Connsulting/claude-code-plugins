# Bonus Drain

Bonus Drain is a self-contained queue, planner, scout, usage-cache refresher, dispatcher,
and viewer for opportunistic low-priority work. Provider names, plans, accounts,
limits, reset windows, usage readers, activation rules, and dispatch bindings are validated
JSON data. The planner does not contain provider-name branches.

Installing this marketplace plugin only makes its skill available. It does not switch a
live Bonus Drain installation, enable a systemd unit, migrate a database, change a router,
or configure Tailscale Serve. Runtime installation also stages files without enabling or
starting services. Cutover remains an explicit manual operation.

## Dependencies

Required runtime dependencies are:

- Python 3.10 or newer with the standard-library `sqlite3` module;
- an executable `agent-router`, referenced by an absolute argv in the JSON configuration;
- one executable usage adapter per account, reporting every configured limit and reset in one
  coherent normalized response.

The core runtime has no third-party Python package, `uv`, `bg-schedule`, or
`codex-bg-thread` dependency. The included account-snapshot, Grok-normalization, and
activation adapters use only Python's standard library. The direct Grok collector uses
Bash, `jq`, `curl`, and GNU utilities as documented in its header; replace any provider-edge
adapter with an executable that implements the normalized JSON contract.
`ai-token-rotator` is optional and can be configured as an account activation adapter.
systemd user services are optional. For the explicitly configured remote viewer, tailnet-only
Tailscale Serve is the sole access boundary, including for explicitly enabled mutations.

Run `bonus-drain doctor --json` after configuring the runtime. It validates the graph,
queue, router-only dispatch boundary, reconciliation state, and file-secret ownership/mode
without printing secret values.

## Install or stage

Claude Code marketplace installation:

```text
/plugin marketplace add Connsulting/claude-code-plugins
/plugin install bonus-drain@connsulting-plugins
```

For a local Codex checkout, register this non-default repository marketplace and then add
the plugin:

```sh
codex plugin marketplace add /absolute/path/to/claude-code-plugins
codex plugin add bonus-drain@connsulting-plugins
```

To stage the stable runtime without activation, run from this plugin directory:

```sh
cd skills/bonus-drain
./install.sh
"${HOME}/.local/bin/bonus-drain" status --json
```

The helper copies a versioned runtime under `${HOME}/.local/lib/bonus-drain`, creates a
stable `${HOME}/.local/bin/bonus-drain` wrapper, and installs owned unit templates. It does
not enable or start a service or timer. Use `./install.sh --home /temporary/home` for a
disposable staging test.

## Generic configuration

Copy the complete valid skeleton and replace every example executable with an absolute
path on the target machine:

```sh
install -d -m 0700 "${XDG_CONFIG_HOME:-$HOME/.config}/bonus-drain"
install -m 0600 skills/bonus-drain/config.example.json \
  "${XDG_CONFIG_HOME:-$HOME/.config}/bonus-drain/config.json"
export BONUS_DRAIN_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/bonus-drain/config.json"
bonus-drain doctor --json
```

Provider and account labels are arbitrary data. For example, a configuration can define
personal and business accounts across Claude, Codex, and Grok without changing core code:

```json
{
  "providers": [
    {"id": "claude", "dispatch": {"adapter_id": "router", "provider": "claude"}, "capabilities": ["legacy-exclusive"]},
    {"id": "codex", "dispatch": {"adapter_id": "router", "provider": "codex"}},
    {"id": "grok", "dispatch": {"adapter_id": "router", "provider": "grok"}}
  ],
  "plans": [
    {"id": "claude-personal-plan", "provider_id": "claude"},
    {"id": "claude-business-plan", "provider_id": "claude"},
    {"id": "codex-personal-plan", "provider_id": "codex"},
    {"id": "grok-personal-plan", "provider_id": "grok"}
  ],
  "accounts": [
    {"id": "claude-personal", "provider_id": "claude", "plan_id": "claude-personal-plan", "usage_adapter_id": "usage", "activation_adapter_id": "claude-personal-activator"},
    {"id": "claude-business", "provider_id": "claude", "plan_id": "claude-business-plan", "usage_adapter_id": "usage", "activation_adapter_id": "account-activator"},
    {"id": "codex-personal", "provider_id": "codex", "plan_id": "codex-personal-plan", "usage_adapter_id": "usage", "activation_adapter_id": "codex-personal-activator"},
    {"id": "grok-personal", "provider_id": "grok", "plan_id": "grok-personal-plan", "usage_adapter_id": "usage"}
  ]
}
```

This is an illustrative fragment; retain the required adapters, limits, record command,
viewer, and schema version from `config.example.json`. Give each plan its own limits and
reset data. Bonus Drain keeps those windows independent and dispatches the eligible batch
whose reset is nearest. Missing, malformed, resetless, or stale usage closes only the
affected account. The refresher owns provider reads and writes normalized cache; scout,
planner, and viewer consume cache and do not call provider APIs on page load.

The JSON graph owns the configured runtime database and cache. `BONUS_DB` is deprecated
queue-only compatibility for graph-free legacy commands; it cannot retarget configured
viewer, refresh, scout, planner, or dispatch state. A graph command rejects a differing
`--database` value.

The shipped example uses the stable installed `.../.local/lib/bonus-drain/current` path and
explicit provider, account, source-label, snapshot, and limit IDs. Replace every
`/ABSOLUTE/PATH/TO` prefix with an absolute path for the target host. The snapshot adapter
reads one account file and can map multiple windows in one call. The Grok adapter invokes
the collector's dedicated direct mode so generic configuration cannot recurse. The optional
activation adapter atomically pins a literal account label, sanitizes the rotator environment,
verifies a configured active-label file, rolls back failed switches, and releases only its
matching pin.

The reserved `legacy-exclusive` capability preserves migrated `claude_only` and recognized
legacy-exclusive model rows without hard-coding a provider name. Declare it only on providers
that can run those tasks. Explicit provider allowlists and required capabilities remain
independent.

Every account of a multi-account provider must use the shipped verified activation form with
a literal expected account, PIN path, and active-label proof. Claims hold durable SQLite
activation leases: same-account launches share one switch, cross-account launches wait,
ambiguous outcomes retain both claim and lease, and only the last terminal record can release
the lease after verified external release. A failed or interrupted release remains durable and
requires explicit reconciliation; it must never be treated as immediately released.

CLI `auto` classification is a non-launching `agent-router --dry-run` before a claim, so its
uncertainty is retry-safe. Once a concrete launch has been attempted, timeout, malformed
output, missing job identity, or bookkeeping uncertainty is ambiguous: no caller retries it,
and the claim and applicable activation lease stay fail-closed until reconciliation.

Scout reserves actual compatible task IDs in nearest-reset order, preventing a portable task
from consuming two provider slots while preserving exclusive work for a capable provider.
Normalized usage requires explicit provider/account identity and capture time. Missing Grok
utilization stays unknown. Source and installed wrappers ignore caller `PYTHONPATH`/cwd
packages; timed-out collector and rotator process groups are terminated with descendants.

File-backed secret references must be current-user-owned, non-symlink regular files with mode
`0600` and a maximum size of 65536 bytes. Activation does not inherit unrelated account usage
secret bindings.

Secrets are references only. Declare an environment-variable or file reference in
`secret_refs` and map it by ID from an adapter. Never place a secret value in the
JSON, an argv, a unit, a log, or the repository. Every launch goes through an explicit
`agent-router` adapter. Multi-account activation is optional; `ai-token-rotator` is one
possible activation adapter, not a core dependency.

## Viewer

Bonus Drain ships the established two-tab background-jobs viewer: the Bonus Drain console
and scheduled systemd jobs. Its HTML and interactions are preserved from the standalone
viewer while Force dispatch now delegates to the shared router-only kickoff service.

```sh
BONUS_DRAIN_CONFIG="${BONUS_DRAIN_CONFIG}" bonus-drain viewer --port 8766
```

The installed `bonus-drain-viewer.service` binds the viewer to loopback on port 8766 and has
`Wants=` and `After=` dependencies on `bonus-drain-refresh.timer`:

```sh
BONUS_DRAIN_CONFIG="${BONUS_DRAIN_CONFIG}" bonus-drain viewer --remote --port 8766
```

The dependency ensures refresh is scheduled before the viewer, not that cache is already
fresh. Page rendering reads cached usage, SQLite, and local systemd timer metadata. It does
not call provider APIs from a request.

Force is never automatic and bypasses pacing only. It still requires an active compatible
task and an atomic claim. Concrete-provider and `auto` buttons both use the shared
`kick_task` service; `auto` asks `agent-router` to classify before the one launch.

### Tailscale access boundary

The viewer has no application authentication, password, login route, session, or viewer
secret. The shipped profile binds only to loopback and trusts tailnet-only Tailscale Serve as
the access boundary. Every tailnet member able to reach the exact viewer URL is an operator.

Remote configuration requires `trusted_loopback_proxy: true`, exact `allowed_hosts`, exact
HTTPS `allowed_origins`, and explicit `mutations_enabled: true` for controls. The backend
accepts mutations only from the exact same-origin page using JSON, without changing the
existing frontend or creating a cookie or identity session.

Do not expose the loopback listener through a generic port-forward or public proxy. See
[`skills/bonus-drain/SECURITY.md`](skills/bonus-drain/SECURITY.md) for the exact profile
requirements.

## Test and diagnose

From the marketplace repository root:

```sh
python3 -m unittest tests.test_bonus_drain_package
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/bonus-drain
claude plugin validate plugins/bonus-drain --strict
grok plugin validate plugins/bonus-drain
```

From a source checkout that contains the regression suites, follow
`skills/bonus-drain/README.md` for the full Python, shell, distribution, byte-compile, and
systemd validation commands. Verify the unit separately with exact unit text and
`systemd-analyze verify`. The disposable browser E2E loads the frozen frontend through the
real HTTP handler; HTTP contracts separately prove exact Host/HTTPS Origin, JSON-only
mutations, body limits, and router-only delegation. These do not prove systemd, product TLS,
or Tailscale. For a credential-free smoke test, set `HOME`
and every XDG directory to a new temporary directory, install with `--home`, use fixture
adapters, run `doctor`, `plan`, migration `--dry-run`, and the loopback viewer, then
uninstall from that same temporary home.

## Manual cutover

Read `skills/bonus-drain/MIGRATION.md` completely before touching an existing installation.
The safe order is:

1. inventory the old DB, writer services, timers, viewer profile/HTTPS proxy mapping, stable
   paths, and unit state;
2. run `bonus-drain migrate --from-db FILE --from-units DIR --destination DIR --dry-run`;
3. stop and mask every old writer, then prove no process holds the DB, WAL, or SHM open;
4. after quiescence, re-count the queue so late-added jobs are included, then create and read
   back the authoritative backup before selecting the new DB;
5. install and validate the new config, run `doctor`, `refresh`, `gates`, and `plan`;
6. enable only the refresh timer, observe it, then enable the scout timer; enable the remote
   viewer only after its tailnet-only trusted-proxy profile has been reviewed;
7. keep the old writers masked through at least one observed scheduling cycle.

Never run old and new writers against one queue. `migrate --dry-run` is report-only and
refuses apply or rollback. Markdown/jsonl import is a separate `import-legacy` operation.

## Database backup

Quiesce and mask every writer before copying a SQLite database. Preserve the DB plus any
`-wal` and `-shm` files, both old and new redacted configs, unit files, enabled/masked-state
inventories, and installed-version/current-link inventory in a timestamped protected
directory. Hash the backup, make it read-only, open the copied DB in SQLite read-only mode,
and record `PRAGMA integrity_check`, schema version, table names, and task/run counts. Do
not treat an unread backup as a rollback point.

## Rollback

Disable, stop, and mask the new scout and refresh timers and stop the viewer. Prove no new
writer holds the DB files. Preserve the failed-cutover DB and logs, restore the verified
DB/config/unit backup with recorded modes and prior unit state, run the old read-only
checks, then unmask and start only the previously active writers. Prove that exactly one
writer topology is active before resuming work.

Runtime uninstall is not rollback: it preserves XDG configuration, cache, queue, and
operator state and refuses modified or unknown owned files.

## Later removal

After rollback or final retirement has been separately completed and verified:

```sh
systemctl --user disable --now \
  bonus-drain-scout.timer bonus-drain-refresh.timer bonus-drain-viewer.service
cd skills/bonus-drain
./uninstall.sh
```

The uninstaller removes only hash-verified runtime-owned versions, wrappers, and units. It
does not remove configuration, cache, database, backups, or other operator state. Remove
the Claude or Codex marketplace plugin separately only after no runtime process depends on
the marketplace cache path.

For the runtime contracts and command reference, see
[`skills/bonus-drain/README.md`](skills/bonus-drain/README.md),
[`skills/bonus-drain/SECURITY.md`](skills/bonus-drain/SECURITY.md), and
[`skills/bonus-drain/MIGRATION.md`](skills/bonus-drain/MIGRATION.md).
