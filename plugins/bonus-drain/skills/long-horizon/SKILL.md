---
name: long-horizon
description: Coordinate a whole engineering goal or Async Work group through independent /implement jobs, stacked or merged PRs, combined acceptance, and repeated fix rounds. Use for Long Horizon design, execution, and resumption. Design or installation alone does not start a goal.
---

# Long Horizon

Replace the human outer loop around Async Work: select goal-needed work, coordinate
dependencies and PR integration, review the combined change where needed, and prove
the agreed outcome. Each build job still runs its own `/implement` pipeline.

Read [the queue skill](../bonus-drain/SKILL.md) for preflight, capacity and dispatch
ownership, and [GOALS.md](../bonus-drain/GOALS.md) for the goal CLI and recovery contract.
The plugin's existing SQLite queue, scout, dispatcher and router own execution.

## Start or resume

Read the existing goal and source plan before doing work. Preserve accepted decisions,
authority, frozen task contracts, live owners, deadlines and consumed turns. Ask only
for consequential decisions missing from the existing authorization. A new goal needs
observable acceptance, permitted effects, explicit task membership, a deadline and
coordination/concurrency bounds. Clear authorized work does not need another interview.
Never treat installation or a proposed goal as execution authority.

An existing work group is a navigation label. Snapshot its exact task IDs and inspect
their contracts and real dependency graph before creating a goal. `goal create` links
those jobs without changing their fields or taking ownership of their scheduling.
Do not infer that the group’s last named E2E task depends on every required component.
Register the actual final join and the acceptance it must prove.

Select `stack` when PRs should remain open for review, or `merge` when the run explicitly
authorizes the coordinator to merge verified PRs. Preserve narrower existing task
permissions. New build tasks use `/implement`; do not retrofit live or historical jobs
to a different pipeline. Research and operational jobs retain their actual contracts.

## One coordination turn

1. Read the goal with the exact read command embedded in the dispatch prompt. It binds
   this turn to its runtime, config and database. Check new results, steering, operations with
   missing receipts, actual PR heads/bases and remaining bounds. The task ID in the
   dispatched prompt identifies this turn; a context-window ID does not.
2. Make the next outer decision. Create concrete independent jobs or resolve the PR
   join. Give each job its scope, source/base/dependencies, allowed effects, resource
   boundaries, done-when and durable result location. Use `goal advance` to atomically
   add new jobs and register the join to wait for. Queue dependencies handle subsequent
   launches; the coordinator does not launch provider processes or duplicate jobs.
3. Return missing task gates to their owner. `/implement` owns planning, source/test
   agents, debugging, reviews, CI, task E2E and cleanup. Do not lend workers, issue stage
   grants, or reproduce those pipelines. Inspect their final evidence instead.
4. Record one `wait`, `pause`, or `complete` decision, then record this coordination job
   `done` with the dispatched terminal command and **end the turn**. A waiting goal is
   not a waiting worker: it has no running coordinator, claim or activation lease.

Do not stay in a polling conversation, sleep for an hour, monitor routine task CI, or
repeatedly write status while jobs run. The deterministic scout observes the registered
join. Partial completions and unchanged polls enqueue zero coordinator turns. A settled
join, explicit steering, or resumed decision creates one ordinary queue job. Existing
Capacity pacing and per-provider in-flight caps govern its launch. An optional global job cap applies only when configured.
Do not bypass them to reduce wake latency. Explicit acceleration remains a one-task action.

## PR dependencies and integration

Independent roots run concurrently. A task needing another task's code waits for that
task’s successful result, then uses its exact returned head. In stack mode, base the PR
on that upstream branch. When several unmerged branches meet, have an integration job
assemble their exact heads on an owned branch and record the real PR-base graph.
A linear dependency chain serializes; independent branches do not need to.

In merge mode, the coordinator may merge only within the goal authority and after the
required checks, reviews, dependency and cleanup gates pass on the exact head. Save
intent with `goal operation` before each merge and its receipt afterwards. Reconcile
an uncertain result against GitHub before retrying. Delegate conflict corrections and
substantial assembly work to their owning jobs. Check affected proof on the new candidate.

Preserve existing review boundaries. A new combined diff or integration decision may need
its own review; this does not justify reviewing every task twice. Use the established
`agent-router adversarial-review` interface when required, with truthful authorship and
capacity rules. A timeout or skipped review is not approval.

## Acceptance and another round

After assembly, register an acceptance job pinned to the actual immutable commit(s) and
relevant runtime identities. Use real product entry points and the agreed positive and
negative paths. For workflows, schedules or external systems, record their runtime
evidence as well as source; a tested branch alone does not prove a live workflow.

When acceptance fails, retain that attempt and finding. Queue a concrete correction
through `/implement`, wait for its result, assemble the new candidate, then run fresh
acceptance. Do not predeclare the commit hash a future fix will produce. Do not make a
fix depend on the failed verifier: queue dependencies require `done`. Reference its
failure evidence in context instead. Retain unaffected completed work.

Fix ordinary environment and staging problems within existing authority. An unavailable
credential, missing permission, frozen contract, or exhausted agreed bound needs a
concrete pause reason; it does not become a passed check. Continue independent authorized
work using an appropriate join. Do not invent per-episode retry caps or reset the goal’s
cumulative bounds. Only the user can extend them through explicit resumption.

Complete only with all required acceptance observations passing on the identified final
candidate, task/operation ownership settled, and cleanup proved. Record evidence paths,
PR review order, unresolved human actions and any separately unelapsed reliability period.
Neither all tasks being `done` nor all PRs being merged proves goal acceptance.

## Models, status and recovery

For Codex goals, explicitly select Astra (`gpt-6-astra`) for coordination; select Sol
(`gpt-5.6-sol`) for complex drivers, Terra (`gpt-5.6-terra`) for clear bounded builds,
and Luna (`gpt-5.6-luna`) for mechanical work. Pass choices through task routing fields
and record actual router results. Claude goals retain their native model semantics.

After context compaction, reread this skill and GOALS.md, then reconcile durable goal
state. Reconnect a healthy owner; never replace one because its heartbeat is old.
The runtime pauses a coordinator that ends without a recorded decision. Reconcile its
effects before an explicit resume; preserve all prior turns and task results.

Keep status concise and tied to observable outcomes, candidate, blockers and next action.
Use the existing Async Work viewer/CLI; do not install another dashboard or watcher.
Planning-thread/CLI controls own goal edits. Notifications require existing authorization
and remain coordinator-only at the agreed cadence; workers do not send progress digests.
