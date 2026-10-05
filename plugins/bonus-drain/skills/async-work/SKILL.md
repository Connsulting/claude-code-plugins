---
name: async-work
description: Hand off autonomous tasks from planning threads into a durable work queue. Use for "queue this", "park this for later", "add dependencies", "edit my queued task", or "run this queued task". Ready work runs automatically when Bonus capacity permits; explicit starts accelerate it. Exact calendar schedules remain with bg-schedule.
---

# Async Work

Read and follow `../bonus-drain/SKILL.md` in this plugin. It owns the shared runtime,
preflight, task contract, dependency rules, and dispatch lifecycle. Read
`../bonus-drain/ASYNC_WORK.md` for the UI and new command reference.

Queueing alone does not authorize an immediate launch. Every ready task is eligible for Bonus
capacity; use an explicit start only to accelerate it. Use the same stable `bonus-drain` CLI and
queue; never create another queue for this skill name. Runtime installation remains a separate action.

A task is queueable only if it can start and can finish with no human in the loop. "Awaiting human" is not a stage; a green PR waiting only on Brian's review or merge already counts as done. Before queueing, run the global `implementable-ticket` skill on the task and pass its readiness record (contract `implementable-ticket/v1`) to `add` as `--readiness-review`; `add` refuses without it, and any contract edit needs a fresh one. See the queue-time readiness contract in `../bonus-drain/SKILL.md` for the record shape.

1. A task whose record answers no to "can it start?" or "can it finish?" is not queued. `add` refuses it with the failed question and its evidence; fix the task, split it, or leave it out.
2. Every acceptance criterion needs a `verified_by` the executor can run and an `environment`; without one the task is not finishable and is refused.
3. An authority finding resolves only by a grant Brian already gave (on the task, `--grant`) or by rewriting done-when so it no longer needs that authority. Never queue a task that waits on a person's approval, decision, or click.
4. Other findings resolve by a queued prerequisite task with its dependency edge, a rewritten done-when, or a structured check.
5. `add` evaluates every launch check before storing anything. A failing check refuses the add unless `depends_on` names a queued prerequisite expected to make it pass; a check that cannot be evaluated now refuses and asks you to retry the add.

Tasks queued before this contract are held as `review_stale`, and launchable tasks with no review as `review_missing`; neither dispatches until edited with a new record, and `bonus-drain held-report` lists them.

Translate each mechanically checkable precondition or dependency (an issue open or in a milestone, a pull request merged, a release published, a base branch present, file content on a ref, an MCP server authenticated, a Kubernetes resource present, OpenRouter credit) into a `checks` entry, and choose `done` or `merged` for each dependency edge. The MCP, Kubernetes, and OpenRouter checks hold the task without consuming an attempt and never proceed as unverified. Keep the free-text precondition only for judgment the worker must make. Workers always have default authority to fix pre-existing lint, format, or type errors in files their change touches; do not grant or precondition that. Use `bonus-drain readiness-backfill` to find older tasks that lack a review or carry a stale one.

For coordinating an entire work group through PR integration, combined E2E and additional
fix rounds, use this plugin's [Long Horizon skill](../long-horizon/SKILL.md). Its goal
records and short coordination jobs use the same queue and capacity controls.
