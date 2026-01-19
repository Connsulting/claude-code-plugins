---
description: Extract learnings from GitHub PR reviews and feedback
---

# PR Learnings Extraction

Extract learnings from GitHub pull request reviews, comments, and code changes.

## Usage

```bash
/pr-learnings <PR-URL-or-reference> [<PR-URL-or-reference> ...]
```

**Examples:**
```bash
# Single PR by URL
/pr-learnings https://github.com/owner/repo/pull/123

# Multiple PRs
/pr-learnings https://github.com/owner/repo/pull/123 https://github.com/owner/repo/pull/456

# PR by number (uses current repo)
/pr-learnings 123

# Multiple PR numbers
/pr-learnings 123 456 789
```

## Use Cases

- **Individual developers**: Learn from feedback on your PRs
- **Team leads**: Extract learnings from team PRs to share across the team
- **Code review insights**: Identify patterns in mistakes and improvements

## Your Task

You are the orchestrator. Your job is simple:

### Step 1: Parse PR References

Extract the PR references from the user's input:
- Full URLs: `https://github.com/owner/repo/pull/123`
- PR numbers: `123` (uses current repository)
- Multiple references separated by spaces

### Step 2: Validate GitHub CLI

Check that `gh` is available:
```bash
gh --version
```

If not installed, instruct the user to install GitHub CLI first.

### Step 3: Invoke pr-learning-extractor

Invoke the **pr-learning-extractor** agent with a minimal prompt:

```markdown
Extract learnings from GitHub PR reviews and write them to appropriate locations.

**PR References:** [space-separated list of URLs or PR numbers]
**Working directory:** [current working directory path]

You have access to the GitHub CLI (`gh`). For each PR:
1. Fetch review comments, discussions, and code changes
2. Identify 0-3 meaningful learnings per PR (be selective)
3. Write small .md files in the compound-learning format

Focus on learnings that reveal patterns, gotchas, or best practices worth remembering.
```

The pr-learning-extractor agent will:
- Use `gh pr view` to fetch PR data
- Analyze feedback for meaningful learnings
- Write learning files directly
- Report what was created

### Step 4: Report to User

Pass through the agent's report or summarize:

```
PR Learnings Extracted:

[agent output with file paths and summaries]
```

Or if no learnings:

```
No significant learnings found in the provided PR(s).
```

## What This Command Does NOT Do

To keep it focused:
- Does NOT modify the PRs
- Does NOT post comments back to GitHub
- Does NOT re-index automatically (run `/index-learnings` separately)
- Does NOT validate PR permissions (assumes you have read access)

## Philosophy

PR reviews contain valuable institutional knowledge. Extract learnings from feedback to help the team avoid similar issues in the future.
