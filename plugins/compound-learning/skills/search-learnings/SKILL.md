---
name: search-learnings
description: Search accumulated learnings with distance-based relevance filtering to find patterns, gotchas, and best practices for current work
---

# Search Learnings

Queries the learning database (ChromaDB) for relevant patterns, gotchas, and best practices. Filters results by relevance (distance < 0.5) **before** outputting to avoid polluting context with irrelevant results.

## When to Use This Skill

**MANDATORY at conversation start** for non-trivial work:
- User provides implementation task (NOT simple typos/cosmetic changes)
- User mentions specific technology/library/framework
- Any non-trivial implementation or exploration task

**High-signal triggers during execution:**
- Security-sensitive operations (auth/token/password/encryption/api-key/secret)
- Repeated errors or stuck debugging (2+ attempts at same issue)
- User explicitly references past work ("we tried X before", "remember when Y broke")
- Architectural decision points (choosing libraries, patterns, approaches)

**Skip for:**
- Pure typo fixes
- Trivial cosmetic changes
- Pure research tasks with no implementation

## How It Works

1. **Automatic hierarchy detection**: Walks from current directory up to home, finds all parent `.projects/learnings/` directories
2. **Scoped search**: Queries current repo + all parent repos + global learnings
3. **Distance filtering**: Only returns results with distance < 0.5 (done in script, not in conversation)
4. **Clean output**: No context pollution from irrelevant results

## Usage

The script accepts: `search-learnings.py "query" [working_dir]`

**IMPORTANT**: Always pass the user's working directory (where Claude was invoked) as the second argument to ensure correct repo hierarchy detection.

Example:
```bash
python3 search-learnings.py "JWT authentication" /home/user/projects/my-repo
```

The skill:
- Connects to ChromaDB at localhost:8000
- Detects repo hierarchy from the provided working directory
- Queries with proper scope filter
- **Filters by distance < 0.5 BEFORE outputting**
- Returns JSON with relevant learnings or "No relevant learnings found"

## Output Format

**When relevant learnings found:**
```json
{
  "status": "success",
  "message": "Found 3 relevant learning(s)",
  "query": "JWT authentication patterns",
  "repos_searched": ["claude-settings", "parent-org"],
  "results": [
    {
      "id": "abc123...",
      "document": "Full markdown content...",
      "metadata": {
        "scope": "global",
        "repo": "",
        "file_path": "~/.projects/learnings/auth-patterns.md",
        "category": "security",
        "tags": "jwt,auth,security",
        "summary": "JWT tokens must use httpOnly cookies..."
      },
      "distance": 0.23
    }
  ]
}
```

**When no relevant learnings:**
```json
{
  "status": "no_results",
  "message": "No relevant learnings found (searched 5 candidates, none met distance < 0.5 threshold)",
  "query": "JWT authentication patterns",
  "repos_searched": ["claude-settings"],
  "results": []
}
```

**On error:**
```json
{
  "status": "error",
  "message": "Error message here",
  "query": "JWT authentication patterns"
}
```

## Integration with Workflow

### At Conversation Start (Mandatory for Non-Trivial Work)

```markdown
User: "Implement JWT authentication for the API"

Agent:
1. Immediately invokes /search-learnings skill with query: "JWT authentication API implementation"
2. Receives filtered results (only distance < 0.5)
3. If relevant results found:
   - Presents key learnings to user
   - Incorporates into implementation approach
   - Passes context to sub-agents if delegating
4. If no results:
   - Reports "No relevant learnings found"
   - Proceeds with general knowledge
```

### During Execution (High-Signal Triggers)

```markdown
Agent working on task, encounters security keyword "API key":
1. Auto-triggers /search-learnings with query: "API key storage security"
2. Receives security-related learnings (if any)
3. Applies learnings before proceeding
```

## Key Benefits

1. **No context pollution**: Filtering happens in Python script before results reach conversation
2. **Efficient**: Only fetches top 5 results, filters by threshold
3. **Hierarchical scoping**: Searches current repo + all parent repos + global
4. **Clean feedback**: Clear "no results" message vs polluting with irrelevant matches

## Error Handling

If ChromaDB is not running:
```
/start-learning-db
```

If no learnings exist yet:
```
/index-learnings
```

## Critical Rules

1. **Always search at conversation start** for non-trivial work
2. **Trust the threshold**: If script returns no results, that's valid information
3. **Don't over-filter**: Show all results that script returns (it already filtered)
4. **Security keywords auto-trigger**: auth/token/password/encryption/api-key/secret
5. **Empty results are OK**: Report and continue with general knowledge
