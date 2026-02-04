---
name: index-learnings
description: Index all learning markdown files into ChromaDB and generate topic manifest
---

# Index Learnings

Discovers and indexes all learning markdown files into ChromaDB for search. Also generates a **manifest** summarizing learnings by topic for CLAUDE.md integration.

## Usage

```bash
# Full indexing + manifest generation
/index-learnings

# Rebuild manifest only (from existing ChromaDB data)
python3 index-learnings.py --rebuild-manifest
```

## How It Works

When invoked via `Skill(skill="compound-learning:index-learnings")`, the skill:

1. Discovers all `.md` files in learnings directories
2. Extracts metadata: scope, tags, category, topic, keywords
3. Indexes documents into ChromaDB with enriched metadata
4. Generates manifest files summarizing learnings by topic

## Manifest Generation

The indexer generates manifest files that summarize available learnings by topic:

**Output locations:**
- `~/.projects/learnings/MANIFEST.md` - Global manifest (all learnings)
- `[repo]/.projects/learnings/MANIFEST.md` - Repo-specific manifests

**Manifest format:**
```markdown
# Learnings Manifest
Generated: 2026-02-03T14:30:00Z

## Global Learnings (47 total, 12 corrections)

| Topic | Count | Sample Keywords |
|-------|-------|-----------------|
| authentication | 12 (3⚠️) | JWT, OAuth, refresh tokens, session |
| error-handling | 8 | retries, timeouts, graceful degradation |
| testing | 7 | mocks, fixtures, integration |
| performance | 6 | caching, N+1, lazy loading |
| other | 2 | |

## Repo Learnings: my-project (23 total)

| Topic | Count | Sample Keywords |
|-------|-------|-----------------|
| api-integration | 9 | GitHub, Slack, webhook |
| security | 5 | credentials, validation |
| other | 2 | |
```

**Topic detection priority:**
1. Explicit `**Topic:**` field in the learning file
2. Automatic detection from content keywords
3. Fallback to "other"

**Correction flagging:**
- Learnings containing "don't", "never", "avoid", "mistake", "gotcha", etc. are flagged
- Count shown as `12 (3⚠️)` where 3 are corrections

## Output Format

**Success:**
```
Loading configuration...
Connecting to ChromaDB...
[OK] Connected to ChromaDB at localhost:8000

Discovering learning files...
Found 47 learning files:
  Global: 25 files
  Repos:
    - my-project: 15 files
    - shared-lib: 7 files

Indexing into ChromaDB...
  Indexed 10/47...
  Indexed 20/47...

[OK] Indexing complete!
  Successfully indexed: 47
  Errors: 0

Total documents in ChromaDB: 47

Generating manifest files...
  Generated: ~/.projects/learnings/MANIFEST.md
  Generated: ~/my-project/.projects/learnings/MANIFEST.md
  Generated: ~/shared-lib/.projects/learnings/MANIFEST.md

[OK] Generated 3 manifest file(s)
```

**ChromaDB not running:**
```
[ERROR] Failed to connect to ChromaDB: ...
  Make sure ChromaDB is running: /start-learning-db
```

## Adding Topic to Learnings

You can explicitly set a topic in your learning files:

```markdown
# JWT Cookie Storage Best Practice

**Type:** pattern
**Tags:** jwt, authentication, cookies
**Topic:** authentication

## Problem
...
```

If no `**Topic:**` is specified, the indexer auto-detects based on content keywords.

## Notes

- Requires ChromaDB to be running (use /start-learning-db first)
- Discovers files from ~/.projects/learnings/ (global) and ~/**/.projects/learnings/ (repo)
- Keywords in manifest come from explicit `**Tags:**` field in each learning (written by Claude during extraction)
- Detects correction-type learnings ("don't do X") for manifest flagging
- Safe to run multiple times (uses upsert)
- Use `--rebuild-manifest` to regenerate manifests without re-indexing files
