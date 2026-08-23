---
name: bonus-drain
description: Manage and run a low-priority bonus backlog funded only by independently configured provider capacity that would otherwise expire. Use for "bonus background task", "add to bonus backlog", "drain leftover tokens", "if there are tokens spare do this", or "kick bonus work now". Add mode validates autonomy and queues work; run performs one cache-gated scout tick; kick force-dispatches a bounded count while preserving claims and router-only launch. Not for urgent or interactive work.
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
6. Claim `(task_id, eligibility_key)` transactionally before activation/routing. Known launch
   failure releases it. Terminal record closes it. Ambiguity holds it for reconciliation and
   releases activation immediately.
7. Use the resolved executable record command embedded in the dispatched prompt. It must
   point to the stable CLI and the same database shown by `BONUS_DB`.
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

Capture at least: stable ID, title, kind (`oneoff` or `recurring`), priority, cwd, goal,
context, constraints, precondition, done-when, and compatible provider/task routing. Prefer
`mcp=none` unless the task demonstrably needs a named server. Mark build-shaped work only
through the explicit implementation flag; do not infer it from cwd or prose.

Preview the validated task, then add it with the CLI. After adding, read it back by ID and
verify every execution field. A duplicate or mismatched canonical identity is an error, not
permission to create a near-duplicate.

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

## Mode: kick

`kick N` is a bounded manual bypass of capacity pacing, not of queue safety. Keep normal
eligibility, compatibility, atomic claim, concrete router launch, account release, and
terminal-record contracts. Refuse non-positive or unbounded counts. Do not classify in a
scout; a manual auto request may classify immediately before its one concrete launch.

Use the stable CLI's `kick`/manual-dispatch command shown by `bonus-drain --help`; never call
a provider command or a legacy monitor directly.

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
- Optional viewer: loopback/read-only by default; see `SECURITY.md` before remote use.
- Install/status/doctor/removal: see `README.md`.
- DB/unit cutover and rollback: dry-run report plus the manual procedure in `MIGRATION.md`.
- Legacy markdown/jsonl: separate `bonus-drain import-legacy` only.

Never install, enable, expose, apply a cutover, roll back, or delete state merely because this
skill was invoked. Those are separate operator-authorized actions.
