---
description: Run a focused performance regression check for compound-learning search
---

# Performance Regression Spotter

Run a deterministic performance check for the `compound-learning` search hot path.

## Usage

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/perf-regression-spotter.py"
```

Optional overrides:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/perf-regression-spotter.py" \
  --baseline "$CLAUDE_PLUGIN_ROOT/.claude-plugin/perf-baseline.json" \
  --iterations 8 \
  --queries-json '["jwt refresh token", "python asyncio gather"]'
```

## Exit Codes

- `0`: No regression detected
- `1`: Regression detected
- `2`: Invalid config/input or runtime error

## Output

The command prints JSON with:
- `status`: `pass`, `fail`, or `error`
- `workload`: search iteration/query settings used
- `metrics`: observed `p50`/`p95` for `total_search_ms`, `vector_search_ms`, `fts_ms`, `rerank_ms`
- `regressions`: failed checks with threshold details
- `warnings`: baseline fallback warnings (for missing metric fields)
