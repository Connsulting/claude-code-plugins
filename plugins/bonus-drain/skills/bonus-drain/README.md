# Async Work / Bonus Drain runtime

Bonus Drain queues low-priority work and spends only configured provider capacity
that would otherwise expire. It is a Python-standard-library runtime with one stable
`bonus-drain` command. Configuration defines providers and pacing limits. CLIProxyAPI
supplies enabled accounts and their capacity readings.

Every launch opens a T3 thread through a `t3-thread` adapter. `auto` selects a compatible provider
from cached proxy usage before claiming and launching a T3 thread. Missing, malformed, resetless, or
stale subscription data closes its entire provider pool. Leaving work queued is expected.

Set `scout_ntfy_url` in the config to a full ntfy topic URL such as
`https://ntfy.sh/your-topic` to receive scout health notices. Omit it or set it to
`null` to disable notifications. The scout sends a notice on a nonzero result or a
`reconciliation_required` blocker, repeats at most once per 24 hours while stuck,
and sends one recovery notice when the next completed tick is healthy. Normal
closed capacity gates and zero dispatch ticks do not alert. Dry runs and ticks
skipped because another scout holds the lock leave notification state untouched.

Notices contain blocker kinds, task IDs, and `bonus-drain doctor --json`, without
error text or credentials. Deduplication lives in the queue SQLite database.
Each notice is reserved before its HTTP request, so failed deliveries consume
the daily reservation. Notification storage or delivery failures do not change
the scout result or dispatch decisions. When the queue database itself rejects the
reservation (for example `database or disk is full`), the scout sends one
`storage_unwritable` stuck notice carrying SQLite's error text, deduplicated by a marker
file in `$XDG_RUNTIME_DIR` (else `/run/user/<uid>`); the first tick that can write the
database again removes the marker and sends a recovery notice. The URL accepts HTTP or
HTTPS and must not contain credentials, query parameters, or a fragment.

Scout reserves concrete compatible task IDs in nearest-reset order before dispatch. A task
can occupy only one batch per tick, so a portable task consumed by an earlier reset cannot
also consume a later provider slot, while exclusive work remains available to a provider
with the required capability. Legacy-exclusive tasks drain before portable tasks regardless
of provider batch order; normal queue priority remains authoritative within each class.

Subscriptions come from CLIProxyAPI's enabled auth files. A provider has one gate and at
most one batch. Every subscription must have fresh, valid readings for every configured
limit; otherwise the provider pool closes because the proxy may serve any subscription.
For each metered limit, compute signed `surplus = ceiling_percent - used_percent - reserve`
for every subscription, including negative values. A positive mean opens the pool and
`min(batch_size, floor(sum(surplus) / estimated_percent_per_job))` determines its job budget.
Multiple limits restrict the tightest budget. The earliest valid reset sets the provider's
`<provider>/<limit>/<cycle>` eligibility key.

Manual and automatic dispatch record the provider ID in the historical `account_id` field.
CLIProxyAPI owns credential selection. Unexpired historical subscription backoffs hold the
whole provider, and a proven launch clears all of that provider's backoffs. Existing workers
can record terminal receipts against their stored claims even when their account IDs are no
longer in inventory. Manual starts have no account-selection argument.

Async Work runs all week: `lead_seconds` equals the weekly window, and reserve is
`max_percent_per_window` points per `pacing_window_seconds` still left, capped at the ceiling.
The planner subtracts already-running jobs once per provider. `batch_size` is the per-provider
cap (3 in the example). Optional `max_jobs` caps total in-flight plus new work. The final
`urgency_seconds` (24 hours) uses the same reserve rule. Grok and Muse are unmetered pools
with at most `unmetered_max_inflight` concurrent runs each, defaulting to 2, and hourly keys.
Recurring task cooldowns and weekly cycle deduplication still apply.

Each provider requires `max_size` on the ordered scale `tiny`, `small`, `medium`, `large`,
`huge`. The example ceilings are Grok `small`, Muse `medium`, Codex `large`, and Claude `huge`.
Planning, explicit or automatic dispatch, and atomic claiming enforce that ceiling. A task
without a size is conservatively `huge`. Oversized pinned tasks may be queued but remain held
with a `provider_size` reason. Every launch opens a T3 Code thread; task MCP scopes do not
restrict these threads.

## Requirements

1. Python 3.10 or newer with the standard `sqlite3` module.
1. An executable `t3-thread` configured as the dispatch adapter.
1. CLIProxyAPI management access through a private dotenv file containing `CPA_BASE_URL`
  and `CPA_MGMT_KEY`. Configure its file secret ref with `proxy_secret_ref`.
1. systemd user services only if the ten-minute scout timer, slot-freed path trigger, and ten-minute refresh timer are wanted.
1. Tailnet-only Tailscale Serve if the viewer is used remotely. Tailscale is the viewer's only
  access boundary; Bonus Drain adds no password or identity session.

There is no runtime dependency on `uv`, `bg-schedule`, `codex-bg-thread`, a provider CLI,
or a shell evaluator. Usage acquisition uses the Python standard library and the proxy management API.

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

1. `recurrence_timezone` is the IANA timezone used for weekly boundaries. The default and
  example use `America/New_York`: weeks start Saturday at midnight, and Saturday through Sunday is the
  automatic dispatch window.
1. `adapters[]` declares argv arrays for `t3-thread`.
1. `providers[]` binds a provider ID to the thread launcher adapter, T3 provider name, and required `max_size`;
  The proxy inventory supplies enabled account identities.
1. `limits[]` binds pacing thresholds to `provider_id`. Weekly windows use `seven_day` and
  five-hour windows use `five_hour` in the normalized cache.
1. `proxy_secret_ref` names the dotenv file secret ref; `unmetered_max_inflight` caps each
  unmetered provider pool. Accounts and plans are not configured.
1. `secret_refs[]` names externally injected values. Credential-shaped inline fields,
  shell interpolation, dangling references, duplicate IDs, and direct-provider dispatch
  are rejected.
1. `pr_exceptions[]` grants ordinary task repository-specific push/PR behavior.
  An explicitly authorized Long Horizon goal with `merge_policy: merge` additionally
  grants its registered, unchanged coordinator permission to merge verified PRs within
  the recorded authority. Task drivers retain their own restrictions; goal membership
  or task prose alone does not grant that exception. See [GOALS.md](GOALS.md).
1. `viewer` binds to loopback. Remote access requires a secretless trusted loopback proxy,
  exact Host/HTTPS Origin values, and an explicit mutations flag. See
  [SECURITY.md](SECURITY.md).

The installed viewer preserves the existing `background jobs` frontend unchanged, including
the Bonus Drain and scheduled tabs. Its Run now buttons delegate to the shared T3 thread kickoff service.
There is no application-auth configuration or login flow.

After installation, expand the operator home and replace every illustrative
`/ABSOLUTE/PATH/TO` prefix in `config.example.json`. Adapter executables should use the
stable `.../.local/lib/bonus-drain/current` path, never a marketplace cache path. The
`t3-thread` and proxy dotenv paths are host-owned dependencies and must also be absolute.

The example configures Claude and Codex weekly pacing, plus unmetered Grok and Muse pools.
Refresh obtains enabled accounts from `GET /v0/management/auth-files` and reads Claude or
Codex windows through `POST /v0/management/api-call`. Only the two provider usage URLs
specified in `proxy_usage.py` are allowed. Disabled accounts are removed from inventory.
Unknown configured providers are unmetered and receive no usage request.

Only Claude may run migrated `claude_only`, recognized legacy-exclusive model tasks, or
work requiring `legacy-exclusive`; its configuration must declare that capability. Only
Claude and Codex may run work invoking the implement skill or requiring `implement`, and
must declare it. Declaring either protected capability on another provider does not grant
permission. Explicit `allowed_providers` and other `required_capabilities` still apply.
When open capacity cannot run implement work, scout reports a `provider_capability` blocker.

CLI `auto` chooses a compatible provider with the soonest reset among open pool gates.
A manual auto-start may use a compatible provider with no subscriptions, after open pools.
A listed subscription with unavailable usage or a malformed inventory cannot use this
fallback. Scout always requires an open pool gate. After a concrete launch has been attempted,
uncertainty retains the claim until reconciliation; no caller retries automatically.

Normalized usage always includes explicit `provider_id`, `account_id`, and `captured_at`.
Claude snapshots keep `five_hour`, `seven_day`, and available per-model `seven_day_*`
windows. Codex snapshots keep the primary five-hour and secondary weekly windows; an
explicit window duration determines the mapping when supplied. Missing or invalid metered
usage closes the whole provider pool. Per-account request failures are cached as unavailable without
storing response bodies or credential data.

File-backed secret references must be current-user-owned, non-symlink regular files with
mode `0600` and size at most 65536 bytes. `doctor` validates those properties without printing
the value.

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

Reinstalling changed source under an existing version is refused. Choose a new install version.
Doctor compares the installed payload with its recorded source checkout while that checkout
exists. If the source checkout is gone, this drift check is unavailable; installation ownership
and payload checks still run.

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
systemctl --user enable --now bonus-drain-refresh.timer bonus-drain-scout.timer bonus-drain-scout.path
systemctl --user list-timers 'bonus-drain-*'
```

The refresher is the only component that calls the proxy management reader. Scout, `gates`,
`plan`, and viewer requests read normalized cache and SQLite only. The viewer's gated,
nearest-reset drain summary represents this tick's paced scout allocation. Its remaining
queue is the complete eligible provider/capability inventory, so stale or closed cache gates
do not hide concrete Run now targets. Run now is manual and bypasses pacing only; it still checks
active state, compatibility, atomic claims, and the configured `t3-thread` path.

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
JSON-only mutations, body limits, and T3 thread delegation. A disposable Chrome test loads
the frozen frontend through the real handler and proves the controls render without a login;
it does not exercise systemd, product TLS, or Tailscale.

Proxy integration should use a local HTTP fixture and a disposable DB/cache.
Proof must include a failed or stale sibling closing the whole pool, signed mean and summed
budget calculations, one atomic claim under a race, and no adapter call from a viewer or
scout request path.

The normalized usage cache contract is:

```json
{"provider_id":"provider-a","account_id":"account-a","captured_at":1800000000,"limits":{"seven_day":{"used_percent":42.5,"resets_at":1800007200}}}
```

The refresher stores only normalized account identity, capture time and window values.
`plan --json` and `gates --json` include a `subscriptions` array containing the same original
readings and per-limit `pacing` fields (`ceiling_percent`, `used_percent`, `reserve`, `surplus`,
`resets_at`) so readers can check the pool calculation independently. The viewer keeps each
Claude and Codex subscription separate and shows Grok and Muse as unmetered pools.
`/api/bonus` reports those subscription records in `usage`.
The source and installed wrappers start Python in isolated mode and insert only their owned
runtime path. Requests have bounded response sizes and timeouts, and management redirects
are refused so credentials cannot follow a redirect.

## Migration and removal

There are two separate operations:

1. `bonus-drain import-legacy --backlog FILE --runs FILE` imports the old markdown/jsonl
  queue format into a selected DB. The deprecated `migrate.py` delegates only to this
  importer and never controls units.
1. `bonus-drain migrate --from-db FILE --from-units DIR --destination DIR --dry-run`
  inspects an old DB/unit installation and emits a manual cutover report. Code refuses
  apply and rollback.

Read [MIGRATION.md](MIGRATION.md) for the required stop/mask, writer proof, backup,
manual apply, viewer-profile inventory, verification, and rollback sequence. Do not run old
and new writers against one queue.

To remove runtime-owned files while preserving config, DB, cache, and operator state:

```sh
systemctl --user disable --now \
  bonus-drain-scout.path bonus-drain-scout.timer bonus-drain-refresh.timer bonus-drain-viewer.service
./uninstall.sh
```

Uninstall verifies hashes and refuses if an installed version, wrapper, or unit has been
changed or contains an unknown file. Interpreter-generated `__pycache__` bytecode is
recognized only when its cache-tagged name maps to a manifest-owned Python source file;
unknown source, executable, configuration, and arbitrary files still fail ownership.
Move operator additions elsewhere and retry; do not delete the state directories as part
of runtime removal.

## Async work and dependencies

Every ready task is eligible for Bonus capacity; `run-now` accelerates an individual task.
Tasks can carry source references, 15-character work groups, and one-off prerequisites.
Work groups are display and navigation labels and do not affect scheduling or limit concurrency.
See [ASYNC_WORK.md](ASYNC_WORK.md) for the contract and commands.
Repository tasks may also set `start_ref` with `--start-ref` on add or through `start_ref` in
edit changes. The viewer's read only `--local` queue and gates view shows the selected start
branch and uses the same readiness result as dispatch. It has no pending verification state.

Every claimed launch receives a unique attempt ID. The prompt's exact record command binds the
terminal event to that attempt and reads structured outcome evidence from its protected state
path. `done` requires reason code `done_when_verified`, `completion.verified: true`, one of
`command`, `artifact`, `operator_receipt`, or `goal_acceptance`, and nonempty evidence that proves
done-when. Normal PR work is done only after all checks pass for the current head. Epic Forge
work also requires a confirmed merge into the exact authorized epic branch. Opening a PR,
pending or failing checks, and an unmerged epic PR are not completion. Use `artifact` completion
with evidence of the PR, passing checks, and any required merge. Pending human review alone does
not block normal completion once checks pass. `awaiting_human` is retired for new records; historical rows stay readable. An external blocker records `failed` with `authority_required` and a `resume_when` check list.
Attempts remain visible after failure, skip, ambiguity, or a proved-not-launched abort, so a late
worker cannot close or release a newer attempt.

Automatic recovery applies only to failed or skipped one-off prerequisites with active dependent
work. It allows at most two recovery attempts, after 5-minute and 30-minute backoffs, and stops
early when the normalized failure reason repeats. Unknown launch state, missing authority, and
unavailable or malformed repository identity stay held. Divergent parent refs require an explicit
child start_ref. Ordinary requeue schedules an operator
recovery without deleting history and leaves retained holds held. `requeue <task> --override-hold --reason <text>` is the one explicit escape: it converts a held authority-required, permanent, or no-progress operator recovery to scheduled now, and records "operator hold override" plus the reason in the recovery detail. It still refuses unknown-launch sources, tasks with dispatch claims, goal-managed tasks or any recovery admission rejection, verified done work, non-one-off tasks, changed contracts, and recoveries that are not held (exit 1). Automatic scout recovery never overrides a hold. Goal-managed public requeue remains rejected; the goal runtime
may admit implementation or integration recovery under its existing guards, while coordinator and
frozen acceptance failures require fresh follow-up jobs.

Scout reads T3 state through read-only SQLite before the in-flight gate. Active or unsettled
parent runs and recursive subagent descendants with an active run or nonterminal latest run
keep their ancestor running. A terminal parent run stays running for 900 seconds after it
ends; an older terminal run with no queue terminal record closes as an unverified failure.
Missing or unreadable state stays held.

`recover-complete` can consume an unknown-launch reconciliation hold only when the source
attempt's signature is `unknown_launch:missing_terminal_record` or `unknown_launch:t3_run_*`,
the source T3 thread is readable and has no active or unsettled run, and no recursive subagent
descendant is live. It uses the existing verified outcome requirements and appends a
verification attempt without a new launch. Other unknown-launch signatures, missing T3
state, and unrelated holds remain refused. `requeue --override-hold` still refuses every
unknown-launch source.

A repository producing task records `remote`, `target_ref`, `branch_ref`, and `integration_state`.
Those fields establish the handoff identity. Commit OIDs and merge receipts may remain as audit
evidence, but dependency readiness does not require them. `reverify-handoff` records a structural
correction for the same remote, target_ref, and branch_ref. It preserves the original done row
and accepts only the exact done row and revision supplied by the caller. It does not run Git or
require fresh repository proof.

Use optional `--start-ref` on add or `start_ref` in edit to choose the branch where repository
work begins. Branch shorthand such as `task/base` and full refs such as `refs/heads/task/base`
are normalized to `refs/heads/task/base`. An explicit child start_ref wins only when repository
parent metadata is valid. It cannot override missing or malformed remote, target_ref, branch_ref,
or integration_state fields. When start_ref is unset, parents with the same remote and target
select their unique unmerged branch; if all are merged, they select the shared target. Different
remotes or targets, or multiple unmerged branches, require an explicit child start_ref. Queue, scout, and dispatch use saved metadata only, with no Git
calls, remote checks, or immutable SHA pin. The worker fetches the selected branch's current tip
during setup. If it is missing, setup stops with reason code `verification_needed` and does not
fall back to the target. No recovery or handoff grants merge, push, deployment, or other external
authority.

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
(default 4.0), `load_per_cpu_max` (default 1.5, used only when `/proc/pressure/cpu` is
unavailable), and `disk_free_min_gib` (default 30.0, free space available to unprivileged
users on the filesystem holding the queue database). When nothing is readable, no gating happens. A gated tick still reconciles,
launches nothing, and reports a `host_pressure` blocker.
