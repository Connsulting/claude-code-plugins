---
description: Extract and compound learnings from current session to benefit future work
---

# Learning Compounding Command

Extract learnings from this conversation and store them for future agents to benefit from.

## Usage

```
/compound
What went well: [successes]
What didn't go well: [failures, struggles]
Recommendations: [suggestions for improvement]
```

## Examples

**After debugging struggle:**
```
/compound
Went poorly: JWT library docs unclear, spent time on cookie config
Recommendations: Document JWT cookie patterns
```

**After quick fix:**
```
/compound
Quick typo fix, no issues
```

## Your Task

You are the orchestrator. Your job is simple:

### Step 1: Gather Minimal Context

Collect:
- Working directory path
- User's notes from the /compound input

Do NOT gather commits, branch names, or other metadata unless the user mentioned them.

### Step 2: Invoke learning-writer

Invoke the **learning-writer** agent with a minimal prompt:

```markdown
Extract learnings from this conversation and write them to appropriate locations.

**Working directory:** [path]
**User notes:** [what user provided]

You have access to the full conversation. Identify 0-3 meaningful learnings and write small .md files. Be selective.
```

The learning-writer agent:
- Already sees the full conversation (no need to pass it)
- Extracts learnings directly (no YAML intermediate)
- Writes files immediately, one scope per learning
- Reports what was created

### Step 3: Dedupe each written file

The learning-writer never checks for duplicates itself, because a prose rule telling it to is the design that already failed. For every path it reports as created, run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dedupe-on-write.py" <path> --json
```

Read the `action` field. `absorbed` means the content was merged into the existing learning named in `into` and the new file is gone; `kept` means it stands on its own. Anything else, including a non-zero exit, is a fail-open: leave the file alone and carry on.

This runs the same script as the `hooks/extract-learnings.sh` loop but is NOT equivalent to it. There the script is a shell step the model cannot skip; here it is an instruction to a model to run a script, which is the shape `dedupe-on-write.py`'s own docstring names as the design that already failed. So on this path dedupe holds only as far as the instruction is followed, and a near-duplicate survives as its own file until a later consolidation pass whenever it is not.

### Step 4: Report to User

Pass through the learning-writer's report, plus one line per file saying whether it was kept or absorbed:

```
Learning Compounded:

[learning-writer output]
```

Or if no learnings:

```
No significant learnings extracted from this session.
```

## What This Command Does NOT Do

To keep token usage minimal:
- Does NOT ask a model to adjudicate duplicates (Step 3 is one vector query, no LLM call)
- Does NOT update CLAUDE.md
- Does NOT create hooks
- Does NOT update agents
- Does NOT re-index automatically

If you need indexing, run `/index-learnings` separately.

## Philosophy

Every conversation should make the next one easier. This command captures learnings quickly and cheaply. Quality over quantity. Small files over comprehensive docs.
