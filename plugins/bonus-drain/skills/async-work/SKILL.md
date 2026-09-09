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

For coordinating an entire work group through PR integration, combined E2E and additional
fix rounds, use this plugin's [Long Horizon skill](../long-horizon/SKILL.md). Its goal
records and short coordination jobs use the same queue and capacity controls.
