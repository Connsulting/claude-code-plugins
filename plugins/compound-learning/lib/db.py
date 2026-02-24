#!/usr/bin/env python3
"""
Shared SQLite + sqlite-vec database module for compound-learning plugin.
All four Python scripts import from here instead of duplicating database boilerplate.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

try:
    import sqlite3
    _test_conn = sqlite3.connect(':memory:')
    if not hasattr(_test_conn, 'enable_load_extension'):
        raise AttributeError('no enable_load_extension')
    _test_conn.close()
except AttributeError:
    try:
        import pysqlite3 as sqlite3  # type: ignore[no-redef]
    except ImportError:
        raise ImportError(
            'sqlite3 extension loading is not available. '
            'Install pysqlite3-binary: pip install pysqlite3-binary'
        )

_model = None


def load_config() -> Dict[str, Any]:
    """Load configuration from environment variables, then config file, then defaults."""
    home = os.path.expanduser('~')

    defaults: Dict[str, Any] = {
        'sqlite': {
            'dbPath': os.path.join(home, '.claude', 'compound-learning.db'),
        },
        'learnings': {
            'globalDir': os.path.join(home, '.projects/learnings'),
            'repoSearchPath': home,
            'archiveDir': os.path.join(home, '.projects/archive/learnings'),
            'highConfidenceThreshold': 0.55,
            'possiblyRelevantThreshold': 0.70,
            'keywordBoostWeight': 0.4,
        },
        'consolidation': {
            'duplicateThreshold': 0.25,
            'scopeKeywords': ['security', 'authentication', 'jwt', 'oauth', 'encryption',
                              'password', 'token', 'api-key', 'secret', 'xss', 'sql-injection'],
            'outdatedKeywords': ['temporary', 'workaround', 'deprecated', 'todo', 'fixme',
                                 'hack', 'remove later', 'obsolete'],
        },
    }

    # Determine plugin root for config file location
    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT')
    if not plugin_root:
        # Auto-detect: this file lives at <plugin_root>/lib/db.py
        plugin_root = str(Path(__file__).parent.parent)

    config_file = Path(plugin_root) / '.claude-plugin' / 'config.json'
    file_config: Dict[str, Any] = {}

    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                file_config = json.load(f)
            # Expand ${HOME} in all string values recursively
            file_config = _expand_home(file_config, home)
        except Exception as e:
            print(f"Warning: Failed to load config from {config_file}: {e}", file=sys.stderr)

    # Env var overrides
    env_db_path = os.environ.get('SQLITE_DB_PATH')
    env_global_dir = os.environ.get('LEARNINGS_GLOBAL_DIR')
    env_repo_path = os.environ.get('LEARNINGS_REPO_SEARCH_PATH')

    result = _deep_merge(defaults, file_config)

    if env_db_path:
        result['sqlite']['dbPath'] = os.path.expanduser(env_db_path)
    if env_global_dir:
        result['learnings']['globalDir'] = os.path.expanduser(env_global_dir)
    if env_repo_path:
        result['learnings']['repoSearchPath'] = os.path.expanduser(env_repo_path)

    return result


def _expand_home(obj: Any, home: str) -> Any:
    if isinstance(obj, str):
        return obj.replace('${HOME}', home)
    if isinstance(obj, dict):
        return {k: _expand_home(v, home) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_home(v, home) for v in obj]
    return obj


def _deep_merge(base: Dict, override: Dict) -> Dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def get_connection(config: Dict[str, Any]) -> sqlite3.Connection:
    """Open SQLite DB, load sqlite-vec extension, create schema if missing."""
    import sqlite_vec

    db_path = config['sqlite']['dbPath']
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    _create_schema(conn)
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS learnings (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            scope TEXT NOT NULL,
            repo TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL,
            topic TEXT NOT NULL DEFAULT 'other',
            keywords TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS vec_learnings USING vec0(
            id TEXT PRIMARY KEY,
            embedding float[384]
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS fts_learnings USING fts5(
            id UNINDEXED,
            content,
            tokenize='porter unicode61'
        );
    """)
    conn.commit()


def get_embedding(text: str):
    """Lazy-load sentence-transformers model and return embedding as list."""
    global _model
    if _model is None:
        model_cache = os.path.expanduser(
            '~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2'
        )
        if not os.path.exists(model_cache):
            print("Downloading embedding model (one-time, ~80MB)...", file=sys.stderr)
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer('all-MiniLM-L6-v2')
    return _model.encode(text, normalize_embeddings=True).tolist()


def upsert_document(
    conn: sqlite3.Connection,
    doc_id: str,
    content: str,
    metadata: Dict[str, Any],
) -> None:
    """Atomic upsert into learnings + vec_learnings + fts_learnings."""
    from datetime import datetime, timezone

    embedding = get_embedding(content)
    created_at = metadata.get('created_at', datetime.now(timezone.utc).isoformat())

    # Delete-then-insert strategy (virtual tables don't support ON CONFLICT cleanly)
    delete_document(conn, doc_id)

    conn.execute(
        """INSERT INTO learnings (id, content, scope, repo, file_path, topic, keywords, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            doc_id,
            content,
            metadata.get('scope', 'global'),
            metadata.get('repo', ''),
            metadata.get('file_path', ''),
            metadata.get('topic', 'other'),
            metadata.get('keywords', ''),
            created_at,
        ),
    )

    import struct
    embedding_blob = struct.pack(f'{len(embedding)}f', *embedding)
    conn.execute(
        "INSERT INTO vec_learnings (id, embedding) VALUES (?, ?)",
        (doc_id, embedding_blob),
    )

    conn.execute(
        "INSERT INTO fts_learnings (id, content) VALUES (?, ?)",
        (doc_id, content),
    )

    conn.commit()


def delete_document(conn: sqlite3.Connection, doc_id: str) -> None:
    """Remove a document from all three tables."""
    conn.execute("DELETE FROM learnings WHERE id = ?", (doc_id,))
    conn.execute("DELETE FROM vec_learnings WHERE id = ?", (doc_id,))
    conn.execute("DELETE FROM fts_learnings WHERE id = ?", (doc_id,))
    conn.commit()


def search(
    conn: sqlite3.Connection,
    query_text: str,
    scope_repos: List[str],
    n_results: int = 10,
    threshold: float = 1.0,
) -> List[Dict[str, Any]]:
    """
    KNN vector search filtered by scope, returns list of result dicts.

    Each result has: id, document, metadata (dict), distance.
    scope_repos: list of repo names in scope (global always included).
    """
    import struct

    query_emb = get_embedding(query_text)
    embedding_blob = struct.pack(f'{len(query_emb)}f', *query_emb)

    # Build scope WHERE clause
    scope_conditions = ["l.scope = 'global'"]
    params: List[Any] = []
    for repo in scope_repos:
        scope_conditions.append("(l.scope = 'repo' AND l.repo = ?)")
        params.append(repo)
    scope_sql = ' OR '.join(scope_conditions)

    # KNN query joined with learnings for scope filtering
    # sqlite-vec returns distance as vec_distance_cosine
    rows = conn.execute(
        f"""
        SELECT l.id, l.content, l.scope, l.repo, l.file_path, l.topic, l.keywords,
               v.distance
        FROM vec_learnings v
        JOIN learnings l ON l.id = v.id
        WHERE v.embedding MATCH ?
          AND k = ?
          AND ({scope_sql})
        ORDER BY v.distance
        """,
        [embedding_blob, n_results * 3] + params,
    ).fetchall()

    results = []
    for row in rows:
        # sqlite-vec cosine distance is in [0, 2]; normalize to [0, 1] so
        # existing thresholds (0.5, 0.6, 0.25) work unchanged
        dist = float(row['distance']) / 2.0
        if dist > threshold:
            continue
        results.append({
            'id': row['id'],
            'document': row['content'],
            'metadata': {
                'scope': row['scope'],
                'repo': row['repo'],
                'file_path': row['file_path'],
                'topic': row['topic'],
                'keywords': row['keywords'],
            },
            'distance': round(dist, 4),
        })
        if len(results) >= n_results:
            break

    return results


def get_all_documents(
    conn: sqlite3.Connection, include_content: bool = True
) -> Dict[str, Any]:
    """Return all documents in a dict format compatible with consolidate-discovery."""
    rows = conn.execute(
        "SELECT id, content, scope, repo, file_path, topic, keywords FROM learnings"
    ).fetchall()

    ids = []
    documents = []
    metadatas = []

    for row in rows:
        ids.append(row['id'])
        documents.append(row['content'] if include_content else '')
        metadatas.append({
            'scope': row['scope'],
            'repo': row['repo'],
            'file_path': row['file_path'],
            'topic': row['topic'],
            'keywords': row['keywords'],
        })

    return {'ids': ids, 'documents': documents, 'metadatas': metadatas}


def get_documents_by_ids(
    conn: sqlite3.Connection, ids: List[str]
) -> Dict[str, Any]:
    """Fetch specific documents by ID list."""
    if not ids:
        return {'ids': [], 'documents': [], 'metadatas': []}

    placeholders = ','.join('?' * len(ids))
    rows = conn.execute(
        f"SELECT id, content, scope, repo, file_path, topic, keywords FROM learnings WHERE id IN ({placeholders})",
        ids,
    ).fetchall()

    # Preserve requested order
    row_map = {row['id']: row for row in rows}
    result_ids = []
    result_docs = []
    result_metas = []

    for doc_id in ids:
        if doc_id in row_map:
            row = row_map[doc_id]
            result_ids.append(row['id'])
            result_docs.append(row['content'])
            result_metas.append({
                'scope': row['scope'],
                'repo': row['repo'],
                'file_path': row['file_path'],
                'topic': row['topic'],
                'keywords': row['keywords'],
            })

    return {'ids': result_ids, 'documents': result_docs, 'metadatas': result_metas}


def count_documents(conn: sqlite3.Connection) -> int:
    """Return total number of indexed documents."""
    row = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()
    return row[0] if row else 0
