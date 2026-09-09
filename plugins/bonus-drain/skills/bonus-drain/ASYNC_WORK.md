# Async Work

For whole-goal coordination across jobs, PR stacks/merges, and repeated combined
acceptance, see [Long Horizon goals](GOALS.md). It uses this same queue; work groups
remain navigation labels unless explicitly linked to a goal contract.

Plan in a thread, hand off a concrete contract, queue it, start it, and review the result.
Bonus Drain is the optional spare-capacity scheduler for the same queue. The executable,
installation paths, database identity, router, and release version keep their existing names.

## Execution policy

Every ready task is eligible for the automatic Bonus scheduler when capacity gates permit.
Priority controls drain order. `run-now` and the viewer's Run now button accelerate one task by
bypassing pacing only; they do not bypass dependencies, recurrence cooldown, provider
compatibility, atomic claims, or activation leases. Paused tasks cannot run.

## Dependencies

Use `--depends-on parent-id,other-parent-id` on add, or update the prerequisite list from a planning thread with the CLI.
The graph can branch and join. A child becomes ready only when **every prerequisite is done**.
Failed and skipped prerequisites leave it waiting; they do not launch a child or mark it failed.
A waiting child creates no run, consumes no claim, and does not call the router.

Prerequisites must be existing one-off tasks. Children may be one-off or recurring; a recurring
child uses those completed one-off prerequisites for each recurrence. Recurring prerequisites
are deliberately rejected until an explicit rule exists for which occurrence satisfies a child.
Self-dependencies and cycles are rejected transactionally. Readiness is checked again during
claiming, including for Run now. A newly ready child waits for a subsequent normal scheduler
tick; completion does not launch a cascade.

Use task dependencies for workflow ordering. The task's textual precondition still describes
external checks performed by the runner. If a runner discovers its precondition is false, it
records skipped under the existing lifecycle contract.

## Thread handoff and editing

```sh
bonus-drain add --id build-report --title 'Build Report' --kind oneoff \
  --size small --cwd /absolute/project --goal 'Produce the agreed report' \
  --source-ref 'THREAD_OR_PLAN_REFERENCE' \
  --work-group 'Report work' --depends-on gather-evidence --json
bonus-drain readiness build-report --json
bonus-drain edit build-report --changes '{"goal":"Produce the revised report"}' --json
bonus-drain run-now build-report auto --json
```

The source reference is a link or opaque reference, not an instruction to fetch arbitrary
thread contents. The stored task contract must remain self-contained. Only HTTP(S) source
references become links in the viewer; other references are displayed as text.

Editing allows title, priority, size, cwd, goal, context, constraints, precondition, done_when,
source_ref, work_group, and depends_on. Active claims and already-run one-off
contracts cannot be edited. For ordinary tasks outside managed goals, a failed/skipped
task must first be explicitly requeued; that operation removes its matching run history.
Goal-owned jobs retain all attempts and reject requeue: create a fresh follow-up with
the failure evidence instead. Coordinator and acceptance execution contracts are frozen;
only their priority, size and active controls remain editable. See [GOALS.md](GOALS.md)
for replacing an obsolete, unlaunched verifier without changing its proof subject.
Use a work group only when it forms a useful cross-task cluster, and keep its name to 15
characters or fewer so the queue filter stays compact. Use title case; the soak-observation
group is `Soak Obs`.

The viewer shows readiness, work group, source reference, and prerequisite progress, with
filters for each workflow facet. Rotation and provider usage remain visible above the queue.
Task contracts are edited through planning threads and the CLI; the viewer has no job editor. Actual new
launches record explicit or automatic provenance; terminal events inherit it. Old rows remain
origin unknown. The scheduled value is reserved for future queue-backed timer integration.

## Calendar scheduling

`bg-schedule` still owns exact times and calendar recurrence. General queue handoffs should
use this skill; timed jobs remain on the Schedules tab. This branch does not install or change
that external skill. Unifying timers by having them start a queued task ID is a follow-up;
the queue's existing weekly/monthly cooldowns are not exact calendar schedules.

## Isolated review preview

From the repository root, create a new private destination using SQLite's online backup API:

```sh
python3 scripts/preview-async-work.py --source-config /absolute/live/config.json \
  --destination /absolute/new/private-preview --host EXACT_TAILSCALE_HOST:PORT --examples
```

Run the source viewer with **both** `BONUS_DRAIN_CONFIG=/absolute/new/private-preview/config.json`
and `BONUS_DRAIN_BIN=/absolute/worktree/plugins/bonus-drain/skills/bonus-drain/bin/bonus-drain`.
The second variable keeps compatibility wrappers on the same branch. Start only the viewer:
`python3 plugins/bonus-drain/skills/bonus-drain/services/jobs-viewer/server.py --port PORT`.
Front its loopback listener with a separately owned Tailscale Serve HTTPS port.

The preview has a copy of tasks, run history, and cached capacity. Copied running rows are
historical snapshot evidence, not proof that this preview owns those jobs. Four clearly labeled
example tasks demonstrate a dependency chain without inferring dependencies for real work.
All execution is disabled. Requeues affect the copy. Task editing remains a CLI operation. Do not enable a scout or
refresher against it. Stop the preview process and remove only its Tailscale Serve port when
review finishes; keep the copied state until its owner chooses to discard it.
