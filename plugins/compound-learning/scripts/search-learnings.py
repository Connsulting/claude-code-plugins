#!/usr/bin/env python3
"""
Search learnings in SQLite with distance threshold filtering.
Filters results before outputting to avoid context pollution.
"""

import sys
import os

# Locate plugin root so lib/ is importable regardless of cwd
_PLUGIN_ROOT = os.environ.get('CLAUDE_PLUGIN_ROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _PLUGIN_ROOT)

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Any, Tuple, Set

import lib.db as db
import lib.git_utils as git_utils


# Common stopwords to filter from query keywords
STOPWORDS = {
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare',
    'ought', 'used', 'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by',
    'from', 'as', 'into', 'through', 'during', 'before', 'after', 'above',
    'below', 'between', 'under', 'again', 'further', 'then', 'once',
    'here', 'there', 'when', 'where', 'why', 'how', 'all', 'each', 'few',
    'more', 'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only',
    'own', 'same', 'so', 'than', 'too', 'very', 'just', 'and', 'but',
    'if', 'or', 'because', 'until', 'while', 'about', 'against', 'this',
    'that', 'these', 'those', 'what', 'which', 'who', 'whom', 'its', 'it'
}


def detect_learning_hierarchy(cwd: str, home: str) -> List[str]:
    """Walk up from cwd to home, collect all dirs with .projects/learnings/.

    Uses worktree-aware repo root resolution so that a worktree CWD produces
    the same repo name as the main checkout.
    """
    repos: List[str] = []
    seen: Set[str] = set()
    home_path = Path(home)

    # Resolve the real repo root first (handles worktrees)
    real_root = Path(git_utils.resolve_repo_root(cwd))
    real_root_learning_dir = real_root / '.projects' / 'learnings'
    if real_root_learning_dir.exists() and real_root != home_path:
        repo_name = real_root.name
        repos.append(repo_name)
        seen.add(repo_name)

    # Walk up from cwd itself for any additional .projects/learnings/ dirs
    current = Path(cwd).resolve()
    while True:
        learning_dir = current / '.projects' / 'learnings'
        if learning_dir.exists() and current != home_path:
            repo_name = current.name
            if repo_name not in seen:
                repos.append(repo_name)
                seen.add(repo_name)

        if current == home_path:
            break
        if current.parent == current:
            break

        current = current.parent

    return repos


def parse_tag_filters(query: str) -> Tuple[str, Dict[str, Any]]:
    """Extract tag: and category: filters from query.

    Returns (cleaned_query, filters_dict)
    """
    filters: Dict[str, Any] = {}
    cleaned = query

    tag_matches = re.findall(r'\btag:(\w+)', query, re.IGNORECASE)
    if tag_matches:
        filters['tags'] = [t.lower() for t in tag_matches]
        cleaned = re.sub(r'\btag:\w+\s*', '', cleaned)

    cat_match = re.search(r'\bcategory:(\w+)', query, re.IGNORECASE)
    if cat_match:
        filters['category'] = cat_match.group(1).lower()
        cleaned = re.sub(r'\bcategory:\w+\s*', '', cleaned)

    return cleaned.strip(), filters


def extract_query_keywords(query: str) -> Set[str]:
    """Extract meaningful keywords from query, removing stopwords."""
    tokens = re.findall(r'\b\w+\b', query.lower())
    return {t for t in tokens if t not in STOPWORDS and len(t) >= 2}


def calculate_keyword_overlap(keywords: Set[str], document: str) -> float:
    """Calculate ratio of query keywords found in document."""
    if not keywords:
        return 0.0
    doc_lower = document.lower()
    matches = sum(1 for kw in keywords if kw in doc_lower)
    return matches / len(keywords)


def hybrid_rerank(
    results: List[Dict[str, Any]],
    query_keywords: Set[str],
    keyword_weight: float = 0.3
) -> List[Dict[str, Any]]:
    """Re-rank results by combining semantic distance with keyword overlap.

    Args:
        results: List of result dicts with 'document' and 'distance'
        query_keywords: Set of keywords extracted from query
        keyword_weight: How much to boost for keyword matches (0-1)

    Returns:
        Results re-sorted by adjusted distance (lower = better)
    """
    for result in results:
        overlap = calculate_keyword_overlap(query_keywords, result['document'])
        result['keyword_overlap'] = round(overlap, 4)
        result['original_distance'] = result['distance']
        result['distance'] = round(
            result['distance'] * (1 - keyword_weight * overlap),
            4
        )

    return sorted(results, key=lambda x: x['distance'])


def query_single_keyword(
    config: Any,
    keyword: str,
    scope_repos: List[str],
    query_size: int,
) -> List[Dict[str, Any]]:
    """Query SQLite for a single keyword. Opens its own connection (thread-safe)."""
    try:
        conn = db.get_connection(config)
        try:
            results = db.search(conn, keyword, scope_repos, n_results=query_size, threshold=1.0)
            for r in results:
                r['matched_keyword'] = keyword
            return results
        finally:
            conn.close()
    except Exception as e:
        print(f"Error in query_single_keyword: {e}", file=sys.stderr)
        return []


def merge_parallel_results(
    all_results: List[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """Merge results from parallel keyword queries, keeping best distance per ID."""
    best_by_id: Dict[str, Dict[str, Any]] = {}

    for result_list in all_results:
        for result in result_list:
            doc_id = result['id']
            if doc_id not in best_by_id or result['distance'] < best_by_id[doc_id]['distance']:
                best_by_id[doc_id] = result

    return sorted(best_by_id.values(), key=lambda x: x['distance'])


def search_learnings(
    query: str,
    working_dir: str | None = None,
    max_results: int = 5,
    peek_mode: bool = False,
    exclude_ids: str = "",
    threshold_override: float | None = None,
    keywords_json: str | None = None
) -> None:
    """
    Search learnings with tiered relevance filtering.
    Returns high confidence results (distance < 0.5) and possibly relevant (0.5-0.7).
    """
    config = db.load_config()
    high_threshold = threshold_override if threshold_override is not None else config['learnings']['highConfidenceThreshold']
    possible_threshold = config['learnings']['possiblyRelevantThreshold']
    keyword_weight = config['learnings']['keywordBoostWeight']

    # Parse keywords from JSON if provided, otherwise use query as single keyword
    if keywords_json:
        try:
            keywords = json.loads(keywords_json)
            if not isinstance(keywords, list) or not keywords:
                keywords = [query] if query else []
        except json.JSONDecodeError:
            keywords = [query] if query else []
    else:
        keywords = [query] if query else []

    if not keywords:
        print(json.dumps({'status': 'empty', 'message': 'No keywords provided'}))
        return

    # Parse tag/category filters from first keyword (for compatibility)
    cleaned_query, _ = parse_tag_filters(keywords[0])
    if cleaned_query != keywords[0]:
        keywords[0] = cleaned_query

    # Extract keywords for hybrid re-ranking (from all keywords)
    query_keywords: Set[str] = set()
    for kw in keywords:
        query_keywords.update(extract_query_keywords(kw))

    cwd = working_dir if working_dir else os.getcwd()
    home = os.path.expanduser('~')

    # Detect repo hierarchy
    repos = detect_learning_hierarchy(cwd, home)

    try:
        # Parse exclusion IDs upfront to determine query size
        exclude_set: Set[str] = set()
        if exclude_ids:
            exclude_set = set(id.strip() for id in exclude_ids.split(',') if id.strip())

        query_size = max_results + len(exclude_set)

        # Run parallel queries for each keyword; each call opens its own connection
        all_results: List[List[Dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=len(keywords)) as executor:
            futures = {
                executor.submit(query_single_keyword, config, kw, repos, query_size): kw
                for kw in keywords
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    all_results.append(result)
                except Exception as e:
                    print(f"Error in search_learnings: {e}", file=sys.stderr)

        # Merge results from parallel queries
        raw_results = merge_parallel_results(all_results)

        # Apply hybrid re-ranking (keyword boost)
        reranked_results = hybrid_rerank(raw_results, query_keywords, keyword_weight)

        # Filter out excluded IDs
        if exclude_set:
            reranked_results = [r for r in reranked_results if r['id'] not in exclude_set]

        # Split results into tiers based on adjusted distance
        high_confidence: List[Dict[str, Any]] = []
        possibly_relevant: List[Dict[str, Any]] = []

        for result in reranked_results:
            if result['distance'] < high_threshold:
                high_confidence.append(result)
            elif result['distance'] < possible_threshold:
                possibly_relevant.append(result)

        # Peek mode: high_confidence first, backfill from possibly_relevant up to max_results
        if peek_mode:
            peek_results = list(high_confidence[:max_results])
            if len(peek_results) < max_results and possibly_relevant:
                remaining = max_results - len(peek_results)
                peek_results.extend(possibly_relevant[:remaining])

            if peek_results:
                output = {
                    'status': 'found',
                    'count': len(peek_results),
                    'keywords_searched': keywords,
                    'learnings': peek_results
                }
            else:
                output = {'status': 'empty', 'keywords_searched': keywords}

            print(json.dumps(output, indent=2))
            return

        # Build output
        total_found = len(high_confidence) + len(possibly_relevant)
        candidates_searched = len(raw_results)

        if total_found == 0:
            output = {
                'status': 'no_results',
                'message': f'No relevant learnings found (searched {candidates_searched} candidates, none met distance < {possible_threshold} threshold)',
                'query': query,
                'repos_searched': repos,
                'high_confidence': [],
                'possibly_relevant': []
            }
        else:
            parts = []
            if high_confidence:
                parts.append(f'{len(high_confidence)} high confidence')
            if possibly_relevant:
                parts.append(f'{len(possibly_relevant)} possibly relevant')
            output = {
                'status': 'success',
                'message': f'Found {" + ".join(parts)} learning(s)',
                'query': query,
                'repos_searched': repos,
                'high_confidence': high_confidence,
                'possibly_relevant': possibly_relevant
            }

        print(json.dumps(output, indent=2))

    except Exception as e:
        error_output = {
            'status': 'error',
            'message': str(e),
            'query': query
        }
        print(json.dumps(error_output, indent=2))
        sys.exit(1)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Search learnings in SQLite')
    parser.add_argument('query', nargs='?', default='', help='Search query text (ignored if --keywords-json provided)')
    parser.add_argument('working_dir', nargs='?', default=None, help='Working directory')
    parser.add_argument('--peek', action='store_true',
                        help='Peek mode: prefers high confidence results, backfills from possibly_relevant if needed')
    parser.add_argument('--exclude-ids', type=str, default='',
                        help='Comma separated learning IDs to exclude from results')
    parser.add_argument('--threshold', type=float, default=None,
                        help='Override high confidence threshold (default from config)')
    parser.add_argument('--max-results', type=int, default=5,
                        help='Maximum results to return')
    parser.add_argument('--keywords-json', type=str, default=None,
                        help='JSON array of keywords to search in parallel (e.g. \'["prompt caching", "TTL"]\')')

    args = parser.parse_args()
    search_learnings(
        args.query,
        args.working_dir,
        max_results=args.max_results,
        peek_mode=args.peek,
        exclude_ids=args.exclude_ids,
        threshold_override=args.threshold,
        keywords_json=args.keywords_json
    )
