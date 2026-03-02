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
- `warnings`: compatibility/fallback warnings (legacy schema, missing fields, and defaults applied)

## Baseline Compatibility

Older baseline JSON files are accepted and normalized with warnings:
- Top-level metric blocks (without a `metrics` wrapper)
- Metric aliases (`p50_ms`, `p95_ms`, `max_regression_percent`, `max_ms`)
- Top-level workload keys (`iterations`, `warmup_runs`, `queries`)

Missing fields fall back to safe defaults instead of failing fast.
