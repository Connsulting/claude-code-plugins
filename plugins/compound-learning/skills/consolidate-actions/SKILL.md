---
name: consolidate-actions
description: Execute approved consolidation actions (merge, archive, delete, rescope, get). Use after review and user approval.
---

# Consolidate Actions

Execute consolidation operations on learnings. All destructive operations create backups.

## Usage

```bash
# Fetch full content for review
python3 [plugin-path]/skills/consolidate-actions/consolidate-actions.py get --ids=id1,id2

# Delete with backup
python3 [plugin-path]/skills/consolidate-actions/consolidate-actions.py delete --ids=id1,id2

# Move to archive
python3 [plugin-path]/skills/consolidate-actions/consolidate-actions.py archive --ids=id1,id2

# Change scope (repo -> global)
python3 [plugin-path]/skills/consolidate-actions/consolidate-actions.py rescope --id=abc123 --scope=global

# Merge multiple into one
python3 [plugin-path]/skills/consolidate-actions/consolidate-actions.py merge --ids=id1,id2,id3 --name=jwt-patterns

# Merge with explicit output directory (overrides scope logic)
python3 [plugin-path]/skills/consolidate-actions/consolidate-actions.py merge --ids=id1,id2 --name=patterns --output-dir=/absolute/path/to/repo/.projects/learnings

# Dry run (show what would happen)
python3 [plugin-path]/skills/consolidate-actions/consolidate-actions.py merge --ids=id1,id2 --name=patterns --dry-run
```

`[plugin-path]` is the absolute path to this plugin root (the directory containing `commands/` and `skills/`).

## Actions

### get
Fetch full document content for specified IDs. Use before merge/delete to review.

### delete
Remove learnings from filesystem and SQLite. Creates backup in `~/.projects/archive/learnings/YYYY-MM-DD/`.

### archive
Move learnings to archive directory (removes from active learnings but preserves content).

### rescope
Move a learning between scopes. Currently supports repo -> global only.

### merge
Combine multiple learnings into a single file. Creates new merged document, backs up and deletes originals.

Options:
- `--output-dir`: Override output directory (useful when merging global-scoped files into a repo)
- `--dry-run`: Show what would happen without executing

## Output Format

All actions return JSON:

```json
{
  "status": "success|error",
  "message": "...",
  ...action-specific fields...
}
```

## Safety

- All destructive ops create backups in `~/.projects/archive/learnings/YYYY-MM-DD/`
- Backups include original file path info
- SQLite entries are removed after successful file operations
- Merge preserves source attribution in merged document

## Notes

- IDs come from consolidate-discovery output
- Use `get` action to review full content before destructive actions
