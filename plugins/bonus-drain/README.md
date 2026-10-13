# Async Work / Bonus Drain

Bonus Drain is a self-contained queue, planner, scout, usage-cache refresher, dispatcher,
and viewer for autonomous async work, with optional opportunistic Bonus scheduling. Provider
size ceilings, pacing limits, reset windows, and dispatch bindings are validated JSON data.
CLIProxyAPI supplies subscription inventory and owns credential selection. The planner does not contain provider-name branches.

Installing this marketplace plugin only makes its skill available. It does not switch a
live Bonus Drain installation, enable a systemd unit, migrate a database, change a thread launcher,
or configure Tailscale Serve. Runtime installation updates an existing queue schema and
stages files without enabling or starting services. Service activation remains an explicit
operator action.

## Dependencies

Required runtime dependencies are:

1. Python 3.10 or newer with the standard-library `sqlite3` module.
2. An executable `t3-thread`, referenced by absolute argv in configuration.
3. CLIProxyAPI management access through a private dotenv file containing `CPA_BASE_URL`
   and `CPA_MGMT_KEY`, referenced by `proxy_secret_ref`.

The core runtime has no third-party Python package, provider usage adapter, or credential
activation adapter. The proxy owns subscription routing.
systemd user services are optional. For the explicitly configured remote viewer, tailnet-only
Tailscale Serve is the sole access boundary, including for explicitly enabled mutations.

Run `bonus-drain doctor --json` after configuring the runtime. It validates the graph,
queue, T3 thread dispatch boundary, reconciliation state, and file-secret ownership/mode
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
disposable staging test. `./install.sh --install-version VERSION` selects a publication
version and may be combined with `--home DIR` in either order.

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

The routable provider pools are Claude, Codex, Grok, and Muse. Each provider declares
`max_size` from `tiny`, `small`, `medium`, `large`, or `huge`:

```json
{
  "providers": [
    {"id": "claude", "dispatch": {"adapter_id": "router", "provider": "claude"}, "max_size": "huge", "capabilities": ["legacy-exclusive", "implement"]},
    {"id": "codex", "dispatch": {"adapter_id": "router", "provider": "codex"}, "max_size": "large", "capabilities": ["implement"]},
    {"id": "grok", "dispatch": {"adapter_id": "router", "provider": "grok"}, "max_size": "small"},
    {"id": "muse", "dispatch": {"adapter_id": "router", "provider": "muse"}, "max_size": "medium"}
  ]
}
```

Retain the required adapters, limits, record command, viewer, and schema version from
`config.example.json`. Do not configure subscription accounts or plans. The proxy cache
provides those observations, and each provider gets one gate and at most one batch.
Every enabled subscription must have fresh, valid readings. A missing, malformed,
resetless, or stale reading closes its entire provider pool because the proxy can serve
any of its subscriptions. The refresher owns provider reads; scout, planner, and viewer
consume cache without calling provider APIs on page load.

For each configured metered limit, calculate every subscription's signed surplus as
`ceiling_percent - used_percent - reserve`. A positive mean opens the pool. The budget is
`min(batch_size, floor(sum(surplus) / estimated_percent_per_job))`; negative surpluses
remain in that sum. Multiple limits use the tightest budget and the earliest reset for the
provider's eligibility key. Grok and Muse use `unmetered_max_inflight` and an hourly cycle.
Task size must fit the provider ceiling at planning, dispatch, and atomic claim. A task
without a size is treated as `huge`; an oversized pinned task remains queued with a reason.

The JSON graph owns the configured runtime database and cache. `BONUS_DB` is deprecated
queue-only compatibility for graph-free legacy commands; it cannot retarget configured
viewer, refresh, scout, planner, or dispatch state. A graph command rejects a differing
`--database` value.

Replace every `/ABSOLUTE/PATH/TO` prefix in the example with an absolute host path.
The example has one weekly pacing limit for each of Claude and Codex, each with a 95 percent
ceiling. Proxy vendor spellings `xai` and `grok` map to the Grok pool; `meta` maps to Muse.

The reserved `legacy-exclusive` capability applies only to Claude, including migrated
`claude_only` and recognized legacy-exclusive model rows. The reserved `implement`
capability applies only to Claude and Codex. Protected tasks still require the respective
capability declaration; a Grok or Muse declaration cannot override the identity rule.
Explicit provider allowlists and other required capabilities continue to apply.

Dispatch records the provider ID in the historical `account_id` field. Historical claims
and terminal receipts retain their stored account identities. Subscription selection and
activation are owned by the proxy; `dispatch` and `run-now` have no `--account` option.
Unexpired historical subscription backoffs hold the entire provider pool, and a proven
launch clears its provider's backoffs.

CLI `auto` selects a compatible provider using configured size ceilings and usage before
claiming the task. Once a launch has been attempted, timeout, malformed output, missing
thread identity, or bookkeeping uncertainty is ambiguous: no caller retries it, and the
claim stays held until reconciliation. Exit 6 from `t3-thread` proves no thread was created
and permits a bounded retry.

Every launch calls `t3-thread` with the task project, provider engine, prompt file, title,
and optional model. Its top-level `threadId` is stored verbatim, and the run records its
surface as `t3`. Task MCP flags are omitted; the T3 provider uses its configured connectors.

Scout reserves actual compatible task IDs in nearest-reset order, preventing a portable task
from consuming two provider slots while preserving exclusive work for a capable provider.
Within one priority, eligible tasks are oldest-waiting-first. Weekly tasks are automatically
eligible only on the weekend (Saturday or Sunday) in the configured recurrence timezone, and at
most once in the week that began Saturday at midnight, so a Saturday run is not repeated Sunday.
A manual weekday run consumes that week's slot. If the weekend passes without a run, the task
waits for the next weekend instead of catching up Monday. Outside the weekend, a due weekly
task reports `cooldown` ("Waiting for weekend window") rather than ready, so the viewer matches
the scout; a manual start may still launch it.
Monthly tasks start waiting when their 28-day cooldown ends. Before any claim, scout reports
queue reconciliation blockers, global in-flight runs with their ages, and the resolved executable
identity for every thread launcher needed by the tick. A lifecycle or thread launcher failure stops the tick once
instead of failing each selected task.
Before applying the in-flight gate, each normal scout tick reads T3 state through read-only
SQLite. An active parent run or any recursive subagent descendant with an active run or
nonterminal latest run keeps the task running. A terminal parent run that ended less than
900 seconds ago also stays running. Once the thread settles and that grace period ends,
a missing queue terminal event is recorded as `failed` through the record CLI's lifecycle
path. This releases its claim without asserting task
completion or retrying it. Missing or unreadable T3 state remains held; age alone never
proves that a worker ended. Scout JSON includes the decisions in `reconciliation`,
and `--dry-run` reports `would_fail` without changing the queue.
Existing ambiguous claims still stop the scout before any automatic reconciliation.
Normalized usage requires explicit provider/account identity and capture time. Missing Grok
utilization stays unknown. Source and installed wrappers ignore caller `PYTHONPATH`/cwd
packages; timed-out adapter process groups are terminated with descendants.

File-backed secret references must be current-user-owned, non-symlink regular files with mode
`0600` and a maximum size of 65536 bytes.

Secrets are references only. Declare an environment-variable or file reference in
`secret_refs` and map it by ID from an adapter. Never place a secret value in the
JSON, an argv, a unit, a log, or the repository. Every launch goes through an explicit
`t3-thread` adapter. CLIProxyAPI owns subscription selection.

## Viewer

Bonus Drain ships the established two-tab background-jobs viewer: the Async Work queue
and scheduled systemd jobs. Its HTML and interactions are preserved from the standalone
viewer while Run now delegates to the shared T3 thread kickoff service.

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

Run now is never automatic and bypasses pacing only. It still requires an active compatible
task and an atomic claim. Concrete-provider and `auto` buttons both use the shared
`kick_task` service; `auto` selects a compatible provider before the one launch.

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
mutations, body limits, and T3 thread delegation. These do not prove systemd, product TLS,
or Tailscale. For a credential-free smoke test, set `HOME`
and every XDG directory to a new temporary directory, install with `--home`, use fixture
adapters, run `doctor`, `plan`, and the loopback viewer, then
uninstall from that same temporary home.

## Database backup

Quiesce and mask every writer before copying a SQLite database. Preserve the DB plus any
`-wal` and `-shm` files, both old and new redacted configs, unit files, enabled/masked-state
inventories, and installed-version/current-link inventory in a timestamped protected
directory. Hash the backup, make it read-only, open the copied DB in SQLite read-only mode,
and record `PRAGMA integrity_check`, schema version, table names, and task/run counts. Do
not treat an unread backup as a rollback point.

## Rollback

Disable, stop, and mask the new scout path unit (`bonus-drain-scout.path`), scout and refresh
timers, and their services, and stop the viewer. Prove no new
writer holds the DB files. Preserve the failed installation's DB and logs, restore the verified
DB/config/unit backup with recorded modes and prior unit state, run the old read-only
checks, then unmask and start only the previously active writers. Prove that exactly one
writer topology is active before resuming work.

Runtime uninstall is not rollback: it preserves XDG configuration, cache, queue, and
operator state and refuses modified or unknown owned files.

## Later removal

After rollback or final retirement has been separately completed and verified:

```sh
systemctl --user disable --now \
  bonus-drain-scout.path bonus-drain-scout.timer bonus-drain-refresh.timer bonus-drain-viewer.service
cd skills/bonus-drain
./uninstall.sh
```

The uninstaller removes only hash-verified runtime-owned versions, wrappers, and units. It
does not remove configuration, cache, database, backups, or other operator state. Remove
the Claude or Codex marketplace plugin separately only after no runtime process depends on
the marketplace cache path.

For the runtime contracts and command reference, see
[`skills/bonus-drain/README.md`](skills/bonus-drain/README.md) and
[`skills/bonus-drain/SECURITY.md`](skills/bonus-drain/SECURITY.md).

## Async work and dependencies

Every ready task is eligible for Bonus capacity; `run-now` accelerates an individual task.
Tasks can carry source references, 15-character work groups, and one-off prerequisites.
Work groups are display and navigation labels and do not affect scheduling or limit concurrency.
See [ASYNC_WORK.md](skills/bonus-drain/ASYNC_WORK.md) for the contract and commands.

A task is queueable only if it can start and can finish with no human in the loop. `add` requires
the readiness record of the global `implementable-ticket` skill (contract
`implementable-ticket/v1`) as `--readiness-review` and refuses a task whose record answers no to
"can it start?" or "can it finish?", or whose acceptance criteria lack a `verified_by`. An
authority finding resolves only by a grant Brian already gave or a rewritten done-when. `add`
evaluates every launch check before storing the task: a failing check refuses unless a queued
prerequisite is expected to make it pass, and an unverifiable check refuses with a request to
retry. Tasks queued under the earlier review shape (`review_stale`) or with no review
(`review_missing`) are held until reviewed.

Each launch has an immutable attempt identity. The dispatched prompt provides the exact
terminal command and private outcome-evidence path for that attempt. A `done` result must
use reason code `done_when_verified` and demonstrate the stored done-when condition through
`command`, `artifact`, `operator_receipt`, or `goal_acceptance` evidence. A PR or branch by
itself is supporting evidence, not completion. Failed, skipped, ambiguous, and aborted attempts
stay in history.

When a failed or skipped one-off blocks active dependent work, the scout may make at most two
automatic recovery attempts after 5-minute and 30-minute backoffs. Repeating the same failure
reason stops recovery early. Missing authority, unknown launch ownership, and unavailable
repository identity remain held. Divergent parent refs need an explicit child start_ref. An
ordinary operator requeue preserves prior attempts and leaves those holds held; `requeue <task>
--override-hold --reason <text>` explicitly reschedules an authority-required, permanent, or
no-progress hold, and still refuses unknown launches, claimed, goal-managed, done, non-one-off,
and changed-contract work.
`recover-complete` can accept verified completion for an unknown-launch reconciliation
closure only with signature `unknown_launch:missing_terminal_record` or
`unknown_launch:t3_run_*`. Its source attempt must identify a readable T3 thread with no
active or unsettled parent run and no live subagent descendant. It consumes only that
closure's matching recovery hold and preserves prior attempts. Other unknown launches
remain held, including with `requeue --override-hold`.
Public requeue remains unavailable for goal-managed work; admitted implementation and integration
recovery stays under the goal contract, while coordinators and frozen acceptance use fresh
follow-up jobs.

Repository handoff readiness uses the recorded remote, target_ref, branch_ref, and
integration_state. Commit OIDs and receipts may remain as audit evidence, but readiness does not
require Git ancestry or content proof. A child may set `start_ref` with `--start-ref`; edits use
the `start_ref` field. Branch shorthands are normalized to `refs/heads/...`. An explicit child
start_ref resolves valid parent branches that differ, but it cannot override missing or malformed
repository identity metadata. Without an override, parents with the same remote and target select
their unique unmerged branch, or the shared target when all are merged. Different remotes or
targets, and multiple unmerged branches, require the child to set start_ref. Queue, scout, and
dispatch resolve from saved metadata without Git calls, remote checks, or an immutable SHA pin.
The worker fetches the selected branch's current tip. A missing branch stops setup with reason
code `verification_needed`; the worker does not fall back to the target. This branch choice grants
no merge, push, deployment, or other external authority.

The bundled [Long Horizon skill](skills/long-horizon/SKILL.md) coordinates whole goals
through those jobs, stacked or authorized merged PRs, combined acceptance, and further
fix rounds. Durable joins leave the coordinator idle between short queue jobs. See
[GOALS.md](skills/bonus-drain/GOALS.md) for goal contracts, commands, bounds, and recovery.
Existing groups are linked by explicit task IDs without rewriting their contracts.

## Scout cadence and host load gate

The scout runs from two triggers. `bonus-drain-scout.timer` fires every ten minutes (30 seconds
of randomized delay). `bonus-drain-scout.path` fires an extra tick when a job frees its slot:
whenever an attempt reaches a terminal state, Bonus Drain writes a `slot-freed` marker next to
the queue DB, and the path unit watches the default XDG state location
`~/.local/state/bonus-drain/slot-freed`. A custom `database` location needs a matching drop-in
that overrides `PathChanged=`. The path unit allows 60 triggers per ten minutes and is the only start limit on scout starts (the
service disables its own with `StartLimitIntervalSec=0`); if that burst limit is hit, systemd stops
watching until you run
`systemctl --user reset-failed bonus-drain-scout.path bonus-drain-scout.service && systemctl --user start bonus-drain-scout.path`.
The ten-minute timer keeps covering in the meantime.

A tick holds `scout.lock` in the state dir. A concurrent tick exits 0 and reports
`skipped: tick_in_progress`.

The optional `host_load_gate` config block gates launches on host pressure: `enabled`,
`cpu_some_avg60_max` (default 40.0, from `/proc/pressure/cpu`), `mem_available_min_gib`
(default 4.0), and `load_per_cpu_max` (default 1.5, used only when `/proc/pressure/cpu` is
unavailable). When nothing is readable, no gating happens. A gated tick still reconciles,
launches nothing, and reports a `host_pressure` blocker.
