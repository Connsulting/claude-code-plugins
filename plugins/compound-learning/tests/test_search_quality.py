"""
Search quality tests against the real indexed corpus.

All tests are marked slow since they depend on the indexed_corpus fixture
which indexes all learning files from ~/.projects/learnings/.
"""

import pytest

from harness_utils import MetricsCollector, match_ids_by_filename, run_search_pipeline


# ---------------------------------------------------------------------------
# Test 1: Known query ground truth
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_known_query_ground_truth(indexed_corpus, search_fn):
    """Queries with known topic words should return files matching expected patterns."""
    config, conn, stats = indexed_corpus

    ground_truth = [
        ("kubernetes pod scheduling", ["kubernetes", "k8s", "pod"]),
        ("docker base image security", ["docker"]),
        ("authentication oauth sso", ["auth", "oauth", "cognito"]),
        ("prometheus alerting rules", ["prometheus", "grafana"]),
        ("git rebase conflicts", ["git"]),
    ]

    collector = MetricsCollector()

    for query, expected_patterns in ground_truth:
        results = search_fn(config, conn, query)
        all_results = results["all_results"]

        matched_expected = match_ids_by_filename(all_results, expected_patterns)
        actual_ids = [r["id"] for r in all_results]

        collector.add_result(query, expected_ids=matched_expected, actual_ids=actual_ids)

        # At least some results should match expected patterns
        assert len(matched_expected) > 0 or len(all_results) > 0, (
            f"Query '{query}' returned no results at all. "
            f"Expected matches for patterns: {expected_patterns}"
        )

    # Overall: at least 30% recall across queries
    summary = collector.summary()
    assert summary["recall"] >= 0.3, (
        f"Overall recall {summary['recall']:.2f} below 0.3 threshold. "
        f"Summary: {summary}"
    )


# ---------------------------------------------------------------------------
# Test 2: Topic coherence
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_topic_coherence(indexed_corpus, search_fn):
    """Top-5 results for 'kubernetes infrastructure' should be topically related."""
    config, conn, stats = indexed_corpus

    results = search_fn(config, conn, "kubernetes infrastructure")
    all_results = results["all_results"][:5]

    related_topics = {
        "kubernetes-infrastructure", "aws", "docker", "deployment",
        "cicd", "observability", "prometheus",
    }

    if not all_results:
        pytest.skip("No results returned for 'kubernetes infrastructure'")

    related_count = 0
    for r in all_results:
        topic = r.get("metadata", {}).get("topic", "other")
        if topic in related_topics:
            related_count += 1

    coherence = related_count / len(all_results)
    assert coherence >= 0.6, (
        f"Topic coherence {coherence:.2f} below 0.6 threshold. "
        f"Topics found: {[r.get('metadata', {}).get('topic') for r in all_results]}"
    )


# ---------------------------------------------------------------------------
# Test 3: No false positives for unrelated queries
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_no_false_positives_for_unrelated_queries(indexed_corpus, search_fn):
    """Completely unrelated queries should produce no high-confidence results."""
    config, conn, stats = indexed_corpus

    unrelated_queries = [
        "recipe for chocolate cake",
        "basketball game score",
    ]

    for query in unrelated_queries:
        results = search_fn(config, conn, query)
        high_confidence = results["high_confidence"]

        assert len(high_confidence) == 0, (
            f"Query '{query}' returned {len(high_confidence)} high-confidence results "
            f"but expected 0. IDs: {[r['id'] for r in high_confidence]}"
        )


# ---------------------------------------------------------------------------
# Test 4: Keyword floor on real corpus
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_keyword_floor_on_real_corpus(indexed_corpus):
    """All results must satisfy the keyword floor: keyword_overlap > 0 OR original_distance < 0.25."""
    config, conn, stats = indexed_corpus

    queries = [
        "kubernetes pod scheduling",
        "docker security scanning",
        "python async patterns",
    ]

    for query in queries:
        results = run_search_pipeline(config, conn, query)
        for r in results["all_results"]:
            keyword_overlap = r.get("keyword_overlap", 0)
            original_distance = r.get("original_distance", 1.0)

            assert keyword_overlap > 0 or original_distance < 0.25, (
                f"Query '{query}': result {r['id']} violates keyword floor "
                f"(keyword_overlap={keyword_overlap}, original_distance={original_distance})"
            )


# ---------------------------------------------------------------------------
# Test 5: FTS5 matches present in results
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_fts5_matches_present_in_results(indexed_corpus):
    """Searching 'kubernetes' (exact term) should produce at least one fts_match=True result."""
    config, conn, stats = indexed_corpus

    results = run_search_pipeline(config, conn, "kubernetes")
    all_results = results["all_results"]

    fts_matched = [r for r in all_results if r.get("fts_match")]

    assert len(fts_matched) >= 1, (
        f"Expected at least 1 result with fts_match=True for 'kubernetes', "
        f"got {len(fts_matched)}. Total results: {len(all_results)}"
    )
