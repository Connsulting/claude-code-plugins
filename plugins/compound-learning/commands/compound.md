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
- Writes files immediately (no duplicate checking)
- Reports what was created

### Step 3: Report to User

Pass through the learning-writer's report:

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
- Does NOT check for duplicate learnings
- Does NOT update CLAUDE.md
- Does NOT create hooks
- Does NOT update agents
- Does NOT re-index automatically

If you need indexing, run `/index-learnings` separately.

## Philosophy

Every conversation should make the next one easier. This command captures learnings quickly and cheaply. Quality over quantity. Small files over comprehensive docs.
