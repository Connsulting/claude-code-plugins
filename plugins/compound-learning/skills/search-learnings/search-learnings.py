#!/usr/bin/env python3
"""
Search learnings in ChromaDB with distance threshold filtering
Filters results before outputting to avoid context pollution
"""

import chromadb
import sys
import json
import os
from pathlib import Path
from typing import List, Dict, Any


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
            'possiblyRelevantThreshold': 0.7
        }
    }

    # Environment variables take highest priority (with validation)
    env_port = os.environ.get('CHROMADB_PORT')
    env_high_threshold = os.environ.get('LEARNINGS_HIGH_CONFIDENCE_THRESHOLD')
    env_possible_threshold = os.environ.get('LEARNINGS_POSSIBLY_RELEVANT_THRESHOLD')
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
            'possiblyRelevantThreshold': parsed_possible_threshold
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


def search_learnings(
    query: str,
    working_dir: str | None = None,
    max_results: int = 10
) -> None:
    """
    Search learnings with tiered relevance filtering.
    Returns high confidence results (distance < 0.5) and possibly relevant (0.5-0.7).

    Args:
        query: Search query text
        working_dir: The user's working directory (where Claude was invoked)
        max_results: Maximum number of results to fetch from ChromaDB
    """
    # Load configuration
    config = load_config()
    host = config['chromadb']['host']
    port = config['chromadb']['port']
    high_threshold = config['learnings']['highConfidenceThreshold']
    possible_threshold = config['learnings']['possiblyRelevantThreshold']

    # Use provided working directory or fall back to cwd
    cwd = working_dir if working_dir else os.getcwd()
    home = os.path.expanduser('~')

    # Detect repo hierarchy
    repos = detect_learning_hierarchy(cwd, home)

    # Build scope filter
    scope_filter = build_scope_filter(repos)

    try:
        # Connect to ChromaDB
        client = chromadb.HttpClient(host=host, port=port)
        collection = client.get_collection(name="learnings")

        # Query ChromaDB (returns sorted by distance)
        results = collection.query(
            query_texts=[query],
            where=scope_filter,
            n_results=max_results,
            include=["documents", "metadatas", "distances"]
        )

        # Split results into tiers
        high_confidence: List[Dict[str, Any]] = []
        possibly_relevant: List[Dict[str, Any]] = []

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

                if distance < high_threshold:
                    high_confidence.append(result_item)
                elif distance < possible_threshold:
                    possibly_relevant.append(result_item)

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
    if len(sys.argv) < 2:
        print(json.dumps({
            'status': 'error',
            'message': 'Usage: search-learnings.py "query text" [working_dir]'
        }))
        sys.exit(1)

    query_text = sys.argv[1]
    work_dir = sys.argv[2] if len(sys.argv) > 2 else None
    search_learnings(query_text, work_dir)
