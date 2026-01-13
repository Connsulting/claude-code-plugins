---
name: search-learnings
description: Search accumulated learnings with distance-based relevance filtering to find patterns, gotchas, and best practices for current work
---

# Search Learnings

Queries the learning database (ChromaDB) for relevant patterns, gotchas, and best practices. Returns **tiered results** to balance precision with recall:
- **High confidence** (distance < 0.5): Strong semantic match, highly relevant
- **Possibly relevant** (distance 0.5-0.7): May be useful, review before applying

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
3. **Tiered filtering**: Splits results into high confidence (< 0.5) and possibly relevant (0.5-0.7)
4. **Clean output**: Results beyond 0.7 distance are excluded to prevent noise

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
- **Returns tiered results**: high_confidence (< 0.5) and possibly_relevant (0.5-0.7)
- Returns JSON with learnings or "No relevant learnings found"

## Output Format

**When learnings found (tiered):**
```json
{
  "status": "success",
  "message": "Found 2 high confidence + 1 possibly relevant learning(s)",
  "query": "JWT authentication patterns",
  "repos_searched": ["claude-settings", "parent-org"],
  "high_confidence": [
    {
      "id": "abc123...",
      "document": "Full markdown content...",
      "metadata": { "scope": "global", "repo": "", ... },
      "distance": 0.23
    }
  ],
  "possibly_relevant": [
    {
      "id": "def456...",
      "document": "Related content...",
      "metadata": { "scope": "repo", "repo": "my-project", ... },
      "distance": 0.58
    }
  ]
}
```

**When no relevant learnings:**
```json
{
  "status": "no_results",
  "message": "No relevant learnings found (searched 10 candidates, none met distance < 0.7 threshold)",
  "query": "JWT authentication patterns",
  "repos_searched": ["claude-settings"],
  "high_confidence": [],
  "possibly_relevant": []
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
2. Receives tiered results
3. If high_confidence results found:
   - Present these first, apply directly
4. If only possibly_relevant results:
   - Review before applying (may not be exact match)
   - Mention to user: "Found possibly relevant learning about X"
5. If no results:
   - Report "No relevant learnings found"
   - Proceed with general knowledge
```

### During Execution (High-Signal Triggers)

```markdown
Agent working on task, encounters security keyword "API key":
1. Auto-triggers /search-learnings with query: "API key storage security"
2. Receives security-related learnings (if any)
3. Applies learnings before proceeding
```

## Key Benefits

1. **Balanced precision/recall**: Tiered results ensure you don't miss borderline-relevant learnings
2. **Clear confidence levels**: Know which learnings to trust immediately vs. review first
3. **Hierarchical scoping**: Searches current repo + all parent repos + global
4. **No noise**: Results beyond 0.7 distance excluded entirely

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
2. **High confidence = apply directly**: Trust results with distance < 0.5
3. **Possibly relevant = review first**: Results 0.5-0.7 may need validation
4. **Security keywords auto-trigger**: auth/token/password/encryption/api-key/secret
5. **Empty results are OK**: Report and continue with general knowledge
