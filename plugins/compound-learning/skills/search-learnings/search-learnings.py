#!/usr/bin/env python3
"""
Search learnings in ChromaDB with distance threshold filtering
Filters results before outputting to avoid context pollution
"""

import chromadb
import sys
import json
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Tuple, Set


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


def load_config() -> Dict[str, Any]:
    """Load configuration from environment variables, then config file, then defaults."""
    home = os.path.expanduser('~')

    # Default configuration
    defaults = {
        'chromadb': {
            'host': 'localhost',
            'port': 8000,
            'dataDir': os.path.join(home, '.claude/chroma-data')
        },
        'learnings': {
            'globalDir': os.path.join(home, '.projects/learnings'),
            'repoSearchPath': home,
            'highConfidenceThreshold': 0.5,
            'possiblyRelevantThreshold': 0.6,
            'keywordBoostWeight': 0.3
        }
    }

    # Environment variables take highest priority (with validation)
    env_port = os.environ.get('CHROMADB_PORT')
    env_high_threshold = os.environ.get('LEARNINGS_HIGH_CONFIDENCE_THRESHOLD')
    env_possible_threshold = os.environ.get('LEARNINGS_POSSIBLY_RELEVANT_THRESHOLD')
    env_keyword_weight = os.environ.get('LEARNINGS_KEYWORD_BOOST_WEIGHT')
    env_data_dir = os.environ.get('CHROMADB_DATA_DIR', '')
    env_global_dir = os.environ.get('LEARNINGS_GLOBAL_DIR', '')
    env_repo_path = os.environ.get('LEARNINGS_REPO_SEARCH_PATH', '')

    # Validate numeric environment variables (silently use defaults on error)
    parsed_port = None
    if env_port:
        try:
            parsed_port = int(env_port)
        except ValueError:
            pass

    parsed_high_threshold = None
    if env_high_threshold:
        try:
            parsed_high_threshold = float(env_high_threshold)
        except ValueError:
            pass

    parsed_possible_threshold = None
    if env_possible_threshold:
        try:
            parsed_possible_threshold = float(env_possible_threshold)
        except ValueError:
            pass

    parsed_keyword_weight = None
    if env_keyword_weight:
        try:
            parsed_keyword_weight = float(env_keyword_weight)
        except ValueError:
            pass

    env_config = {
        'chromadb': {
            'host': os.environ.get('CHROMADB_HOST'),
            'port': parsed_port,
            'dataDir': os.path.expanduser(env_data_dir) if env_data_dir else None
        },
        'learnings': {
            'globalDir': os.path.expanduser(env_global_dir) if env_global_dir else None,
            'repoSearchPath': os.path.expanduser(env_repo_path) if env_repo_path else None,
            'highConfidenceThreshold': parsed_high_threshold,
            'possiblyRelevantThreshold': parsed_possible_threshold,
            'keywordBoostWeight': parsed_keyword_weight
        }
    }

    # Try to load config file
    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT', os.path.join(home, '.claude'))
    config_file = Path(plugin_root) / '.claude-plugin' / 'config.json'
    file_config: Dict[str, Any] = {'chromadb': {}, 'learnings': {}}

    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                file_config = json.load(f)

            # Expand ${HOME} in paths
            for key in ['dataDir']:
                if key in file_config.get('chromadb', {}):
                    file_config['chromadb'][key] = file_config['chromadb'][key].replace('${HOME}', home)
            for key in ['globalDir', 'repoSearchPath']:
                if key in file_config.get('learnings', {}):
                    file_config['learnings'][key] = file_config['learnings'][key].replace('${HOME}', home)
        except Exception:
            pass  # Silently fall back in skill context

    # Merge: defaults <- file_config <- env_config (env wins)
    result = defaults.copy()
    for section in ['chromadb', 'learnings']:
        if section in file_config:
            for key, value in file_config[section].items():
                if value is not None:
                    result[section][key] = value
        for key, value in env_config[section].items():
            if value is not None:
                result[section][key] = value

    return result


def detect_learning_hierarchy(cwd: str, home: str) -> List[str]:
    """Walk up from cwd to home, collect all dirs with .projects/learnings/"""
    repos = []
    current = Path(cwd)
    home_path = Path(home)

    # Walk up from cwd, stopping at or before home
    while True:
        # Check if this directory has .projects/learnings
        learning_dir = current / '.projects' / 'learnings'
        if learning_dir.exists() and current != home_path:
            repos.append(current.name)

        # Stop when we reach home
        if current == home_path:
            break

        # Stop when we reach root (parent == self)
        if current.parent == current:
            break

        current = current.parent

    return repos


def build_scope_filter(repos: List[str]) -> Dict[str, Any]:
    """Build ChromaDB where filter for hierarchical repo scoping"""
    # Always include global
    conditions: List[Dict[str, Any]] = [{"scope": {"$eq": "global"}}]

    # Add all parent repos
    for repo in repos:
        conditions.append({
            "$and": [
                {"scope": {"$eq": "repo"}},
                {"repo": {"$eq": repo}}
            ]
        })

    return {"$or": conditions} if len(conditions) > 1 else conditions[0]


def parse_tag_filters(query: str) -> Tuple[str, Dict[str, Any]]:
    """Extract tag: and category: filters from query.

    Returns (cleaned_query, filters_dict)
    """
    filters: Dict[str, Any] = {}
    cleaned = query

    # Extract tag:value patterns
    tag_matches = re.findall(r'\btag:(\w+)', query, re.IGNORECASE)
    if tag_matches:
        filters['tags'] = [t.lower() for t in tag_matches]
        cleaned = re.sub(r'\btag:\w+\s*', '', cleaned)

    # Extract category:value patterns
    cat_match = re.search(r'\bcategory:(\w+)', query, re.IGNORECASE)
    if cat_match:
        filters['category'] = cat_match.group(1).lower()
        cleaned = re.sub(r'\bcategory:\w+\s*', '', cleaned)

    return cleaned.strip(), filters


def build_tag_filter(tag_filters: Dict[str, Any]) -> Dict[str, Any] | None:
    """Build ChromaDB where clause for tag/category filters."""
    if not tag_filters:
        return None

    conditions: List[Dict[str, Any]] = []

    # Category filter (exact match)
    if 'category' in tag_filters:
        conditions.append({"category": {"$eq": tag_filters['category']}})

    # Tag filter (tags field contains the tag, using $contains for comma-separated)
    # ChromaDB doesn't support $contains on strings, so we use $like pattern matching
    # Since tags are stored as comma-separated, we check if tag appears in the string
    if 'tags' in tag_filters:
        for tag in tag_filters['tags']:
            # Match tag at start, end, or surrounded by commas
            conditions.append({"tags": {"$contains": tag}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def extract_query_keywords(query: str) -> Set[str]:
    """Extract meaningful keywords from query, removing stopwords."""
    # Tokenize on whitespace and punctuation
    tokens = re.findall(r'\b\w+\b', query.lower())
    # Remove stopwords, keep words >= 2 chars
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
        # Reduce distance (better score) when keywords match
        # Boost factor: up to keyword_weight reduction
        result['keyword_overlap'] = round(overlap, 4)
        result['original_distance'] = result['distance']
        result['distance'] = round(
            result['distance'] * (1 - keyword_weight * overlap),
            4
        )

    # Re-sort by adjusted distance
    return sorted(results, key=lambda x: x['distance'])


def search_learnings(
    query: str,
    working_dir: str | None = None,
    max_results: int = 5,
    peek_mode: bool = False,
    exclude_ids: str = ""
) -> None:
    """
    Search learnings with tiered relevance filtering.
    Returns high confidence results (distance < 0.5) and possibly relevant (0.5-0.7).

    Args:
        query: Search query text
        working_dir: The user's working directory (where Claude was invoked)
        max_results: Maximum number of results to fetch from ChromaDB
        peek_mode: If True, return only high confidence results in simplified format
        exclude_ids: Comma separated learning IDs to exclude from results
    """
    # Load configuration
    config = load_config()
    host = config['chromadb']['host']
    port = config['chromadb']['port']
    high_threshold = config['learnings']['highConfidenceThreshold']
    possible_threshold = config['learnings']['possiblyRelevantThreshold']
    keyword_weight = config['learnings']['keywordBoostWeight']

    # Parse tag/category filters from query
    cleaned_query, tag_filters = parse_tag_filters(query)
    search_query = cleaned_query if cleaned_query else query

    # Extract keywords for hybrid re-ranking
    query_keywords = extract_query_keywords(search_query)

    # Use provided working directory or fall back to cwd
    cwd = working_dir if working_dir else os.getcwd()
    home = os.path.expanduser('~')

    # Detect repo hierarchy
    repos = detect_learning_hierarchy(cwd, home)

    # Build scope filter
    scope_filter = build_scope_filter(repos)

    # Build combined filter (scope + optional tags)
    tag_filter = build_tag_filter(tag_filters)
    if tag_filter:
        combined_filter = {"$and": [scope_filter, tag_filter]}
    else:
        combined_filter = scope_filter

    try:
        # Connect to ChromaDB
        client = chromadb.HttpClient(host=host, port=port)
        collection = client.get_collection(name="learnings")

        # Parse exclusion IDs upfront to determine query size
        exclude_set: Set[str] = set()
        if exclude_ids:
            exclude_set = set(id.strip() for id in exclude_ids.split(',') if id.strip())

        # Request extra results to compensate for client-side ID exclusion
        # ChromaDB doesn't support ID exclusion at query time
        query_size = max_results + len(exclude_set)

        # Query ChromaDB (returns sorted by distance)
        results = collection.query(
            query_texts=[search_query],
            where=combined_filter,
            n_results=query_size,
            include=["documents", "metadatas", "distances"]
        )

        # Collect raw results
        raw_results: List[Dict[str, Any]] = []

        ids = results.get('ids')
        if ids and ids[0] and len(ids[0]) > 0:
            distances = results.get('distances', [[]])
            documents = results.get('documents', [[]])
            metadatas = results.get('metadatas', [[]])

            for i in range(len(ids[0])):
                distance = distances[0][i] if distances and len(distances[0]) > i else 1.0
                result_item = {
                    'id': ids[0][i],
                    'document': documents[0][i] if documents and len(documents[0]) > i else "",
                    'metadata': metadatas[0][i] if metadatas and len(metadatas[0]) > i else {},
                    'distance': round(distance, 4)
                }
                raw_results.append(result_item)

        # Apply hybrid re-ranking (keyword boost)
        reranked_results = hybrid_rerank(raw_results, query_keywords, keyword_weight)

        # Filter out excluded IDs (parsed earlier to determine query size)
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

        # Peek mode: only high_confidence, simplified output
        if peek_mode:
            if high_confidence:
                output = {
                    'status': 'found',
                    'count': len(high_confidence),
                    'learnings': high_confidence
                }
            else:
                output = {'status': 'empty'}

            print(json.dumps(output, indent=2))
            return

        # Build output
        total_found = len(high_confidence) + len(possibly_relevant)
        candidates_searched = len(results["ids"][0]) if results["ids"] else 0

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

    parser = argparse.ArgumentParser(description='Search learnings in ChromaDB')
    parser.add_argument('query', help='Search query text')
    parser.add_argument('working_dir', nargs='?', default=None, help='Working directory')
    parser.add_argument('--peek', action='store_true',
                        help='Peek mode: only high confidence, no possibly_relevant')
    parser.add_argument('--exclude-ids', type=str, default='',
                        help='Comma separated learning IDs to exclude from results')

    args = parser.parse_args()
    search_learnings(args.query, args.working_dir, peek_mode=args.peek, exclude_ids=args.exclude_ids)
