# Compound Learning Plugin Development

## Test Harness

This plugin has a comprehensive test harness for validating search quality, indexing, and consolidation. **Run the test harness before and after any feature changes** to quantitatively verify improvements.

### Running Tests

```bash
cd plugins/compound-learning

# Fast tests only (~5s): hooks, search relevance
pytest tests/test_hooks.py tests/test_search_relevance.py -v

# Full harness (~40s): indexes real corpus, runs quality/benchmark tests
pytest tests/ -v

# Skip slow corpus tests
pytest tests/ -m "not slow" -v

# Run with stdout for benchmark tables
pytest tests/test_benchmark.py -v -s
```

### Test Categories

- `test_search_relevance.py`: Unit-level search pipeline tests with synthetic data
- `test_hooks.py`: Transcript context extraction, edge cases
- `test_lifecycle.py`: Full index/search/consolidate lifecycle against real corpus
- `test_search_quality.py`: Ground truth queries, topic coherence, false positive rejection
- `test_benchmark.py`: Performance bounds, A/B threshold/weight comparison with metrics

### A/B Testing Changes

When modifying search ranking, thresholds, or retrieval logic:

1. Run `pytest tests/test_benchmark.py -v -s` to capture baseline metrics
2. Make changes
3. Re-run and compare the printed metrics tables
4. Use `harness_utils.MetricsCollector` and `compare_metrics()` for programmatic comparison

### Sample Learnings

Developers without learnings in `~/.projects/learnings/` can still run the full harness. The fixtures automatically fall back to `tests/fixtures/sample_learnings/` which contains 10 representative learning files.

### Key Files

- `tests/conftest.py`: Shared fixtures (learning_snapshot, indexed_corpus, isolated_db)
- `tests/harness_utils.py`: MetricsCollector, run_search_pipeline, match_ids_by_filename
