#!/usr/bin/env python3
"""
Discover consolidation candidates in SQLite.
Returns compact output to avoid token bloat.
"""

import sys
import os

# Locate plugin root so lib/ is importable regardless of cwd
_PLUGIN_ROOT = os.environ.get('CLAUDE_PLUGIN_ROOT', os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _PLUGIN_ROOT)

import json
import argparse
from typing import Dict, List, Any, Set

import lib.db as db


def find_duplicate_clusters(conn, config: Dict[str, Any], limit: int = 20) -> List[Dict[str, Any]]:
    """Find clusters of similar documents. Returns compact format."""
    threshold = config['consolidation']['duplicateThreshold']

    seen_ids: Set[str] = set()
    clusters: List[Dict[str, Any]] = []

    all_docs = db.get_all_documents(conn, include_content=True)
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

        # Search for similar documents using this doc's content as query
        # All repos in scope since we're comparing across the entire collection
        similar = db.search(conn, doc_content, scope_repos=[], n_results=min(5, len(all_ids)), threshold=1.0)

        # Override scope filtering: search returns global-only by default; here we want all docs
        # We re-query using raw vector lookup to avoid scope restriction
        similar = _search_all_scopes(conn, doc_content, n_results=min(5, len(all_ids)))

        cluster_ids = []
        for result in similar:
            other_id = result['id']
            distance = result['distance']
            if distance <= threshold and other_id not in seen_ids:
                cluster_ids.append(other_id)
                seen_ids.add(other_id)

        if len(cluster_ids) >= 2:
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


def _search_all_scopes(conn, query_text: str, n_results: int) -> List[Dict[str, Any]]:
    """Search across all scopes (no scope filter) for duplicate detection."""
    import struct

    query_emb = db.get_embedding(query_text)
    embedding_blob = struct.pack(f'{len(query_emb)}f', *query_emb)

    try:
        rows = conn.execute(
            """
            SELECT l.id, v.distance
            FROM vec_learnings v
            JOIN learnings l ON l.id = v.id
            WHERE v.embedding MATCH ?
              AND k = ?
            ORDER BY v.distance
            """,
            [embedding_blob, n_results],
        ).fetchall()

        return [{'id': row['id'], 'distance': float(row['distance'])} for row in rows]
    except Exception:
        return []


def find_outdated_candidates(conn, config: Dict[str, Any], limit: int = 20) -> List[Dict[str, Any]]:
    """Find learnings with outdated markers. Returns compact format."""
    outdated_keywords = config['consolidation']['outdatedKeywords']
    candidates: List[Dict[str, Any]] = []

    all_docs = db.get_all_documents(conn, include_content=True)
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
                'markers': matching[:2]
            })

            if len(candidates) >= limit:
                break

    return candidates


def run_discovery(mode: str = 'all', limit: int = 20) -> None:
    """Run consolidation discovery and output compact results."""
    config = db.load_config()

    try:
        conn = db.get_connection(config)
    except Exception as e:
        print(json.dumps({
            'status': 'error',
            'message': f'Failed to open database: {e}',
            'hint': 'Run /index-learnings first'
        }, indent=2))
        sys.exit(1)

    results: Dict[str, Any] = {
        'status': 'success',
        'total_documents': db.count_documents(conn),
        'duplicates': [],
        'outdated': []
    }

    if mode in ['all', 'duplicates']:
        results['duplicates'] = find_duplicate_clusters(conn, config, limit)

    if mode in ['all', 'outdated']:
        results['outdated'] = find_outdated_candidates(conn, config, limit)

    conn.close()

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
