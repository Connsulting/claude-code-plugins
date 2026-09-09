---
name: bonus-drain
description: Queue, edit, and execute autonomous async work planned in threads. Use for "queue this", "park this for later", "add to my work queue", "run this queued task", and bonus-capacity work. Ready work is automatically eligible when Bonus capacity is available; an explicit start accelerates one task. Dependencies gate every launch; all execution uses agent-router. Use bg-schedule for exact calendar timing.
---

# Async Work / Bonus Drain

Async Work is the queue for autonomous work handed off from planning threads. Bonus Drain
is its opportunistic automatic scheduling policy. Every ready task in the queue is eligible
when spare capacity is available. Queueing a task does not authorize an immediate launch;
an explicit start accelerates that one task without changing its contract.

Automatic Bonus scheduling may leave work queued when usage is unknown, stale, ahead of pace,
at the remaining-headroom floor, or already in flight. Never reinterpret a closed gate as spare
capacity. An explicit start bypasses pacing while preserving dependency and execution checks.

Use the installed command at `${BONUS_DRAIN_BIN:-$HOME/.local/bin/bonus-drain}`. Config,
state, and cache follow XDG. Source-tree shell files are compatibility wrappers only; do not
source private helper functions or invent a second DB path.

## Invariants

1. Provider, plan, account, limit, usage, activation, and dispatch behavior comes from the
   validated JSON graph. Do not branch on a familiar provider name.
2. The refresher alone calls usage adapters. Scout, plan, viewers, and consumers read cache.
3. Missing, stale, malformed, or resetless cache closes only that account. No data is not
   zero usage.
4. Every accounted launch goes through the configured `agent-router` adapter. Direct provider
   launch and `codex-bg-thread` are forbidden.
5. `auto` classifies in a router dry run only. Validate the result, claim the task, and launch
   once with a concrete provider/account. Never persist `auto` or a null provider.
6. Classifier uncertainty is pre-claim and retry-safe because `agent-router --dry-run` is
   non-launching. Claim `(task_id, eligibility_key)` before concrete activation/routing.
   Known-not-launched failure releases it; a post-launch ambiguity holds the claim and any
   activation lease fail-closed for reconciliation. Never claim activation releases
   immediately after ambiguity.
   A configured launch-scoped activation releases only after a concrete job identity and its
   dispatched record exist; run-scoped activation remains held through the terminal event.
7. Use the resolved executable record command embedded in the dispatched prompt. It must
   point to the stable CLI and the JSON graph's database. `BONUS_DB` is deprecated queue-only
   compatibility and cannot retarget the configured graph.
8. No publish, merge, credential change, production mutation, contract/schema/ADR change, or
   other externally consequential action is implied by being bonus work. The task contract
   must grant it explicitly.

## Before any mode

Run read-only checks through the stable CLI:

```sh
bonus-drain doctor --json
bonus-drain status --json
```

Stop if config validation fails, the stable command and DB disagree, a claim is ambiguous,
or lifecycle ownership is unsafe. Do not repair live state implicitly.

## Mode: add

An item is eligible for this queue only when all are true:

- independently executable from a concrete cwd and goal;
- completion can be demonstrated;
- ambiguity can be resolved conservatively without expanding authority;
- required credentials/tools are references already present in the environment;
- no user decision is required during execution.

The task must be independently executable with clear authority. Redirect interactive design,
unclear publishing authority, and work that requires a person's response during execution.
Every queued task must be safe to leave queued until capacity permits or Brian explicitly
accelerates it.

Capture at least: stable ID, title, kind (`oneoff` or `recurring`), priority, size, cwd, goal,
context, constraints, precondition, done-when, and compatible provider/task routing. Also capture
source thread/plan reference when available, a work group when useful, and explicit prerequisite
task IDs. Never infer dependencies or authorization from similar titles. Priority is urgency/drain
order; size is the best available estimate of autonomous scope and effort.
Work groups are optional navigation labels, not task titles: use them only for a meaningful
cross-task cluster and keep each at 15 characters or fewer. Use title case; the soak-observation
group is `Soak Obs`.
Before choosing a work group, inspect the groups already used by active tasks and reuse the
existing label for the same cluster instead of creating a spelling or version variant.
Estimate size before previewing or adding the task:

- `tiny`: one deterministic action or edit plus one quick proof; roughly under 15 minutes.
- `small`: a few bounded edits/checks or narrow research; roughly under 1 hour.
- `medium`: multi-file work or several evidence paths; roughly 1–3 hours.
- `large`: cross-module, integration/E2E, or substantial research; roughly 3–8 hours.
- `huge`: broader than one workday or highly uncertain; split it when possible, and reject it
  when it cannot remain one autonomous job (safely skippable when using Bonus).

When between sizes, choose the larger. `unknown` is display-only for legacy/null rows and is
never valid on add. Prefer `mcp=none` unless the task demonstrably needs a named server. Mark
build-shaped work only through the explicit implementation flag; do not infer it from cwd or
prose.

Any task that requires live Kubernetes proof, multiple external components, or integration
E2E is at least `medium`. Choose `large` when the required proof crosses two or more component
boundaries, even if the source edit itself looks small.

Preview the validated task, then add it with the CLI and its required estimate:

```sh
bonus-drain add --id TASK_ID --title "TASK TITLE" --kind oneoff --priority 2 \
  --size medium --cwd /absolute/project/path --goal "CONCRETE GOAL" \
  --source-ref "THREAD_OR_PLAN_REFERENCE" \
  --work-group "WORK GROUP" --depends-on PREREQUISITE_ID --json
```

After adding, read the canonical JSON task back by ID:

```sh
bonus-drain contract-task --id TASK_ID --title "TASK TITLE"
```

Inspect the exact-ID task object and compare every execution field, including `size`.
Human `queue-status` output is not add verification. A duplicate, extra canonical match, or
mismatched canonical identity is an error, not permission to create a near-duplicate.

For an explicitly authorized current-upcoming size backfill, freeze the authoritative `pick`
and its exact cycle as `CYCLE` from the same current scout/viewer snapshot, then have an
independent estimation agent assign one rubric value per returned ID from the complete task
contract. Immediately fetch that pick again using the same frozen `CYCLE` and update only IDs
in both snapshots:

```sh
bonus-drain set-size TASK_ID medium --cycle "$CYCLE" --json
```

Verify each returned task object, then re-run the current pick and confirm every still-upcoming
task has a non-null size. `set-size` does not discover the current cycle; it validates the
operator-supplied frozen cycle at its transaction boundary and does not close a later
post-recheck dispatch race. Never query or update spent, inactive, run-log-only, or otherwise
absent rows for this backfill. Recurrence is based on elapsed time from the most recent recorded
run, never a provider-reset or manual-dispatch cycle: weekly jobs cool down for at least four
days and monthly jobs for at least 28 days. A recurring task absent during that cooldown stays
null until it is separately eligible for an authorized upcoming-only estimate.

## Mode: edit and inspect

Read `ASYNC_WORK.md` for dependencies, editing, run provenance, and the review UI. A dependency
is satisfied only by a successful (`done`) one-off prerequisite; failed, skipped, running, and
missing prerequisites keep the child waiting. Self-dependencies, cycles, missing IDs, and
recurring prerequisites are rejected. Both automatic and explicit launches enforce dependencies.

Use `bonus-drain readiness TASK_ID --json` to explain readiness and
`bonus-drain edit TASK_ID --changes '{"depends_on":["PARENT_ID"]}' --json`
to update a queued contract. Read back with `contract-task`. A live claimed task cannot be
edited; finish or reconcile it before revising it. Successful prerequisites do not auto-start
a child; it becomes eligible for either normal capacity dispatch or an explicit acceleration.

## Mode: automatic bonus run

An explicit request to run a queued task uses `run-now TASK_ID`, not a scout tick.
"Run bonus drain" means one normal scout tick:

1. Read eligible count without provider I/O.
2. Read normalized cached account snapshots and build independent gates.
3. Fail closed per affected account; do not close healthy siblings.
4. Order open batches by nearest exact reset.
5. Pick only tasks compatible with the concrete provider.
6. Claim before activation and routing.
7. Activate the selected account only when configured.
8. Route once and record the concrete provider/account/job identity.

Use:

```sh
bonus-drain scout --json
```

Do not loop to empty the queue. systemd owns later ticks. A zero-dispatch result with explicit
closed reasons is successful operation.

Before the in-flight gate, scout checks router status and records `failed` for a positively
terminal worker that omitted its terminal event, using the existing claim/lease lifecycle.
Running or unknown jobs remain held; missing status and elapsed time do not prove exit.
Inspect the `reconciliation` list in scout JSON. `--dry-run` only proposes queue repairs.
Ambiguous claims still require operator reconciliation and are never cleared automatically.

## Mode: explicit acceleration

An explicit acceleration is a one-task bypass of capacity pacing, not of queue safety. Keep normal
eligibility, compatibility, atomic claim, concrete router launch, activation-lease, and
terminal-record contracts. CLI `auto` may classify immediately before its one concrete launch;
the viewer never accepts `auto`. Do not loop or retry ambiguous launches.

Use the stable CLI's shared dispatch path; never call a provider command or a legacy monitor
directly:

```sh
bonus-drain dispatch TASK_ID [PROVIDER_ID_OR_auto] [--account ACCOUNT_ID] --json
```

## Terminal contract

Every dispatched task must record exactly one terminal event through the command embedded in
its prompt. The prompt must include task ID, kind, eligibility key, concrete provider and
account, DB/config identity, precondition, constraints, and done-when.

A background task must not exit blocked or waiting for input while its claim and activation
lease remain live. If continuing safely would require new input or authority, it records
`failed` with that blocker before exiting.

Replaying the same terminal status for one task and eligibility key is idempotent. A conflicting
terminal status or pre-existing duplicate terminal history is a reconciliation error, never a
second terminal event.

- `done`: done-when is demonstrated.
- `skipped`: the precondition is false or the work is already complete.
- `failed`: work was attempted and did not satisfy done-when.

Do not call an ambiguous router response failed: its claim remains held until `doctor` and an
operator reconcile whether a job exists. Requeue is an explicit operator action and removes
the matching terminal history and claim atomically; it is not an automatic retry.

## Operations

- Ten-minute cache refresh: `bonus-drain-refresh.timer`.
- Hourly scout: `bonus-drain-scout.timer`.
- Optional viewer: the established two-tab background-jobs UI. Run now is manual and delegates
  only to the shared `kick_task` to `agent-router` path. Tailscale Serve is the sole access
  boundary; there is no application login. Exact Host/HTTPS Origin and JSON-only checks
  protect browser mutations; see `SECURITY.md` before remote use.
- Install/status/doctor/removal: see `README.md`.
- DB/unit cutover and rollback: dry-run report plus the manual procedure in `MIGRATION.md`.
- Legacy markdown/jsonl: separate `bonus-drain import-legacy` only.

Never install, enable, expose, apply a cutover, roll back, or delete state merely because this
skill was invoked. Those are separate operator-authorized actions.
