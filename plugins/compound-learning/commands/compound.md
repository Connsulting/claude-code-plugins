---
description: Extract and compound learnings from current session to benefit future work
---

# Learning Compounding Command

Extract learnings from this conversation and store them at the appropriate scope level (repo/client/global) for future agents to benefit from.

## Usage

```
/compound
What went well: [successes]
What didn't go well: [failures, struggles]
Recommendations: [suggestions for improvement]
```

## Examples

**After successful feature implementation:**
```
/compound
Went well: TDD flow was smooth, test-impact-analyzer used proactively
Went poorly: JWT library docs unclear, spent time on cookie config
Recommendations: Document JWT cookie patterns
```

**After debugging struggle:**
```
/compound
Went poorly: Went in circles on Redux bug, sub-agent didn't bail out
Recommendations: Strengthen bail-out protocol, add Redux debugging checklist
```

**After quick fix:**
```
/compound
Quick typo fix, no issues
```

## How It Works

The command orchestrates two specialized agents:

1. **learning-extractor**: Analyzes conversation, commits, and your notes to extract typed learnings
2. **learning-writer**: Determines scope, generates/updates documentation, agents, hooks, and indexes

## What Gets Created

**Learnings are stored at the appropriate scope:**
- **Repo-specific**: `.projects/learnings/` (library gotchas, repo patterns)
- **Global**: `~/.projects/learnings/` (reusable patterns, best practices)
- **Protocol improvements**: Agent updates, CLAUDE.md enhancements
- **Security critical**: Global docs + pre-commit hooks

## Your Task

You are the orchestrator for the learning compounding process.

### Step 1: Gather Context

Collect information about the current session:
- Full conversation history
- Git commits made during session (if any)
- Current branch name
- Working directory path
- User's notes (what went well/poorly/recommendations)

### Step 2: Extract Learnings

Invoke the **learning-extractor** agent:
```markdown
Analyze this session and extract learnings.

**Session context:**
- Branch: [branch name]
- Working directory: [path]
- Commits: [list of commits with messages]

**User notes:**
[paste user's success/failure/recommendation notes]

**Conversation:**
[provide full conversation history]

Extract typed learnings (patterns, gotchas, validations, protocols, security issues).
Return structured YAML with technical details.
```

### Step 3: Scope and Generate Artifacts

Invoke the **learning-writer** agent:
```markdown
Scope and generate documentation artifacts for these learnings.

**Learnings from extractor:**
[paste YAML from learning-extractor]

**Current context:**
- Working directory: [path]
- Directory structure: [show repo/client/global hierarchy]

For each learning:
1. Determine scope (repo/client/global)
2. Choose storage location
3. Create/update documentation using templates
4. Update agents if needed
5. Create hooks if security-related
6. Index into ChromaDB

Return summary of artifacts created/updated.
```

### Step 4: Report to User

Provide a concise summary:
```markdown
Learning Compounded:

[OK] [Scope]: [What was created/updated]
[OK] [Scope]: [What was created/updated]
[WARN] [Critical findings if any]

Future agents will now:
- [How they'll access/benefit from these learnings]

[If no significant learnings:]
[INFO] No significant learnings extracted (simple task)
[OK] Workflow validated: [what worked correctly]
```

## Important Guidelines

1. **Be selective**: Not every session has meaningful learnings
2. **Scope correctly**: Store learnings at the most specific applicable level
3. **Keep CLAUDE.md minimal**: Only add critical rules, not detailed patterns
4. **Security priority**: Security learnings are CRITICAL and should be global + enforced
5. **No bloat**: Don't create docs for trivial validations

## Philosophy

Every conversation should make the next one easier. This command is your opportunity to capture what you learned so future agents don't repeat the same research, make the same mistakes, or miss the same patterns.
