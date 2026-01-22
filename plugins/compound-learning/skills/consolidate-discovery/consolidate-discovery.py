#!/usr/bin/env python3
"""
Discover consolidation candidates in ChromaDB.
Returns compact output to avoid token bloat.
"""

import chromadb
import sys
import json
import os
import argparse
from pathlib import Path
from typing import Dict, List, Any, Set


def load_config() -> Dict[str, Any]:
    """Load configuration from environment variables, then config file, then defaults."""
    home = os.path.expanduser('~')

    defaults = {
        'chromadb': {
            'host': 'localhost',
            'port': 8000,
        },
        'consolidation': {
            'duplicateThreshold': 0.25,  # Tighter threshold for true duplicates
            'scopeKeywords': ['security', 'authentication', 'jwt', 'oauth', 'encryption',
                             'password', 'token', 'api-key', 'secret', 'xss', 'sql-injection'],
            'outdatedKeywords': ['temporary', 'workaround', 'deprecated', 'todo', 'fixme',
                                'hack', 'remove later', 'obsolete']
        }
    }

    env_port = os.environ.get('CHROMADB_PORT')
    parsed_port = None
    if env_port:
        try:
            parsed_port = int(env_port)
        except ValueError:
            pass

    env_config = {
        'chromadb': {
            'host': os.environ.get('CHROMADB_HOST'),
            'port': parsed_port,
        }
    }

    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT', os.path.join(home, '.claude'))
    config_file = Path(plugin_root) / '.claude-plugin' / 'config.json'
    file_config: Dict[str, Any] = {'chromadb': {}}

    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                file_config = json.load(f)
        except Exception:
            pass

    result = defaults.copy()
    for section in ['chromadb']:
        if section in file_config:
            for key, value in file_config[section].items():
                if value is not None:
                    result[section][key] = value
        if section in env_config:
            for key, value in env_config[section].items():
                if value is not None:
                    result[section][key] = value

    return result


def find_duplicate_clusters(collection, threshold: float, limit: int = 20) -> List[Dict[str, Any]]:
    """Find clusters of similar documents. Returns compact format."""
    seen_ids: Set[str] = set()
    clusters: List[Dict[str, Any]] = []

    all_docs = collection.get(include=["metadatas", "documents"])
    if not all_docs or not all_docs.get('ids'):
        return clusters

    all_ids = all_docs['ids']
    id_to_meta = {all_ids[i]: all_docs['metadatas'][i] for i in range(len(all_ids))}
    id_to_doc = {all_ids[i]: all_docs['documents'][i] for i in range(len(all_ids))}

    for doc_id in all_ids:
        if doc_id in seen_ids:
            continue

        doc_content = id_to_doc.get(doc_id, "")
        if not doc_content:
            continue

        similar = collection.query(
            query_texts=[doc_content],
            n_results=min(5, len(all_ids)),
            include=["distances"]
        )

        if not similar.get('ids') or not similar['ids'][0]:
            continue

        cluster_ids = []
        for other_id, distance in zip(similar['ids'][0], similar['distances'][0]):
            if distance <= threshold and other_id not in seen_ids:
                cluster_ids.append(other_id)
                seen_ids.add(other_id)

        if len(cluster_ids) >= 2:
            # Compact format: just file basenames and IDs
            files = []
            for cid in cluster_ids:
                meta = id_to_meta.get(cid, {})
                fp = meta.get('file_path', '')
                files.append({
                    'id': cid,
                    'file': os.path.basename(fp) if fp else cid[:8],
                    'path': fp
                })
            clusters.append({
                'files': files,
                'count': len(files)
            })

            if len(clusters) >= limit:
                break

    return clusters


def find_outdated_candidates(collection, outdated_keywords: List[str], limit: int = 20) -> List[Dict[str, Any]]:
    """Find learnings with outdated markers. Returns compact format."""
    candidates: List[Dict[str, Any]] = []

    all_docs = collection.get(include=["documents", "metadatas"])
    if not all_docs or not all_docs.get('ids'):
        return candidates

    for i, doc_id in enumerate(all_docs['ids']):
        doc_content = all_docs['documents'][i] if all_docs.get('documents') else ""
        metadata = all_docs['metadatas'][i] if all_docs.get('metadatas') else {}

        content_lower = doc_content.lower()
        matching = [kw for kw in outdated_keywords if kw.lower() in content_lower]

        if matching:
            fp = metadata.get('file_path', '')
            candidates.append({
                'id': doc_id,
                'file': os.path.basename(fp) if fp else doc_id[:8],
                'path': fp,
                'markers': matching[:2]  # Limit markers shown
            })

            if len(candidates) >= limit:
                break

    return candidates


def run_discovery(mode: str = 'all', limit: int = 20) -> None:
    """Run consolidation discovery and output compact results."""
    config = load_config()
    host = config['chromadb']['host']
    port = config['chromadb']['port']

    try:
        client = chromadb.HttpClient(host=host, port=port)
        collection = client.get_collection(name="learnings")
    except Exception as e:
        print(json.dumps({
            'status': 'error',
            'message': f'Failed to connect to ChromaDB: {e}',
            'hint': 'Run /start-learning-db first'
        }, indent=2))
        sys.exit(1)

    results: Dict[str, Any] = {
        'status': 'success',
        'total_documents': collection.count(),
        'duplicates': [],
        'outdated': []
    }

    if mode in ['all', 'duplicates']:
        threshold = config['consolidation']['duplicateThreshold']
        results['duplicates'] = find_duplicate_clusters(collection, threshold, limit)

    if mode in ['all', 'outdated']:
        keywords = config['consolidation']['outdatedKeywords']
        results['outdated'] = find_outdated_candidates(collection, keywords, limit)

    results['summary'] = {
        'duplicate_clusters': len(results['duplicates']),
        'outdated_candidates': len(results['outdated']),
        'limit_applied': limit
    }

    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Discover consolidation candidates')
    parser.add_argument('--mode', choices=['all', 'duplicates', 'outdated'],
                        default='all', help='Discovery mode')
    parser.add_argument('--limit', type=int, default=20,
                        help='Max items per category (default: 20)')

    args = parser.parse_args()
    run_discovery(args.mode, args.limit)
