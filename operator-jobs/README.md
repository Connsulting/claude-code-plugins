# operator-jobs

Job definitions for recurring bonus-drain work on a specific operator's machine.

These are **not** part of any plugin payload. `plugins/bonus-drain/` is contract-tested to
contain no machine-specific paths (`tests/test_bonus_drain_package.py`, `PERSONAL_TEXT`), and
these prompts deliberately carry absolute paths, a report directory, and a Big Plan viewer
host — they describe one operator's setup, not the shipped product. They live here so the repo
that owns bonus-drain also owns the jobs that exercise it, without leaking that setup to
everyone who installs the plugin.

The bonus-drain queue references a prompt by absolute path (`tasks.goal` /
`tasks.precondition`), so a job here is wired up by pointing its task row at this checkout.

| prompt | task id | cadence |
|---|---|---|
| `bonus-drain-review.prompt` | `bonus-drain-review` | weekly, P5 |
