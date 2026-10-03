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

Before queueing, run the mandatory readiness review from `../bonus-drain/SKILL.md`, with Brian present, and store it with `--readiness-review`; `add` refuses without it, and any contract edit needs a fresh one:

1. Read the source issue, every ADR it cites, and the AGENTS.md and CLAUDE.md files governing the paths the work will likely touch.
2. Check done-when against the authority the task will have for contradictions: merge, release, external infrastructure, sacred paths, contract freezes.
3. Check that each acceptance criterion is feasible, including vendor capabilities.
4. Enumerate external dependencies: credentials, provider credit, MCP auth, cluster resources.
5. Resolve each finding to exactly one of: a grant obtained from Brian now (`--grant`), a prerequisite task plus dependency edge, a rewritten done-when, or a structured check.

Translate each mechanically checkable precondition or dependency (an issue open or in a milestone, a pull request merged, a release published, a base branch present, file content on a ref, an MCP server authenticated, a Kubernetes resource present, OpenRouter credit) into a `checks` entry, and choose `done` or `merged` for each dependency edge. The MCP, Kubernetes, and OpenRouter checks hold the task without consuming an attempt and never proceed as unverified. Keep the free-text precondition only for judgment the worker must make. Workers always have default authority to fix pre-existing lint, format, or type errors in files their change touches; do not grant or precondition that. Use `bonus-drain readiness-backfill` to find older tasks that lack a review.

For coordinating an entire work group through PR integration, combined E2E and additional
fix rounds, use this plugin's [Long Horizon skill](../long-horizon/SKILL.md). Its goal
records and short coordination jobs use the same queue and capacity controls.
