---
description: Consolidate duplicate, misscoped, or outdated learnings
---

# Consolidate Learnings Command

Find and consolidate learnings that are duplicated or outdated.

## Usage

```
/consolidate-learnings
```

## Your Task

You orchestrate a three-phase workflow: Discovery -> Approval -> Parallel Execution.

### Phase 1: Discovery

Run the discovery skill:

```bash
python3 [plugin-path]/skills/consolidate-discovery/consolidate-discovery.py --mode all 2>/dev/null
```

Parse the JSON output to build your report.

### Phase 2: Present Report for Approval

Present a structured report:

```
## Consolidation Report

**Summary:** X learnings can be consolidated into Y groups, Z should be deleted

---

### Merge Groups

**Group 1: [descriptive-name]**
Why: [Brief explanation of why these are duplicates/related]
Files:
- /path/to/file1.md
- /path/to/file2.md

**Group 2: [descriptive-name]**
Why: [Brief explanation]
Files:
- /path/to/file3.md
- /path/to/file4.md
- /path/to/file5.md

---

### Deletions (Outdated - Not Being Merged)

These files have outdated markers and are NOT part of any merge group above:

1. /path/to/orphan-outdated1.md
   Why: Contains "temporary" - one-time fix no longer needed

2. /path/to/orphan-outdated2.md
   Why: Contains "deprecated" - superseded by other patterns

---

Approve this plan? (You can request modifications first)
```

**Important for report:**
- Use `consolidate-actions.py get` to fetch content if needed to understand WHY files should be merged
- Group name should be descriptive (e.g., "jwt-authentication", "helm-deployment-patterns")
- **CRITICAL: Deletions must EXCLUDE files that appear in any merge group** - those get deleted automatically by the merge operation
- Only list truly orphaned outdated files in the Deletions section

### Phase 3: Parallel Execution

After user approval, launch sub-agents in parallel:

1. **One sub-agent per merge group** - Each merges its assigned files
2. **One sub-agent for deletions** - Deletes all approved outdated files

Each sub-agent runs:
```bash
# Merge sub-agent
python3 consolidate-actions.py merge --ids=id1,id2 --name=group-name

# Deletion sub-agent
python3 consolidate-actions.py delete --ids=id1,id2,id3
```

### Phase 4: Report Results

After all sub-agents complete:

```
## Consolidation Complete

Merged:
- jwt-authentication-2026-01-22.md (from 3 files)
- helm-patterns-2026-01-22.md (from 2 files)

Deleted:
- 4 outdated learnings removed

Backups at: ~/.projects/archive/learnings/2026-01-22/
```

## Key Points

- **No automatic actions** - Always get approval first
- **Parallel execution** - Launch all sub-agents at once after approval
- **Backups automatic** - All destructive actions create backups
- **Fetch content sparingly** - Only use `get` when needed to understand merge rationale
