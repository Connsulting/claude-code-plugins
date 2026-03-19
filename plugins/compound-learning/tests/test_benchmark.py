"""
Benchmark and A/B comparison tests for compound-learning search.

These tests exercise the full indexed corpus (~258 learnings) to measure
indexing performance, search latency, and compare search configurations.
All tests are marked slow since they require a fully indexed corpus.
"""

import statistics
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from harness_utils import MetricsCollector, compare_metrics, count_indexed, match_ids_by_filename, run_search_pipeline

PLUGIN_ROOT = Path(__file__).parent.parent

# Representative queries used across multiple tests
LATENCY_QUERIES = [
    "kubernetes pod scheduling",
    "docker base image security",
    "authentication oauth sso",
    "prometheus alerting",
    "git rebase workflow",
    "python asyncio patterns",
    "cicd pipeline github actions",
    "dependency management lock files",
    "grafana dashboard monitoring",
    "api integration testing",
]

# Ground truth: query -> list of expected filename substrings
GROUND_TRUTH: Dict[str, List[str]] = {
    "kubernetes pod scheduling": ["kubernetes", "k8s", "pod", "karpenter", "eks"],
    "docker base image security": ["docker", "base-image", "container", "security"],
    "authentication oauth sso": ["auth", "oauth", "sso", "cognito", "jwt"],
    "prometheus alerting": ["prometheus", "promql", "alerting", "alert"],
    "git rebase workflow": ["git", "rebase", "branch", "merge"],
}


# ---------------------------------------------------------------------------
# Test 1: Indexing performance
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_indexing_performance(indexed_corpus: Any) -> None:
    """Verify indexing completes within 120 seconds and print summary."""
    _config, conn, stats = indexed_corpus

    elapsed = stats["elapsed_seconds"]
    total = stats["total_files"]
    indexed = stats["indexed_count"]
    failed = stats["failed_count"]
    docs_per_sec = indexed / elapsed if elapsed > 0 else 0

    print(f"\n{'Indexing Performance':=^60}")
    print(f"  Total files:    {total}")
    print(f"  Indexed:        {indexed}")
    print(f"  Failed:         {failed}")
    print(f"  Elapsed:        {elapsed:.2f}s")
    print(f"  Docs/second:    {docs_per_sec:.2f}")
    print(f"  DB doc count:   {count_indexed(conn)}")
    print("=" * 60)

    assert elapsed < 120, f"Indexing took {elapsed:.2f}s, expected < 120s"


# ---------------------------------------------------------------------------
# Test 2: Search latency
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_latency(indexed_corpus: Any) -> None:
    """Measure search latency across 10 representative queries. Assert mean < 500ms."""
    config, conn, _stats = indexed_corpus
    latencies: List[float] = []

    for query in LATENCY_QUERIES:
        start = time.perf_counter()
        run_search_pipeline(config, conn, query)
        elapsed_ms = (time.perf_counter() - start) * 1000
        latencies.append(elapsed_ms)

    sorted_latencies = sorted(latencies)
    mean_ms = statistics.mean(latencies)
    median_ms = statistics.median(latencies)
    min_ms = min(latencies)
    max_ms = max(latencies)
    p95_ms = sorted_latencies[int(len(sorted_latencies) * 0.95)]

    print(f"\n{'Search Latency (ms)':=^60}")
    print(f"  {'Query':<40} {'Latency':>10}")
    print(f"  {'-' * 40} {'-' * 10}")
    for query, lat in zip(LATENCY_QUERIES, latencies):
        print(f"  {query:<40} {lat:>10.1f}")
    print(f"  {'-' * 40} {'-' * 10}")
    print(f"  {'Min':<40} {min_ms:>10.1f}")
    print(f"  {'Max':<40} {max_ms:>10.1f}")
    print(f"  {'Mean':<40} {mean_ms:>10.1f}")
    print(f"  {'Median':<40} {median_ms:>10.1f}")
    print(f"  {'P95':<40} {p95_ms:>10.1f}")
    print("=" * 60)

    assert mean_ms < 500, f"Mean search latency {mean_ms:.1f}ms exceeds 500ms threshold"


# ---------------------------------------------------------------------------
# Test 3: A/B threshold comparison
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_ab_threshold_comparison(indexed_corpus: Any) -> None:
    """Compare two threshold configurations across ground-truth queries."""
    config, conn, _stats = indexed_corpus

    configs = {
        "A (high=0.35, possible=0.50)": {"high_threshold": 0.35, "possible_threshold": 0.50},
        "B (high=0.45, possible=0.60)": {"high_threshold": 0.45, "possible_threshold": 0.60},
    }

    collectors: Dict[str, MetricsCollector] = {}

    for label, overrides in configs.items():
        collector = MetricsCollector()
        for query, expected_subs in GROUND_TRUTH.items():
            results = run_search_pipeline(
                config, conn, query,
                high_threshold=overrides["high_threshold"],
                possible_threshold=overrides["possible_threshold"],
            )
            all_results = results["all_results"]
            actual_ids = [r["id"] for r in all_results]
            expected_ids = match_ids_by_filename(all_results, expected_subs)
            collector.add_result(query, expected_ids, actual_ids)
        collectors[label] = collector

    summaries = {label: c.summary() for label, c in collectors.items()}
    labels = list(summaries.keys())
    diff = compare_metrics(summaries[labels[0]], summaries[labels[1]])

    print(f"\n{'A/B Threshold Comparison':=^60}")
    print(f"  {'Metric':<20} {'Config A':>12} {'Config B':>12} {'Delta':>10} {'% Change':>10}")
    print(f"  {'-' * 20} {'-' * 12} {'-' * 12} {'-' * 10} {'-' * 10}")
    for metric in ("precision_at_5", "recall", "f1"):
        d = diff[metric]
        print(
            f"  {metric:<20} {d['baseline']:>12.4f} {d['candidate']:>12.4f} "
            f"{d['delta']:>+10.4f} {d['pct_change']:>+9.2f}%"
        )
    print("=" * 60)

    # Both configs must complete without error (assertions above are implicit)
    for label, summary in summaries.items():
        assert summary["total_queries"] == len(GROUND_TRUTH), (
            f"Config {label} processed {summary['total_queries']} queries, expected {len(GROUND_TRUTH)}"
        )


# ---------------------------------------------------------------------------
# Test 4: A/B keyword weight comparison
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_ab_keyword_weight_comparison(indexed_corpus: Any) -> None:
    """Compare two keyword weight configurations across ground-truth queries."""
    config, conn, _stats = indexed_corpus

    configs = {
        "A (keyword_weight=0.5)": {"keyword_weight": 0.5},
        "B (keyword_weight=0.8)": {"keyword_weight": 0.8},
    }

    collectors: Dict[str, MetricsCollector] = {}

    for label, overrides in configs.items():
        collector = MetricsCollector()
        for query, expected_subs in GROUND_TRUTH.items():
            results = run_search_pipeline(
                config, conn, query,
                keyword_weight=overrides["keyword_weight"],
            )
            all_results = results["all_results"]
            actual_ids = [r["id"] for r in all_results]
            expected_ids = match_ids_by_filename(all_results, expected_subs)
            collector.add_result(query, expected_ids, actual_ids)
        collectors[label] = collector

    summaries = {label: c.summary() for label, c in collectors.items()}
    labels = list(summaries.keys())
    diff = compare_metrics(summaries[labels[0]], summaries[labels[1]])

    print(f"\n{'A/B Keyword Weight Comparison':=^60}")
    print(f"  {'Metric':<20} {'Config A':>12} {'Config B':>12} {'Delta':>10} {'% Change':>10}")
    print(f"  {'-' * 20} {'-' * 12} {'-' * 12} {'-' * 10} {'-' * 10}")
    for metric in ("precision_at_5", "recall", "f1"):
        d = diff[metric]
        print(
            f"  {metric:<20} {d['baseline']:>12.4f} {d['candidate']:>12.4f} "
            f"{d['delta']:>+10.4f} {d['pct_change']:>+9.2f}%"
        )
    print("=" * 60)

    for label, summary in summaries.items():
        assert summary["total_queries"] == len(GROUND_TRUTH), (
            f"Config {label} processed {summary['total_queries']} queries, expected {len(GROUND_TRUTH)}"
        )


# ---------------------------------------------------------------------------
# Test 5: Metrics collection summary (baseline)
# ---------------------------------------------------------------------------

# Extended ground truth for all 10 latency queries
FULL_GROUND_TRUTH: Dict[str, List[str]] = {
    "kubernetes pod scheduling": ["kubernetes", "k8s", "pod", "karpenter", "eks"],
    "docker base image security": ["docker", "base-image", "container", "security"],
    "authentication oauth sso": ["auth", "oauth", "sso", "cognito", "jwt"],
    "prometheus alerting": ["prometheus", "promql", "alerting", "alert"],
    "git rebase workflow": ["git", "rebase", "branch", "merge"],
    "python asyncio patterns": ["python", "asyncio", "async", "concurrency"],
    "cicd pipeline github actions": ["cicd", "ci-cd", "pipeline", "github-actions", "workflow"],
    "dependency management lock files": ["dependency", "lock-file", "lock-files", "npm", "poetry"],
    "grafana dashboard monitoring": ["grafana", "dashboard", "monitoring", "observability"],
    "api integration testing": ["api", "integration", "testing", "test"],
}


@pytest.mark.slow
def test_metrics_collection_summary(indexed_corpus: Any) -> None:
    """Run all 10 queries and print full metrics summary to establish a baseline."""
    config, conn, _stats = indexed_corpus
    collector = MetricsCollector()

    for query, expected_subs in FULL_GROUND_TRUTH.items():
        results = run_search_pipeline(config, conn, query)
        all_results = results["all_results"]
        actual_ids = [r["id"] for r in all_results]
        expected_ids = match_ids_by_filename(all_results, expected_subs)
        collector.add_result(query, expected_ids, actual_ids)

    summary = collector.summary()
    per_query = collector.per_query_stats()

    print(f"\n{'Baseline Metrics Summary':=^70}")
    print(f"  {'Query':<40} {'P@5':>8} {'Recall':>8} {'Expected':>10} {'Actual':>8}")
    print(f"  {'-' * 40} {'-' * 8} {'-' * 8} {'-' * 10} {'-' * 8}")
    for pq in per_query:
        print(
            f"  {pq['query']:<40} {pq['precision_at_5']:>8.4f} {pq['recall']:>8.4f} "
            f"{pq['expected_count']:>10} {pq['actual_count']:>8}"
        )
    print(f"  {'-' * 40} {'-' * 8} {'-' * 8} {'-' * 10} {'-' * 8}")
    print(f"  {'AGGREGATE':<40} {summary['precision_at_5']:>8.4f} {summary['recall']:>8.4f}")
    print(f"  {'F1':<40} {summary['f1']:>8.4f}")
    print("=" * 70)

    assert summary["total_queries"] == len(FULL_GROUND_TRUTH)
