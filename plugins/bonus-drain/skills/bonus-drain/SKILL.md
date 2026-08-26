---
name: bonus-drain
description: Manage and run a low-priority bonus backlog funded only by independently configured provider capacity that would otherwise expire. Use for "bonus background task", "add to bonus backlog", "drain leftover tokens", "if there are tokens spare do this", or "kick bonus work now". Add mode validates autonomy and queues work; run performs one cache-gated scout tick; manual dispatch preserves claims and router-only launch. Not for urgent or interactive work.
---

# Bonus Drain

Bonus Drain is an opportunistic queue, not a completion promise. It may leave all work
queued when usage is unknown, stale, ahead of pace, outside a reset lead window, or already
in flight. Never reinterpret a closed gate as spare capacity.

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

- low priority and safe to skip for a week;
- independently executable from a concrete cwd and goal;
- completion can be demonstrated;
- ambiguity can be resolved conservatively without expanding authority;
- required credentials/tools are references already present in the environment;
- no user decision is required during execution.

Reject or redirect urgent work, interactive design, broad cleanup, unclear publishing,
production changes, and tasks whose success depends on another person's response.

Capture at least: stable ID, title, kind (`oneoff` or `recurring`), priority, size, cwd, goal,
context, constraints, precondition, done-when, and compatible provider/task routing. Priority
is urgency/drain order; size is the best available estimate of autonomous scope and effort.
Estimate size before previewing or adding the task:

- `tiny`: one deterministic action or edit plus one quick proof; roughly under 15 minutes.
- `small`: a few bounded edits/checks or narrow research; roughly under 1 hour.
- `medium`: multi-file work or several evidence paths; roughly 1–3 hours.
- `large`: cross-module, integration/E2E, or substantial research; roughly 3–8 hours.
- `huge`: broader than one workday or highly uncertain; split it when possible, and reject it
  when it cannot remain one autonomous, safely skippable job.

When between sizes, choose the larger. `unknown` is display-only for legacy/null rows and is
never valid on add. Prefer `mcp=none` unless the task demonstrably needs a named server. Mark
build-shaped work only through the explicit implementation flag; do not infer it from cwd or
prose.

Preview the validated task, then add it with the CLI and its required estimate:

```sh
bonus-drain add --id TASK_ID --title "TASK TITLE" --kind oneoff --priority 2 \
  --size medium --cwd /absolute/project/path --goal "CONCRETE GOAL" --json
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
absent rows for this backfill. A recurring task absent because it already ran this cycle stays
null until it is separately eligible for an authorized upcoming-only estimate.

## Mode: run

`run` means one normal scout tick:

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

## Mode: manual dispatch

Manual dispatch is a one-task bypass of capacity pacing, not of queue safety. Keep normal
eligibility, compatibility, atomic claim, concrete router launch, activation-lease, and
terminal-record contracts. CLI `auto` may classify immediately before its one concrete launch;
the viewer never accepts `auto`. Do not loop or retry ambiguous launches.

Use the stable CLI's shared dispatch path; never call a provider command or a legacy monitor
directly:

```sh
bonus-drain dispatch TASK_ID [PROVIDER_ID_OR_auto] --json
```

## Terminal contract

Every dispatched task must record exactly one terminal event through the command embedded in
its prompt. The prompt must include task ID, kind, eligibility key, concrete provider and
account, DB/config identity, precondition, constraints, and done-when.

- `done`: done-when is demonstrated.
- `skipped`: the precondition is false or the work is already complete.
- `failed`: work was attempted and did not satisfy done-when.

Do not call an ambiguous router response failed: its claim remains held until `doctor` and an
operator reconcile whether a job exists. Requeue is an explicit operator action and removes
the matching terminal history and claim atomically; it is not an automatic retry.

## Operations

- Ten-minute cache refresh: `bonus-drain-refresh.timer`.
- Hourly scout: `bonus-drain-scout.timer`.
- Optional viewer: the established two-tab background-jobs UI. Force is manual and delegates
  only to the shared `kick_task` to `agent-router` path. Tailscale Serve is the sole access
  boundary; there is no application login. Exact Host/HTTPS Origin and JSON-only checks
  protect browser mutations; see `SECURITY.md` before remote use.
- Install/status/doctor/removal: see `README.md`.
- DB/unit cutover and rollback: dry-run report plus the manual procedure in `MIGRATION.md`.
- Legacy markdown/jsonl: separate `bonus-drain import-legacy` only.

Never install, enable, expose, apply a cutover, roll back, or delete state merely because this
skill was invoked. Those are separate operator-authorized actions.
