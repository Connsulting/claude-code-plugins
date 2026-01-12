---
name: index-learnings
description: Index all learning markdown files into ChromaDB
---

# Index Learnings

Discovers and indexes all learning markdown files into ChromaDB for search.

## Usage

```
/index-learnings
```

## How It Works

When invoked via `Skill(skill="compound-learning:index-learnings")`, the skill automatically executes and outputs indexing progress.

## Output Format

**Success:**
```
Learning files indexed successfully

Found and indexed:
- Global:  X files
- Repo:    Y files
Total:     N documents in ChromaDB
```

**ChromaDB not running:**
```
ChromaDB is not running

Start it first with: /start-learning-db
```

## Notes

- Requires ChromaDB to be running (use /start-learning-db first)
- Discovers files from ~/.projects/learnings/ (global) and ~/**/.projects/learnings/ (repo)
- Automatically extracts tags and categories from content
- Safe to run multiple times (uses upsert)
