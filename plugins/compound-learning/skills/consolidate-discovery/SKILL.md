---
name: consolidate-discovery
description: Find duplicate and outdated learnings in SQLite. Use before proposing or running consolidation actions.
---

# Consolidate Discovery

Discovers learnings that may need consolidation: duplicates and outdated content.

## Usage

```bash
python3 [plugin-path]/skills/consolidate-discovery/consolidate-discovery.py [--mode all|duplicates|outdated] [--limit N]
```

`[plugin-path]` is the absolute path to this plugin root (the directory containing `commands/` and `skills/`).

## Options

- `--mode`: Discovery type (default: all)
- `--limit`: Max items per category (default: 20)

## Output Format

Compact JSON with file basenames, paths, and IDs:

```json
{
  "status": "success",
  "total_documents": 187,
  "duplicates": [
    {
      "files": [
        {"id": "abc", "file": "jwt-auth.md", "path": "/full/path/jwt-auth.md"},
        {"id": "def", "file": "jwt-auth.md", "path": "/other/path/jwt-auth.md"}
      ],
      "count": 2
    }
  ],
  "outdated": [
    {"id": "ghi", "file": "temp-fix.md", "path": "/path/temp-fix.md", "markers": ["temporary"]}
  ],
  "summary": {
    "duplicate_clusters": 3,
    "outdated_candidates": 2,
    "limit_applied": 20
  }
}
```

## Notes

- Uses tight similarity threshold (0.25) for true duplicates
- Output is compact to avoid token bloat
- Use `python3 [plugin-path]/skills/consolidate-actions/consolidate-actions.py get --ids=...` to fetch full content when needed
