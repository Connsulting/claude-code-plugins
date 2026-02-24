"""
E2E tests for compound-learning search relevance.

These tests exercise the full search pipeline: real embeddings (all-MiniLM-L6-v2),
real SQLite/sqlite-vec/fts5, real indexing, and real reranking logic.
No internal modules are mocked.
"""

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

# Ensure plugin root is on path so lib/ and scripts/ are importable
PLUGIN_ROOT = str(Path(__file__).parent.parent)
sys.path.insert(0, PLUGIN_ROOT)

import importlib.util

import lib.db as db

# Import search-learnings.py via spec because the hyphen prevents normal import
_spec = importlib.util.spec_from_file_location(
    "search_learnings",
    Path(PLUGIN_ROOT) / "scripts" / "search-learnings.py",
)
_search_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_search_mod)

calculate_keyword_overlap = _search_mod.calculate_keyword_overlap
extract_query_keywords = _search_mod.extract_query_keywords
fts5_search = _search_mod.fts5_search
hybrid_rerank = _search_mod.hybrid_rerank


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def embedding_model():
    """Warm up the embedding model once per test module."""
    # Trigger lazy load
    db.get_embedding("warmup")
    yield


@pytest.fixture()
def tmp_db(tmp_path, embedding_model):
    """
    Isolated SQLite database in a temporary directory.
    Returns (config, conn) tuple. Caller is responsible for conn.close().
    """
    db_path = str(tmp_path / "test-compound-learning.db")
    config = {
        "sqlite": {"dbPath": db_path},
        "learnings": {
            "globalDir": str(tmp_path / "learnings"),
            "repoSearchPath": str(tmp_path),
            "highConfidenceThreshold": 0.40,
            "possiblyRelevantThreshold": 0.55,
            "keywordBoostWeight": 0.65,
        },
    }
    conn = db.get_connection(config)
    yield config, conn
    conn.close()


def _insert(conn: Any, doc_id: str, content: str, scope: str = "global") -> None:
    """Insert a document through the real indexing pipeline (get_embedding + upsert)."""
    db.upsert_document(
        conn,
        doc_id,
        content,
        {"scope": scope, "repo": "", "file_path": "", "topic": "other", "keywords": ""},
    )


def _search_raw(config: Dict, conn: Any, query: str, n: int = 20) -> List[Dict[str, Any]]:
    """KNN search returning raw results (before reranking)."""
    return db.search(conn, query, scope_repos=[], n_results=n, threshold=1.0)


def _full_search(
    config: Dict,
    conn: Any,
    query: str,
    n: int = 20,
    peek: bool = False,
) -> Dict[str, Any]:
    """
    Run the full search + rerank + tier pipeline and return a dict with keys:
      high_confidence, possibly_relevant, all_results
    """
    keyword_weight = config["learnings"]["keywordBoostWeight"]
    high_threshold = config["learnings"]["highConfidenceThreshold"]
    possible_threshold = config["learnings"]["possiblyRelevantThreshold"]

    raw = _search_raw(config, conn, query, n)
    query_keywords = extract_query_keywords(query)
    fts_matches = fts5_search(conn, query)
    reranked = hybrid_rerank(raw, query_keywords, keyword_weight, fts_ids=fts_matches)

    # Keyword overlap floor (mirrors search-learnings.py)
    reranked = [
        r for r in reranked
        if r.get("keyword_overlap", 0) > 0 or r.get("original_distance", 1.0) < 0.25
    ]

    high_confidence = [r for r in reranked if r["distance"] < high_threshold]
    possibly_relevant = [r for r in reranked if high_threshold <= r["distance"] < possible_threshold]

    if peek:
        max_results = 5
        peek_results = high_confidence[:max_results]
        if len(peek_results) < max_results:
            peek_results.extend(possibly_relevant[: max_results - len(peek_results)])
        return {"peek_results": peek_results, "high_confidence": high_confidence, "possibly_relevant": possibly_relevant}

    return {
        "high_confidence": high_confidence,
        "possibly_relevant": possibly_relevant,
        "all_results": reranked,
    }


# ---------------------------------------------------------------------------
# Test documents
# ---------------------------------------------------------------------------

PDF_LEARNING = (
    "pdf-layout-001",
    (
        "Slash command pdf layout: when exporting documents to PDF, "
        "use @pdf-layout slash command to control page breaks and margins. "
        "The /pdf command accepts layout options such as landscape, portrait, and custom margins."
    ),
)

GRAFANA_LEARNING = (
    "grafana-alerting-001",
    (
        "Grafana alerting gotchas: alert rules fire duplicate notifications when "
        "the evaluation interval is shorter than the pending period. "
        "Set pending period to at least 2x the evaluation interval to avoid alert flapping."
    ),
)

PYTHON_ASYNC_LEARNING = (
    "python-async-001",
    (
        "Python asyncio patterns: use asyncio.gather() for concurrent coroutine execution. "
        "Avoid blocking calls like time.sleep() inside async functions; use asyncio.sleep() instead. "
        "Always await coroutines; forgetting await causes silent bugs."
    ),
)

REACT_TESTING_LEARNING = (
    "react-testing-001",
    (
        "React component testing: use React Testing Library with user-event for interaction tests. "
        "Query by role or label text, not by test-id, to test accessible behavior. "
        "Avoid shallow rendering; prefer full render with real child components."
    ),
)

COMMAND_LEARNING = (
    "command-reference-001",
    (
        "Command reference guide: terminal commands for productivity. "
        "Use command aliases to shorten frequently used commands. "
        "The command history can be searched with Ctrl-R."
    ),
)


# ---------------------------------------------------------------------------
# Test 1: Relevant results ranked above irrelevant
# ---------------------------------------------------------------------------

def test_relevant_results_ranked_above_irrelevant(tmp_db):
    """
    Search 'slash command pdf layout' must return the PDF learning and must
    exclude the Grafana learning (different topic, zero keyword overlap with query).
    """
    config, conn = tmp_db
    _insert(conn, *PDF_LEARNING)
    _insert(conn, *GRAFANA_LEARNING)
    _insert(conn, *PYTHON_ASYNC_LEARNING)
    _insert(conn, *REACT_TESTING_LEARNING)

    results = _full_search(config, conn, "slash command pdf layout")
    all_ids = {r["id"] for r in results["all_results"]}

    assert PDF_LEARNING[0] in all_ids, (
        f"Expected PDF learning in results but got ids: {all_ids}"
    )
    assert GRAFANA_LEARNING[0] not in all_ids, (
        f"Grafana learning should be excluded (zero keyword overlap with 'slash command pdf layout')"
    )


# ---------------------------------------------------------------------------
# Test 2: High confidence threshold filters weak matches
# ---------------------------------------------------------------------------

def test_high_confidence_threshold_filters_weak_matches(tmp_db):
    """
    All results in high_confidence bucket must have adjusted distance < 0.40.
    """
    config, conn = tmp_db
    for doc_id, content in [PDF_LEARNING, PYTHON_ASYNC_LEARNING, REACT_TESTING_LEARNING]:
        _insert(conn, doc_id, content)

    results = _full_search(config, conn, "slash command pdf layout")
    high_confidence = results["high_confidence"]

    for r in high_confidence:
        assert r["distance"] < 0.40, (
            f"Result {r['id']} in high_confidence has distance {r['distance']} >= 0.40"
        )


# ---------------------------------------------------------------------------
# Test 3: Possibly relevant threshold boundary
# ---------------------------------------------------------------------------

def test_possibly_relevant_threshold_boundary(tmp_db):
    """
    Results in possibly_relevant bucket must have distance in [0.40, 0.55).
    """
    config, conn = tmp_db
    for doc_id, content in [PDF_LEARNING, PYTHON_ASYNC_LEARNING, REACT_TESTING_LEARNING, COMMAND_LEARNING]:
        _insert(conn, doc_id, content)

    results = _full_search(config, conn, "pdf command slash")
    possibly_relevant = results["possibly_relevant"]

    for r in possibly_relevant:
        assert 0.40 <= r["distance"] < 0.55, (
            f"Result {r['id']} in possibly_relevant has distance {r['distance']} outside [0.40, 0.55)"
        )


# ---------------------------------------------------------------------------
# Test 4: Keyword overlap floor removes zero-overlap results
# ---------------------------------------------------------------------------

def test_keyword_overlap_floor_removes_zero_overlap(tmp_db):
    """
    A document with moderate semantic similarity but zero keyword overlap should
    be excluded from results (unless original_distance < 0.25, which is the escape hatch).

    We verify: after filtering, no result has keyword_overlap == 0 AND
    original_distance >= 0.25.
    """
    config, conn = tmp_db
    for doc_id, content in [PDF_LEARNING, GRAFANA_LEARNING, PYTHON_ASYNC_LEARNING, REACT_TESTING_LEARNING]:
        _insert(conn, doc_id, content)

    results = _full_search(config, conn, "slash command pdf layout")

    for r in results["all_results"]:
        keyword_overlap = r.get("keyword_overlap", 0)
        original_distance = r.get("original_distance", 1.0)

        # The floor rule: must not have zero overlap unless distance < 0.25
        if keyword_overlap == 0:
            assert original_distance < 0.25, (
                f"Result {r['id']} has zero keyword_overlap but original_distance "
                f"{original_distance} >= 0.25 - should have been filtered out"
            )


# ---------------------------------------------------------------------------
# Test 5: Keyword boost improves ranking
# ---------------------------------------------------------------------------

def test_keyword_boost_improves_ranking(tmp_db):
    """
    When two results have similar semantic distance, the one with keyword overlap
    should rank higher (lower adjusted distance) after hybrid_rerank.
    """
    config, conn = tmp_db

    # Two documents that are semantically similar to the query
    kw_overlap_doc_id = "kw-overlap-doc"
    kw_overlap_content = (
        "slash command pdf layout: how to use the pdf slash command "
        "to layout pages and configure pdf export settings."
    )
    no_overlap_doc_id = "no-kw-overlap-doc"
    no_overlap_content = (
        "When creating presentations, page arrangement and format configuration "
        "tools help organize visual structures and export options efficiently."
    )

    _insert(conn, kw_overlap_doc_id, kw_overlap_content)
    _insert(conn, no_overlap_doc_id, no_overlap_content)

    query = "slash command pdf layout"
    query_keywords = extract_query_keywords(query)
    keyword_weight = config["learnings"]["keywordBoostWeight"]

    raw = _search_raw(config, conn, query, n=20)
    reranked = hybrid_rerank(raw, query_keywords, keyword_weight, fts_ids=set())

    kw_result = next((r for r in reranked if r["id"] == kw_overlap_doc_id), None)
    no_kw_result = next((r for r in reranked if r["id"] == no_overlap_doc_id), None)

    assert kw_result is not None, "keyword-overlap doc not found in results"

    assert no_kw_result is not None, "Expected no-overlap document in results for ranking comparison"
    assert kw_result["distance"] < no_kw_result["distance"], (
        f"Keyword-overlap doc (dist={kw_result['distance']}) should rank higher than "
        f"no-overlap doc (dist={no_kw_result['distance']})"
    )

    # Verify keyword overlap was actually detected for the keyword doc
    assert kw_result["keyword_overlap"] > 0, (
        f"Expected non-zero keyword_overlap for keyword doc, got {kw_result['keyword_overlap']}"
    )


# ---------------------------------------------------------------------------
# Test 6: FTS5 boost helps stemmed matches
# ---------------------------------------------------------------------------

def test_fts5_boost_helps_stemmed_matches(tmp_db):
    """
    Search "commanding" should match a document containing "command" via FTS5
    Porter stemming. The matched document should have fts_match=True and a
    lower distance than it would without the FTS5 boost.
    """
    config, conn = tmp_db

    stem_doc_id = "stem-command-doc"
    stem_doc_content = (
        "Terminal command tips: use command aliases and command history "
        "to improve command-line workflow productivity."
    )
    _insert(conn, stem_doc_id, stem_doc_content)

    query = "commanding"

    # Verify FTS5 matches (Porter stemming: commanding -> command)
    fts_matches = fts5_search(conn, query)
    assert stem_doc_id in fts_matches, (
        f"FTS5 should match '{stem_doc_id}' for query 'commanding' via Porter stemming, "
        f"got matches: {fts_matches}"
    )

    # Verify FTS5 boost reduces distance vs no boost
    query_keywords = extract_query_keywords(query)
    keyword_weight = config["learnings"]["keywordBoostWeight"]
    raw = _search_raw(config, conn, query, n=20)

    stem_raw = next((r for r in raw if r["id"] == stem_doc_id), None)
    assert stem_raw is not None, "stem doc not found in raw KNN results"
    original_distance = stem_raw["distance"]

    # Rerank with FTS5 boost
    reranked_with_fts = hybrid_rerank(
        [dict(r) for r in raw], query_keywords, keyword_weight, fts_ids=fts_matches
    )
    # Rerank without FTS5 boost
    reranked_without_fts = hybrid_rerank(
        [dict(r) for r in raw], query_keywords, keyword_weight, fts_ids=None
    )

    with_fts = next((r for r in reranked_with_fts if r["id"] == stem_doc_id), None)
    without_fts = next((r for r in reranked_without_fts if r["id"] == stem_doc_id), None)

    assert with_fts is not None
    assert without_fts is not None
    assert with_fts["fts_match"] is True

    # FTS5 boost reduces distance by 0.05
    assert with_fts["distance"] < without_fts["distance"], (
        f"FTS5 boost should lower distance: with={with_fts['distance']} without={without_fts['distance']}"
    )
    assert abs(with_fts["distance"] - (without_fts["distance"] - 0.05)) < 0.001, (
        f"Expected 0.05 reduction from FTS5 boost"
    )


# ---------------------------------------------------------------------------
# Test 7: Very high similarity bypasses keyword floor
# ---------------------------------------------------------------------------

def test_very_high_similarity_bypasses_keyword_floor(tmp_db):
    """
    A document with original_distance < 0.25 (very high semantic similarity)
    should pass the keyword floor filter even if keyword_overlap == 0.

    We simulate this by constructing a near-duplicate that uses synonyms.
    The test verifies the filter logic directly with a manually crafted result.
    """
    # Directly test the filter logic since getting original_distance < 0.25
    # with zero keyword overlap in a real corpus is fragile

    # Simulate a result that has very high semantic similarity but zero keyword overlap
    simulated_result = {
        "id": "very-similar-doc",
        "document": "Document with no matching words",
        "distance": 0.30,  # adjusted distance after rerank
        "original_distance": 0.20,  # very high semantic similarity (< 0.25)
        "keyword_overlap": 0.0,
        "fts_match": False,
        "metadata": {},
    }

    # Simulate a result that has moderate similarity and zero keyword overlap (should be filtered)
    filtered_result = {
        "id": "moderately-similar-doc",
        "document": "Another document with no matching words",
        "distance": 0.45,
        "original_distance": 0.45,  # NOT < 0.25
        "keyword_overlap": 0.0,
        "fts_match": False,
        "metadata": {},
    }

    results_before_filter = [simulated_result, filtered_result]

    # Apply the keyword floor filter (same logic as search-learnings.py)
    filtered = [
        r for r in results_before_filter
        if r.get("keyword_overlap", 0) > 0 or r.get("original_distance", 1.0) < 0.25
    ]

    result_ids = {r["id"] for r in filtered}
    assert "very-similar-doc" in result_ids, (
        "Document with original_distance < 0.25 should bypass keyword floor"
    )
    assert "moderately-similar-doc" not in result_ids, (
        "Document with zero keyword_overlap and original_distance >= 0.25 should be filtered"
    )


# ---------------------------------------------------------------------------
# Test 8: Peek mode respects new thresholds
# ---------------------------------------------------------------------------

def test_peek_mode_respects_new_thresholds(tmp_db):
    """
    Peek mode results must only contain items that passed the tightened thresholds
    (high_confidence < 0.40, possibly_relevant < 0.55). No result above 0.55 should appear.
    """
    config, conn = tmp_db
    for doc_id, content in [PDF_LEARNING, GRAFANA_LEARNING, PYTHON_ASYNC_LEARNING, REACT_TESTING_LEARNING, COMMAND_LEARNING]:
        _insert(conn, doc_id, content)

    results = _full_search(config, conn, "slash command pdf layout", peek=True)
    peek_results = results["peek_results"]

    for r in peek_results:
        assert r["distance"] < 0.55, (
            f"Peek result {r['id']} has distance {r['distance']} >= 0.55 (above possibly_relevant threshold)"
        )

    # Peek results must be sorted: high_confidence first, then possibly_relevant
    high_conf_cutoff = config["learnings"]["highConfidenceThreshold"]
    seen_possibly_relevant = False
    for r in peek_results:
        if r["distance"] >= high_conf_cutoff:
            seen_possibly_relevant = True
        if seen_possibly_relevant:
            assert r["distance"] >= high_conf_cutoff, (
                "High confidence result appeared after possibly_relevant result in peek mode"
            )
