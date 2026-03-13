# Skills

This directory contains the packaged skills that ship with the plugin on the current `main` branch.

## Skill Index

| Skill | Files | When It Is Used | Purpose |
|-------|-------|-----------------|---------|
| `index-learnings` | [index-learnings/SKILL.md](index-learnings/SKILL.md), [index-learnings/index-learnings.py](index-learnings/index-learnings.py) | `/index-learnings`, plus incremental indexing from [../hooks/extract-learnings.sh](../hooks/extract-learnings.sh) | Discover markdown learning files, upsert them into SQLite, prune orphaned rows, and regenerate `MANIFEST.md` |
| `consolidate-discovery` | [consolidate-discovery/SKILL.md](consolidate-discovery/SKILL.md), [consolidate-discovery/consolidate-discovery.py](consolidate-discovery/consolidate-discovery.py) | Phase 1 of `/consolidate-learnings` | Find duplicate clusters and outdated candidates, returning compact JSON |
| `consolidate-actions` | [consolidate-actions/SKILL.md](consolidate-actions/SKILL.md), [consolidate-actions/consolidate-actions.py](consolidate-actions/consolidate-actions.py) | Phase 3 of `/consolidate-learnings` or manual maintenance | Execute merge, archive, delete, rescope, and get actions with backups |

## Notes

- There is no `search-learnings` skill directory on the current `main` branch. Search is implemented as [../scripts/search-learnings.py](../scripts/search-learnings.py) and is called directly by [../hooks/auto-peek.sh](../hooks/auto-peek.sh).
- Skill metadata lives in each subdirectory's `SKILL.md`; the Python entrypoint next to it performs the actual work.
- If you add or remove skills here, update [../README.md](../README.md) so the top-level plugin docs stay aligned with the packaged surface area.
