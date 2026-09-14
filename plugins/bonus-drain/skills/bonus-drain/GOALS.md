# Long Horizon goals

A goal adds durable coordination to the same Async Work queue. It does not replace
task claims, success-only dependencies, router dispatch or `/implement` pipelines.
The runtime has no new dependency or service. The existing scout tick reconciles
goal joins, queues a short coordinator task when needed, then applies ordinary
capacity policy. `goal tick` performs reconciliation only; it never calls a model.

Goals use the existing Async Work scheduler and Bonus capacity policy. An explicit
`run-now TASK` still accelerates only that eligible task. Per-provider in-flight caps
and an optional global job cap apply to ready goal turns as ordinary queue jobs.
`max_inflight` additionally caps this goal's new managed jobs at the atomic claim
boundary. Existing linked tasks keep their original scheduling and contracts.

## Create and inspect

Run the normal `doctor --json` and `status --json` preflight first. Inspect the exact
source group and each task contract. Creating a goal is an explicit authorization
to queue coordination under the supplied contract; it is not an immediate launch.
Goal/task IDs are stable. Membership is an explicit snapshot, never a title match.

Write a JSON contract file (timestamps below illustrate the shape; use the agreed
future UTC epoch for the real run):

```json
{
  "id": "example-release",
  "title": "Example release",
  "cwd": "/absolute/project",
  "outcome": "The assembled user journey works with the agreed failure paths",
  "authority": "Own isolated worktrees. Push branches and open PRs in the named project. Do not deploy, message others, or merge.",
  "acceptance": [{"id": "journey", "proof": "Run the real CLI journey with a successful input and a rejected input; retain commands and outcomes."}],
  "merge_policy": "stack",
  "deadline": 2000003600,
  "max_turns": 8,
  "max_inflight": 3,
  "coordinator": {"model": "gpt-6-astra"},
  "task_ids": ["existing-root-a", "existing-root-b"],
  "source_ref": "planning-thread-reference",
  "work_group": "Example"
}
```

`authority` must resolve scope and permitted effects before queueing. `stack` means
unmerged PRs; `merge` explicitly grants the coordinator the approved merges and must
agree with the authority text. It does not grant task drivers merge or deployment
permission. A merge goal's stored coordinator identity is what overrides the queue's
default no-merge prompt; task prose or a similarly named task does not.

The example numbers are not defaults. Choose deadline, maximum coordination turns
and concurrency for the actual authorized run. They are cumulative across rounds
and resumption. Model turns are an execution bound, not a token or dollar estimate.
`coordinator` accepts ordinary task `model`, `allowed_providers`,
`required_capabilities`, and `mcp` fields. Do not hard-code provider/account names.

```sh
bonus-drain goal create --file /private/goal.json --json
bonus-drain goal show example-release --json
bonus-drain goal list --json
bonus-drain goal tick example-release --dry-run --json
```

Existing membership is read-only: it does not edit tasks, convert their pipeline,
requeue them, infer their permissions, or alter active claims. The same task cannot
be assigned to competing goal owners. Tasks created by a goal decision are managed:
goal pause/deadline/concurrency guards apply to their future launches. Pausing never
kills a running job, and does not pause preexisting linked jobs or unrelated work.

## Record one decision

The coordinator reads the current revision and its dispatched task ID, writes a
decision file, then calls the bound decision command from its dispatch prompt.
The generic shape is:

```sh
bonus-drain goal advance example-release --turn COORDINATOR_TASK --file /private/decision.json --json
```

```json
{
  "expected_revision": 1,
  "action": "wait",
  "summary": "Two roots are ready to build",
  "tasks": [
    {"role": "implementation", "task": {
      "id": "example-fix", "title": "Fix the example path", "size": "small",
      "cwd": "/absolute/project", "goal": "Implement the identified correction",
      "constraints": "Own one isolated branch; preserve other owners",
      "done_when": "Reviewed PR, required verification and cleanup evidence are recorded at /private/example-fix/RESULT.md",
      "use_implement": true, "depends_on": []
    }}
  ],
  "wait_for": ["example-fix"]
}
```

New task fields follow `add` contracts. Roles are `implementation`, `integration`
or `acceptance`; build jobs require `use_implement: true`. Task IDs must be new.
`task_ids` may link additional existing one-off jobs without rewriting them.
All new tasks, links and the decision commit in one transaction. Invalid input
rolls everything back. Retrying the identical decision is a no-op; a conflicting
decision or stale revision is rejected. Read state after an uncertain CLI result.

Coordinator and acceptance job execution contracts are frozen and fingerprinted.
Ordinary queue edits cannot replace their goals, constraints, routing or candidate
context. Priority, size and active controls remain available without changing the
proof subject. Goal-owned failure history cannot be erased through `requeue`, and public
requeue continues to reject every managed task.

The goal runtime may internally admit bounded recovery for failed/skipped managed implementation
or integration members, including members with no queue descendants. The managed role and
GoalStore admission replace the ordinary active-dependent requirement. Admission keeps the
original task ID and every attempt, and rechecks goal authority, pause, deadline, concurrency, contract,
operation, dependency-base, and frozen-candidate guards. At most two automatic attempts are made,
after 5-minute and 30-minute backoffs; a repeated normalized reason stops early. Unknown launch
ownership, missing authority, and unavailable or divergent repository state remain held.
Coordinator and acceptance jobs do not use this recovery path: a coordinator resumes through a
fresh turn, and acceptance always uses a fresh verifier bound to the exact candidate.

After `advance`, the coordinator records its own queue job `done` using the exact
terminal command in its dispatch prompt, then ends. A `wait` decision completes
this coordination job, not the goal. Pending controller claims prevent replacement
even if a decision was already saved. The scout waits for the registered join, not
individual progress events. A failed prerequisite settles the blocked path for
coordination, while leaving the queue dependency unsatisfied. A join that is
already settled cannot be rearmed to burn more model turns.

The coordinator need not wake for each dependency launch: queued downstream tasks
become eligible on later scout ticks. Register separate joins when a PR integration
decision is actually needed. Drivers return PR/head/base, dependency heads, required
review/check outcomes, evidence paths and resource cleanup; retain their own journals.

## Candidate and acceptance

A candidate is `{"commits":{"repository-label":"FULL_COMMIT_HASH"},"runtime":{}}`.
Commit values must be full immutable hashes. For live proof, also fill `runtime`
with relevant observed build, deployment or workflow revision identities. Evidence
must identify the scheduled/live execution when that is the acceptance surface.

Create an acceptance job after its candidate exists. Add `candidate` beside `role`
and `task` in that job entry. The runtime stamps the candidate and every goal
criterion into its task context. Register its ID in `wait_for`. The verifier retains
the actual command/interaction and positive/negative outcomes with timestamps.

Its next coordinator decision reports observations:

```json
{
  "expected_revision": 5,
  "action": "complete",
  "summary": "All agreed journeys passed; cleanup is proved",
  "candidate": {"commits": {"example": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}, "runtime": {}},
  "observations": [{"criterion": "journey", "task_id": "example-acceptance", "outcome": "pass", "evidence": "/private/example-acceptance/RESULT.md"}]
}
```

Only a terminal acceptance job may supply an observation (`pass`, `fail`, or
`unavailable`); a pass additionally requires that job to be `done`. Completion
requires one passing observation per criterion on the exact final candidate,
no unfinished member jobs and no operations without receipts. The runtime validates
these links; the coordinator still inspects the actual evidence and required reviews.
An evidence path string by itself is not independent verification of its contents.
An accepted completion enters `finishing`. The next tick marks it `complete` only
after the coordinator records `done`; failed closeout pauses it for reconciliation.

A failed verifier stays failed. Record its observation in a `wait` decision and
add a fresh correction referencing the failure evidence, without depending on the
failed verifier. Wait for the fix, assemble its real head, then create fresh acceptance.
Do not delete attempts with `requeue`. An ordinary queued implementation child blocked
by an obsolete failed parent may receive an explicit, authorized dependency correction.
Acceptance contracts are frozen: replace an obsolete verifier rather than editing it.
Within the goal's authority, first `deactivate` that verifier to close future admission.
Then inspect its claims and leases in `queue --json`, followed by `runs --task ID --json`.
Only if both checks show no claim, lease or run may the coordinator use the existing
`record --task ID --kind oneoff --cycle EPOCH --status skipped --summary REASON` path
to retire it. The reason names the obsolete candidate and replacement; preserve the
original contract and skipped event. No provider is claimed for this unlaunched work.
If any launch/history exists, preserve its owner and reconcile instead. Create the
replacement with the correct immutable candidate through `goal advance`. A skipped
verifier cannot supply a passing observation. The goal never silently rewrites a
dependency or treats obsolete verification as successful.

If a user continues the same failed implementation or integration thread and it later proves
done-when, the worker uses the exact stable package CLI `recover-complete` command embedded in its
original prompt, not the configurable terminal-record adapter. GoalStore must admit the completion
against the unchanged contract and exact failed attempt. The command retains the task ID, appends
verified evidence without a new router launch or claim, and works without a scheduled recovery
projection; it atomically consumes one when present. It refuses an active or queued successor. It
refuses coordinator and acceptance completion; use the fresh-turn and fresh-verifier flows above.

## External operations and recovery

Before a coordinator merge or stack assembly, persist intent with
`goal operation ID --turn TASK --file /private/operation.json --json`:

```json
{"key":"join-1","intent":{"action":"stack","heads":["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}}
```

After the effect, supply the same key and intent with a nonempty `receipt` object
containing the resulting immutable head/PR/merge receipt. Intents and receipts are
immutable and keyed per goal. On lost responses or coordinator failure, inspect
operations in `goal show` and reconcile the actual effect before retrying. Stack
goals reject recorded merge operations. The CLI records evidence; it does not itself
perform Git or GitHub operations. An unresolved operation blocks goal completion.

Repository handoff also binds the exact canonical remote, target ref, prior target base, branch,
and parent head. A PR or receipt does not by itself prove completion or integration. A normal
merge must be present in the current exact target by result ancestry and parent-delta content;
a squash merge requires the same content equivalence because the parent head may not be an
ancestor. Until then, an explicitly unmerged parent supplies its verified head. One compatible
descendant may contain all parent heads; divergent heads require an explicitly authorized
integration member and, where applicable, its immutable operation receipt. The runtime does not
merge branches or expand the stored authority.

Explicit planning-thread controls use the current revision:

```sh
bonus-drain goal steer example-release --revision 6 --message "New decision from the user" --json
bonus-drain goal pause example-release --revision 7 --message "Hold new goal work" --json
bonus-drain goal resume example-release --revision 8 --message "Input is now available" --json
```

Steering is durable and coalesced into the active or next turn. It does not replace
a live coordinator. A pause holds future managed admission and leaves active owners
alone. A failed coordinator or one that exits without a decision pauses the goal;
positively reconcile its effects and queue claim before resuming. A heartbeat timeout
does not prove retirement. The existing router reconciliation rules still apply.
If a coordinator was paused before it ever claimed or dispatched, resume retires
that provably unlaunched task atomically and queues a fresh turn. Its old identity
and consumed turn remain in history; actual live/ambiguous owners are never replaced.

Expired bounds pause deterministically, with no extra model call. A user-authorized
resume may explicitly supply `--deadline UTC_EPOCH` and/or `--max-turns TOTAL`;
consumed turns and prior evidence remain. Workers cannot extend their own authority.

## Packaging and proof

The plugin contains the `long-horizon` skill beside `async-work` and `bonus-drain`.
Its runtime is staged by the existing installer. Installation and live cutover remain
separate operator actions. Retire any older standalone Long Horizon skill from local
discovery when installing this version; preserve old run artifacts and ownership.
Do not version-bump or reinstall the live runtime for each source iteration.

SQLite/CLI tests prove joins, atomic updates, waiting, budget admission, recovery,
and acceptance linkage. Hermetic router tests prove dispatcher mechanics. They do
not prove a real model completed `/implement`, that a remote PR merged, or that a
live assembled user journey passed. Retain those separate proof tiers for a real run.
