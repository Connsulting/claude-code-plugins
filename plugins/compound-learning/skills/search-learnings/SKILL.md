---
name: search-learnings
description: Search learnings with relevance filtering
---

# Search Learnings

Queries ChromaDB for relevant learnings. Returns tiered results:
- **High confidence** (distance < 0.5): Strong match
- **Possibly relevant** (0.5-0.7): Review before applying

## Usage

```bash
# Standard search
python3 search-learnings.py "query" /working/dir

# Peek mode (mid-conversation, excludes seen IDs)
python3 search-learnings.py "query" /working/dir --peek --exclude-ids "id1,id2"
```

## When to Search

Check manifest (`~/.projects/learnings/MANIFEST.md`) to see what topics exist.

**Search when:**
- Task involves a topic in the manifest (authentication, testing, etc.)
- Error occurs in a manifest topic area
- User references past work

**Skip when:**
- Manifest shows no related topics
- Trivial changes

**Peek when:**
- You shift to a new topic that's in the manifest
- Stuck on a problem in a manifest topic area

## Output

```json
{
  "status": "success",
  "high_confidence": [...],
  "possibly_relevant": [...]
}
```

Peek mode returns `{"status": "found", "learnings": [...]}` or `{"status": "empty"}`.

## Key Rules

1. Check manifest first to see if relevant topic exists
2. Use topic + context in queries: "authentication JWT refresh" not "implement login"
3. High confidence = apply directly
4. Possibly relevant = review first
5. Empty peek = continue silently
