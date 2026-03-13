# Scripts

This directory holds operator-facing Python scripts. They are not packaged as slash commands themselves, but the hooks and skills call them directly.

## `search-learnings.py`

Purpose: search the SQLite index for relevant learnings with repo-aware scoping and hybrid reranking.

Used by:

- [../hooks/auto-peek.sh](../hooks/auto-peek.sh)
- maintainers validating search behavior manually

Basic usage:

```bash
PLUGIN_DIR=/path/to/compound-learning
CLAUDE_PLUGIN_ROOT="$PLUGIN_DIR" \
python3 "$PLUGIN_DIR/scripts/search-learnings.py" "prompt caching ttl" "$PWD"
```

Arguments and flags:

| Option | Meaning |
|--------|---------|
| `query` | Search text; optional when `--keywords-json` is supplied |
| `working_dir` | Directory used to detect repo scope |
| `--peek` | Return the compact output format used by auto-peek |
| `--exclude-ids` | Comma-separated IDs to omit from results |
| `--threshold` | Override the high-confidence threshold only |
| `--max-results` | Cap returned results |
| `--keywords-json` | JSON array of keywords to search in parallel |

Behavior:

- Opens one SQLite connection per keyword query for thread safety
- Detects the current repo hierarchy with [../lib/git_utils.py](../lib/git_utils.py)
- Merges results across keywords, keeping the best distance per learning ID
- Applies FTS5 and keyword-overlap boosts before splitting results into `high_confidence` and `possibly_relevant`
- Emits JSON with statuses such as `success`, `found`, `empty`, `no_results`, or `error`

Dependencies:

- Indexed learnings already present in SQLite
- Python packages from the `SessionStart` bootstrap

## `backfill-topics.py`

Purpose: add a missing `**Topic:**` field to existing learning files based on their `**Tags:**`.

Used by:

- maintainers cleaning older learning corpora before re-indexing

Basic usage:

```bash
python3 backfill-topics.py --dry-run
python3 backfill-topics.py --apply
python3 backfill-topics.py --apply --dir /path/to/learnings
```

Arguments and flags:

| Option | Meaning |
|--------|---------|
| `--dry-run` | Default mode; prints proposed changes without writing |
| `--apply` | Writes `**Topic:**` lines into matching files |
| `--dir PATH` | Directory to scan instead of `~/.projects/learnings` |

Behavior:

- Skips files that already contain `**Topic:**`
- Skips `MANIFEST.md`
- Uses [../lib/topic_mapping.py](../lib/topic_mapping.py) for inference
- Inserts the topic after `**Type:**` when present, otherwise after the first heading

Dependencies:

- Standard library only
- Markdown learning files with `**Tags:**` metadata

## Relationship To The Index

- `search-learnings.py` reads from the SQLite index and never touches markdown files directly.
- `backfill-topics.py` updates markdown metadata only; run `/index-learnings` afterward if you want the SQLite index and manifest to reflect the new topics.
