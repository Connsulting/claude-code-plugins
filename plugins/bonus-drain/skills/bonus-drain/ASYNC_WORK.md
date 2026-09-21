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

Failed and skipped attempts remain attached to the original task ID. When such a one-off blocks
active dependent work, the automatic scout may schedule at most two recovery attempts: the first
after 5 minutes and the second after 30 minutes. A legacy or unstructured failure is verified
before it can be retried as successful. Recovery stops early when the normalized reason repeats.
Unknown launch ownership, missing authority, unavailable repository identity, and ambiguous or
divergent dependency state remain held; none is interpreted as success.
Authority-required, permanent, unknown-launch, and no-progress recovery holds never retry
silently. Continuing that work requires a fresh, explicit follow-up task under reviewed policy
and authority; requeue or edit does not reinterpret the retained hold.

Prerequisites must be existing one-off tasks. Children may be one-off or recurring; a recurring
child uses those completed one-off prerequisites for each recurrence. Recurring prerequisites
are deliberately rejected until an explicit rule exists for which occurrence satisfies a child.
Self-dependencies and cycles are rejected transactionally. Readiness is checked again during
claiming, including for Run now. A newly ready child waits for a subsequent normal scheduler
tick; completion does not launch a cascade.

Use task dependencies for workflow ordering. The task's textual precondition still describes
external checks performed by the runner. If a runner discovers its precondition is false, it
records skipped under the existing lifecycle contract.

Each claimed launch receives a new immutable attempt ID. The dispatched prompt contains the exact
terminal command and protected outcome-evidence path bound to that attempt. A new `done` event
requires reason code `done_when_verified`, `completion.verified: true`, a supported completion
mechanism (`command`, `artifact`, `operator_receipt`, or `goal_acceptance`), and nonempty evidence
that verifies done-when. A run that opened or updated a PR is done, even while that PR awaits
review, approval, or pending CI: it records `completion.mechanism: artifact` with the PR URL as
evidence. PR presence still does not prove integration for a dependency handoff. Failed, skipped,
and `awaiting_human` outcomes use one of `retryable`, `verification_needed`, `authority_required`,
`permanent`, or `unknown_launch` with nonempty detail and a stable non-secret signature.
`awaiting_human` means the worker finished everything it can and the remaining step needs Brian
personally; its detail names exactly what Brian must do. It is never requeued or recovered
automatically, and dependents keep waiting; only operator recovery (requeue or
recover-complete) continues it. Terminal
replay is idempotent only for the same attempt, and an old attempt cannot release a newer claim.
Failed, skipped, ambiguous, and proved-not-launched aborted attempts remain in history.

If the user continues the same failed/skipped worker thread with “try harder” and the work then
meets done-when, the worker keeps the original task ID, writes the required verified evidence, and
runs the exact stable package CLI `recover-complete` command already embedded in its prompt before
replying. That command is separate from the configurable terminal-record adapter, which may not
support recovery. It appends a verification attempt without a new claim or router launch and works
even when no recovery projection has been scheduled; when one exists, it consumes only the exact
matching projection. It refuses changed contracts, ambiguous ownership, or an already queued or
active successor; the worker then keeps its evidence for that successor instead of recording over
it.

Repository-producing completion includes the exact remote, target ref, prior target base, branch,
and head. A child starts from the current exact target only when ancestry and content prove the
parent is present; squash merges use content equivalence because parent-head ancestry alone cannot
prove them. Otherwise an explicitly unmerged parent supplies its verified head. Missing or
conflicting identity holds dispatch. For multiple parents, one compatible descendant may contain
all heads; divergent heads require an explicitly authorized integration task. The queue does not
merge branches or add push, PR, deployment, or other external authority.

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
task must first be explicitly requeued; that schedules an operator recovery and preserves every
prior attempt. Its contract can then be edited only before that exact recovery is claimed.
Goal-owned jobs retain all attempts and continue to reject public requeue. The goal runtime may
internally admit recovery for implementation or integration work without bypassing its authority,
deadline, concurrency, operation, or frozen-candidate checks. Coordinator and acceptance execution
contracts stay frozen and use the documented fresh follow-up flows; only their priority, size and
active controls remain editable. See [GOALS.md](GOALS.md) for replacing an obsolete, unlaunched
verifier without changing its proof subject.

The `--now` clocks on `requeue`, `dispatch`, and `scout` are trusted operator controls. Worker-facing
`record` and `recover-complete` use server receipt and admission time instead. A worker-supplied
historical run timestamp does not choose the recovery backoff clock.
Use a work group only when it forms a useful cross-task cluster, and keep its name to 15
characters or fewer so the queue filter stays compact. Use title case; the soak-observation
group is `Soak Obs`.

The viewer shows readiness, work group, source reference, and prerequisite progress, with
filters for each workflow facet. Rotation and provider usage remain visible above the queue.
In flight rows have Mark done and Mark failed buttons. Each action records a structured operator
outcome against the exact attempt and frees the dispatch slot. Mark done is an explicit operator
receipt that confirms the done condition and can unblock dependents. Mark failed records that
verification is still needed. Neither button stops the provider worker.
Task contracts are edited through planning threads and the CLI; the viewer has no job editor. Actual new
launches record explicit or automatic provenance plus their attempt identity; terminal events and
structured outcome evidence inherit it. Old rows remain origin unknown. The scheduled value is
reserved for future queue-backed timer integration.

## Calendar scheduling

`bg-schedule` still owns exact clock times and general calendar recurrence. General queue
handoffs should use this skill; timed jobs remain on the Schedules tab. Weekly Bonus Drain work
has a bounded calendar window: the automatic scout may launch it only on Sunday in the configured
recurrence timezone, at most once in the week that began Saturday at midnight. Missed Sundays do
not catch up on Monday. Manual acceleration may launch a weekly task on another day, but that run
consumes the same weekly slot. Monthly work retains its 28-day elapsed cooldown.

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
All execution is disabled. Requeues and in-flight done/failed marks affect the copy. Task editing remains a CLI operation. Do not enable a scout or
refresher against it. Stop the preview process and remove only its Tailscale Serve port when
review finishes; keep the copied state until its owner chooses to discard it.
