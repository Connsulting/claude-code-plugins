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
   non-launching. Claiming creates a unique immutable attempt alongside
   `(task_id, eligibility_key)` before concrete activation/routing. Known-not-launched failure
   records that attempt as aborted and releases it; a post-launch ambiguity records the exact
   attempt as ambiguous and holds the claim and any activation lease fail-closed for
   reconciliation. Never claim activation releases immediately after ambiguity.
   A configured launch-scoped activation releases only after a concrete job identity and its
   dispatched record exist; run-scoped activation remains held through the terminal event.
7. Use the resolved executable record command embedded in the dispatched prompt. It must
   point to the stable CLI and the JSON graph's database, bind the exact attempt, and use its
   protected structured outcome-evidence path. Do not reconstruct uncertain CLI flags.
   `BONUS_DB` is deprecated queue-only compatibility and cannot retarget the configured graph.
8. No publish, merge, credential change, production mutation, contract/schema/ADR change, or
   other externally consequential action is implied by being bonus work. The task contract
   must grant it explicitly.
9. Only explicit done-when verification satisfies a dependency. A PR, branch, router status,
   failed attempt, or skipped attempt is not completion evidence.
10. Recovery keeps the original task ID and prior attempts. It cannot weaken authority,
    dependency, activation, calendar, GoalStore, or frozen-candidate gates.

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

A precondition is an external fact the worker cannot create. If it is false, the worker records
skipped and stops, and that stop is correct. Be critical of the sentence before queueing it.
Do not write a precondition that is really setup the worker is already allowed to perform: a
clean checkout, a named branch, a private worktree, free default ports, an unlocked shared
baseline, or installed local dependencies. Put the base ref and the test commands in constraints
or done-when. The worker creates its own worktree from that remote base, binds private ports,
and installs dependencies, and it does not clean another owner's checkout. Write a precondition
only for a fact outside that setup: missing authority, a prerequisite or release fact the worker
cannot create, an unavailable provider or required service, a frozen contract, another owner
already editing the same paths, or a validation gate the worker must not weaken. If none of
those applies, leave the precondition empty. Do not invent a checkout check so the task looks
guarded.
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
absent rows for this backfill. Weekly recurrence uses Saturday-starting calendar weeks in the
configured timezone. The automatic scout may launch a weekly task only on Sunday and at most
once in that week; a missed Sunday is not carried into Monday. Manual acceleration outside
Sunday consumes the same weekly slot. Monthly recurrence retains its 28-day elapsed cooldown.
A recurring task that is not currently eligible stays null until it becomes eligible for an
authorized upcoming-only estimate.

## Mode: edit and inspect

For a whole outcome spanning jobs, PR joins, and repeated acceptance/fix rounds, use
the plugin's [Long Horizon skill](../long-horizon/SKILL.md) and [GOALS.md](GOALS.md).
It uses this same queue and dispatcher. A goal is durable waiting state; its short
coordinator jobs finish between joins, so an idle coordinator holds no dispatch claim.

Read `ASYNC_WORK.md` for dependencies, editing, run provenance, and the review UI. A dependency
is satisfied only by a verified successful (`done`) one-off prerequisite; failed, skipped,
running, and missing prerequisites keep the child waiting. A blocked active child may make its
failed/skipped parent eligible for the bounded recovery policy described below. Self-dependencies,
cycles, missing IDs, and recurring prerequisites are rejected. Both automatic and explicit
launches enforce dependencies.

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
terminal worker that omitted its terminal event, using that exact attempt and an unverified
structured reason under the existing claim/lease lifecycle.
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
its prompt. The prompt includes the stable task ID, immutable attempt ID, kind, eligibility key,
concrete provider and account, DB/config identity, precondition, constraints, done-when, and a
protected path for structured outcome evidence. Use that command exactly.

A background task must not exit blocked or waiting for input while its claim and activation
lease remain live. A run that opened or updated a PR records `done`. When only a step Brian must
take personally remains, it records `awaiting_human`. If the work itself cannot be completed, it
records `failed` with that blocker before exiting.

Replaying the same terminal status and evidence for the same attempt is idempotent. A missing or
different attempt ID cannot release its claim, and a conflicting replay is a reconciliation
error. Prior attempts stay immutable.

- `done`: done-when is explicitly verified with supported evidence. A run that opened or updated
  a PR is done, even while that PR awaits review, approval, or pending CI; it records
  `completion.mechanism: artifact` with the PR URL as evidence. PR presence still does not prove
  integration for a dependency handoff. Its outcome uses reason code `done_when_verified`, `completion.verified: true`,
  one of `command`, `artifact`, `operator_receipt`, or `goal_acceptance`, and nonempty evidence.
- `skipped`: the work is already complete, or a genuine precondition is false. That means
  missing authority, a prerequisite the worker cannot create, an unavailable provider or
  required service, a frozen contract, another owner already editing the same paths, or a
  validation gate that setup cannot remove. A dirty or wrong-branch shared checkout,
  untracked worktree directories, occupied default ports, a shared baseline lock, or a
  missing local dependency is setup: create an isolated worktree from the named remote
  base, bind private ports, and install dependencies without cleaning another owner's
  checkout. Include the structured reason.
- `failed`: work was attempted and did not satisfy done-when; record a structured reason that
  distinguishes retryable, verification-needed, authority, permanent, and unknown-launch cases.
- `awaiting_human`: the worker finished everything it can and the remaining step needs Brian
  personally, such as hands-on testing only he can do or a decision or approval. Its structured
  reason must not use `done_when_verified`, its `reason.detail` must name exactly what Brian must
  do, and it must not claim verified completion. It is not requeued or recovered automatically,
  and dependents keep waiting; only operator recovery (requeue or recover-complete) continues it.

Follow the exact `OUTCOME_SCHEMA` printed in the prompt. A repository-producing verified success
has this shape; omit `repository` when the task does not produce one:

```json
{
  "reason": {"code": "done_when_verified", "detail": "what passed", "signature": "stable-non-secret-signature"},
  "completion": {"verified": true, "mechanism": "command", "evidence": ["bounded verification reference"]},
  "repository": {
    "remote": "exact canonical remote URL",
    "target_ref": "refs/heads/main",
    "target_base_oid": "full starting commit OID",
    "branch_ref": "refs/heads/task/example",
    "head_oid": "full result commit OID",
    "integration_state": "merged|unmerged",
    "merge_receipt": {"kind": "merge|squash", "result_oid": "full verified target result OID"}
  }
}
```

`merge_receipt` is optional. For `failed`, `skipped`, or `awaiting_human`, omit `completion` and use a reason code of
`retryable`, `verification_needed`, `authority_required`, `permanent`, or `unknown_launch`, with
nonempty detail and a stable non-secret signature. Accepted completion mechanisms are `command`,
`artifact`, `operator_receipt`, and `goal_acceptance`.

Do not call an ambiguous router response failed: its claim remains held until `doctor` and an
operator reconcile whether a job exists.

Automatic recovery applies only when active work depends on a failed or skipped one-off. It keeps
the task ID and all attempts, allows at most two automatic recovery attempts after 5-minute and
30-minute backoffs, and stops early when the normalized reason repeats. Legacy or unspecified
failure is verification-first. Missing authority, unknown launch ownership, unavailable Git
identity, and divergent parent heads remain held.

Ordinary public `requeue` schedules an operator recovery without deleting history. Public requeue
for goal-managed tasks remains rejected. GoalStore may internally admit implementation or
integration recovery under its existing gates; a failed coordinator or frozen acceptance job
uses its documented fresh follow-up path.

If the user later says “try harder” in the same thread after this attempt recorded failed or
skipped, continue under the original task ID and contract. Before more work, run the exact
`continue-progress` command embedded in the prompt. It marks that same router job in progress
and does not launch another worker. When the continued work finishes, record the terminal
result with the prompt's record command, replacing only the attempt id with the `attempt_id`
`continue-progress` printed. Do not call `recover-complete` after `continue-progress` succeeds.
If `continue-progress` refuses, stop without changing the original attempt and do not launch a
replacement. If `continue-progress` was not opened and the continued work already proves
done-when, write the required evidence and run the exact stable package CLI `recover-complete`
command already embedded in the prompt before replying. This command is separate from the
configurable record adapter, which may not support recovery. It appends a verification attempt
without a new claim or router launch and does not require a previously scheduled recovery
projection; if an exact projection exists, it consumes it atomically. If it refuses because a
successor owns the task, keep the evidence for that successor and do not overwrite its state.

For repository dependencies, use the resolved handoff in the prompt. A merged parent requires the
exact target plus ancestry and content proof; a squash merge uses content equivalence. An unmerged
parent uses its verified head. Unavailable identity holds dispatch, and divergent multiple-parent
heads require an explicitly authorized integration task. Never merge or expand external authority
to make a dependency ready.

## Operations

- Ten-minute cache refresh: `bonus-drain-refresh.timer`.
- Hourly scout: `bonus-drain-scout.timer`.
- Optional viewer: the established two-tab background-jobs UI. Run now is manual and delegates
  only to the shared `kick_task` to `agent-router` path. In-flight Mark done / Mark failed
  records a terminal event through the same `record` path as the CLI and frees the dispatch
  slot; it does not stop the provider worker. Tailscale Serve is the sole access
  boundary; there is no application login. Exact Host/HTTPS Origin and JSON-only checks
  protect browser mutations; see `SECURITY.md` before remote use.
- Install/status/doctor/removal: see `README.md`.
- DB/unit cutover and rollback: dry-run report plus the manual procedure in `MIGRATION.md`.
- Legacy markdown/jsonl: separate `bonus-drain import-legacy` only.

Never install, enable, expose, apply a cutover, roll back, or delete state merely because this
skill was invoked. Those are separate operator-authorized actions.
