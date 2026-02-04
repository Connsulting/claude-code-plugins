---
name: search-learnings
description: Search accumulated learnings with distance-based relevance filtering to find patterns, gotchas, and best practices for current work
---

# Search Learnings

Queries the learning database (ChromaDB) for relevant patterns, gotchas, and best practices. Returns **tiered results** to balance precision with recall:
- **High confidence** (distance < 0.5): Strong semantic match, highly relevant
- **Possibly relevant** (distance 0.5-0.7): May be useful, review before applying

## When to Use This Skill (Use Manifest to Decide)

Check the learnings manifest (`~/.projects/learnings/MANIFEST.md`) to see what topics have available learnings. The manifest shows topic counts and sample keywords.

**Search when (manifest shows relevant topic):**
- Task start: Manifest shows topic matching your task (e.g., "authentication" when implementing login)
- Error/stuck: Manifest has topic matching error domain (e.g., "error-handling" for retry logic)
- Security: Manifest shows security topic when working on auth/credentials/validation

**High-signal triggers during execution:**
- Security-sensitive operations (auth/token/password/encryption/api-key/secret)
- Repeated errors or stuck debugging (2+ attempts at same issue)
- User explicitly references past work ("we tried X before", "remember when Y broke")
- Architectural decision points (choosing libraries, patterns, approaches)

**Skip for:**
- Manifest shows no related topics (saves tokens)
- Pure typo fixes
- Trivial cosmetic changes
- Pure research tasks with no implementation

**How to search effectively:**
- Use topic name + specific context: "authentication JWT refresh token"
- Not just the task description verbatim: "implement the login feature"
- Pattern-match keywords from manifest: if manifest shows "OAuth, refresh tokens" under authentication, use those terms

## How It Works

1. **Automatic hierarchy detection**: Walks from current directory up to home, finds all parent `.projects/learnings/` directories
2. **Scoped search**: Queries current repo + all parent repos + global learnings
3. **Tiered filtering**: Splits results into high confidence (< 0.5) and possibly relevant (0.5-0.7)
4. **Clean output**: Results beyond 0.7 distance are excluded to prevent noise

## Usage

The script accepts: `search-learnings.py "query" [working_dir] [--peek] [--exclude-ids "id1,id2"]`

**IMPORTANT**: Always pass the user's working directory (where Claude was invoked) as the second argument to ensure correct repo hierarchy detection.

### Standard Search (conversation start)
```bash
python3 search-learnings.py "JWT authentication" /home/user/projects/my-repo
```

### Peek Mode (mid-conversation)
```bash
python3 search-learnings.py "database connection errors" /home/user/projects/my-repo --peek --exclude-ids "abc123,def456"
```

**Peek mode** is designed for mid-conversation checks:
- Returns only high_confidence results (distance < 0.5)
- Skips possibly_relevant tier entirely
- Excludes already-seen IDs via `--exclude-ids`
- Returns minimal JSON: `{"status": "found", "count": N, "learnings": [...]}` or `{"status": "empty"}`
- When empty, Claude should continue silently (no "no results" message)

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

**Peek mode output (when learnings found):**
```json
{
  "status": "found",
  "count": 2,
  "learnings": [...]
}
```

**Peek mode output (when empty):**
```json
{
  "status": "empty"
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

### During Execution (Peek Mode)

Use `--peek` mode when:
- **Topic shift to manifest topic**: You start working on a topic that exists in the manifest (e.g., moving from UI work to authentication - peek for "authentication")
- **Errors in manifest topic area**: Error occurs in a domain covered by manifest (e.g., database error when manifest shows "database" topic)
- **Stuck on manifest topic**: 2+ attempts at a problem in an area the manifest covers
- **User references past**: "we tried this before", "remember when"

```markdown
Agent starts implementing database queries (manifest shows "database" topic):
1. Run peek: search-learnings "database queries" /workdir --peek --exclude-ids "ids,from,initial,search"
2. If peek finds NEW learnings: "Found additional relevant learning about X" and apply
3. If peek returns empty: Continue silently (no message needed)
```

**Key principle:** Peek when entering a topic area that the manifest shows has learnings. Cost of empty peek is near zero.

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

1. **Check manifest first**: Use `@~/.projects/learnings/MANIFEST.md` to see what topics exist
2. **Search if manifest shows relevant topic**: Don't search blindly - use manifest to decide
3. **Use topic + context in queries**: "authentication JWT refresh" not "implement login"
4. **High confidence = apply directly**: Trust results with distance < 0.5
5. **Possibly relevant = review first**: Results 0.5-0.7 may need validation
6. **Security keywords auto-trigger**: auth/token/password/encryption/api-key/secret
7. **Empty results are OK**: Report and continue with general knowledge

## Using the Manifest

The manifest (`~/.projects/learnings/MANIFEST.md`) summarizes available learnings:

```markdown
## Global Learnings (47 total, 12 corrections)

| Topic | Count | Sample Keywords |
|-------|-------|-----------------|
| authentication | 12 (3⚠️) | JWT, OAuth, refresh tokens |
| error-handling | 8 | retries, timeouts |
```

**How to use it:**
1. Before searching, check if manifest shows a relevant topic
2. Use topic name and keywords from manifest in your search query
3. If manifest shows corrections (⚠️), prioritize searching that topic - corrections are "don't do X" learnings
4. If no relevant topic exists, skip the search (saves tokens)

**Example workflow:**
```
Task: "Implement JWT authentication"
1. Check manifest -> sees "authentication | 12 (3⚠️) | JWT, OAuth, refresh tokens"
2. Search: "authentication JWT refresh token patterns"
3. Apply high-confidence results, review corrections carefully
```
