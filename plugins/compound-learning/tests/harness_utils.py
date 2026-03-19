"""Utility module for compound-learning test harness."""

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import importlib.util

import lib.db as db

# Import index-learnings.py via spec (hyphen in filename)
_index_spec = importlib.util.spec_from_file_location(
    "index_learnings",
    PLUGIN_ROOT / "skills" / "index-learnings" / "index-learnings.py",
)
_index_mod = importlib.util.module_from_spec(_index_spec)
_index_spec.loader.exec_module(_index_mod)
index_single_file = _index_mod.index_single_file

# Import search-learnings.py via spec (hyphen in filename)
_search_spec = importlib.util.spec_from_file_location(
    "search_learnings",
    PLUGIN_ROOT / "scripts" / "search-learnings.py",
)
_search_mod = importlib.util.module_from_spec(_search_spec)
_search_spec.loader.exec_module(_search_mod)

extract_query_keywords = _search_mod.extract_query_keywords
fts5_search = _search_mod.fts5_search
hybrid_rerank = _search_mod.hybrid_rerank


def match_ids_by_filename(results: List[Dict[str, Any]], patterns: List[str]) -> Set[str]:
    """Return IDs of results whose file_path basename contains any pattern."""
    matched: Set[str] = set()
    for r in results:
        basename = os.path.basename(r.get("metadata", {}).get("file_path", "")).lower()
        if any(p.lower() in basename for p in patterns):
            matched.add(r["id"])
    return matched


class MetricsCollector:
    """Collects search results and computes precision/recall/F1 metrics."""

    def __init__(self) -> None:
        self._results: List[Dict[str, Any]] = []

    def add_result(
        self,
        query: str,
        expected_ids: Set[str],
        actual_ids: List[str],
        tier: str = "all",
    ) -> None:
        self._results.append({
            "query": query,
            "expected_ids": expected_ids,
            "actual_ids": actual_ids,
            "tier": tier,
        })

    def precision_at_k(self, k: int = 5) -> float:
        if not self._results:
            return 0.0
        precisions = []
        for r in self._results:
            top_k = r["actual_ids"][:k]
            if not top_k:
                precisions.append(0.0)
                continue
            hits = sum(1 for doc_id in top_k if doc_id in r["expected_ids"])
            precisions.append(hits / len(top_k))
        return sum(precisions) / len(precisions)

    def recall(self) -> float:
        if not self._results:
            return 0.0
        recalls = []
        for r in self._results:
            expected = r["expected_ids"]
            if not expected:
                recalls.append(1.0)
                continue
            hits = sum(1 for doc_id in r["actual_ids"] if doc_id in expected)
            recalls.append(hits / len(expected))
        return sum(recalls) / len(recalls)

    def f1(self) -> float:
        p = self.precision_at_k()
        r = self.recall()
        if p + r == 0:
            return 0.0
        return 2 * p * r / (p + r)

    def per_query_stats(self) -> List[Dict[str, Any]]:
        stats = []
        for r in self._results:
            expected = r["expected_ids"]
            actual = r["actual_ids"]
            top_k = actual[:5]
            hits_at_k = sum(1 for doc_id in top_k if doc_id in expected)
            precision = hits_at_k / len(top_k) if top_k else 0.0
            rec = (
                sum(1 for doc_id in actual if doc_id in expected) / len(expected)
                if expected
                else 1.0
            )
            stats.append({
                "query": r["query"],
                "tier": r["tier"],
                "precision_at_5": round(precision, 4),
                "recall": round(rec, 4),
                "expected_count": len(expected),
                "actual_count": len(actual),
            })
        return stats

    def summary(self) -> Dict[str, Any]:
        return {
            "total_queries": len(self._results),
            "precision_at_5": round(self.precision_at_k(), 4),
            "recall": round(self.recall(), 4),
            "f1": round(self.f1(), 4),
        }


def run_search_pipeline(
    config: Dict[str, Any],
    conn: Any,
    query: str,
    keyword_weight: float | None = None,
    high_threshold: float | None = None,
    possible_threshold: float | None = None,
) -> Dict[str, Any]:
    """Run the full search pipeline and return tiered results."""
    kw_weight = keyword_weight if keyword_weight is not None else config["learnings"]["keywordBoostWeight"]
    hi_thresh = high_threshold if high_threshold is not None else config["learnings"]["highConfidenceThreshold"]
    pos_thresh = possible_threshold if possible_threshold is not None else config["learnings"]["possiblyRelevantThreshold"]

    raw = db.search(conn, query, scope_repos=[], n_results=20, threshold=1.0)
    query_keywords = extract_query_keywords(query)
    fts_matches = fts5_search(conn, query)
    reranked = hybrid_rerank(raw, query_keywords, kw_weight, fts_ids=fts_matches)

    reranked = [
        r for r in reranked
        if r.get("keyword_overlap", 0) > 0 or r.get("original_distance", 1.0) < 0.25
    ]

    high_confidence = [r for r in reranked if r["distance"] < hi_thresh]
    possibly_relevant = [
        r for r in reranked if hi_thresh <= r["distance"] < pos_thresh
    ]

    return {
        "high_confidence": high_confidence,
        "possibly_relevant": possibly_relevant,
        "all_results": reranked,
    }


def compare_metrics(
    baseline_summary: Dict[str, Any],
    candidate_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare two metric summaries and return deltas with percentage changes."""
    diff: Dict[str, Any] = {}
    for key in ("precision_at_5", "recall", "f1"):
        base_val = baseline_summary.get(key, 0.0)
        cand_val = candidate_summary.get(key, 0.0)
        delta = round(cand_val - base_val, 4)
        pct_change = round((delta / base_val) * 100, 2) if base_val != 0 else 0.0
        diff[key] = {"baseline": base_val, "candidate": cand_val, "delta": delta, "pct_change": pct_change}
    return diff


def count_indexed(conn: Any) -> int:
    """Return the number of indexed documents."""
    return db.count_documents(conn)
