#!/usr/bin/env python3
"""
Shared SQLite + sqlite-vec database module for compound-learning plugin.
All four Python scripts import from here instead of duplicating database boilerplate.
"""

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

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
_model_lock = threading.Lock()


import lib.observability as observability


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {'1', 'true', 'yes', 'on'}:
            return True
        if normalized in {'0', 'false', 'no', 'off'}:
            return False
    return None


def _normalize_level(value: Any) -> str:
    level = str(value or 'info').strip().lower()
    if level == 'warning':
        level = 'warn'
    if level not in {'debug', 'info', 'warn', 'error'}:
        return 'info'
    return level


def _db_logger(
    config: Optional[Mapping[str, Any]],
    **fields: Any,
) -> observability.StructuredLogger:
    return observability.get_logger('db', config, **fields)


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
            'highConfidenceThreshold': 0.40,
            'possiblyRelevantThreshold': 0.55,
            'keywordBoostWeight': 0.65,
        },
        'consolidation': {
            'duplicateThreshold': 0.25,
            'scopeKeywords': ['security', 'authentication', 'jwt', 'oauth', 'encryption',
                              'password', 'token', 'api-key', 'secret', 'xss', 'sql-injection'],
            'outdatedKeywords': ['temporary', 'workaround', 'deprecated', 'todo', 'fixme',
                                 'hack', 'remove later', 'obsolete'],
        },
        'observability': {
            'enabled': False,
            'level': 'info',
            'logPath': os.path.join(home, '.claude', 'plugins', 'compound-learning', 'observability.jsonl'),
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
    env_obs_enabled = os.environ.get('LEARNINGS_OBS_ENABLED')
    env_obs_level = os.environ.get('LEARNINGS_OBS_LEVEL')
    env_obs_log_path = os.environ.get('LEARNINGS_OBS_LOG_PATH')

    result = _deep_merge(defaults, file_config)

    if env_db_path:
        result['sqlite']['dbPath'] = os.path.expanduser(env_db_path)
    if env_global_dir:
        result['learnings']['globalDir'] = os.path.expanduser(env_global_dir)
    if env_repo_path:
        result['learnings']['repoSearchPath'] = os.path.expanduser(env_repo_path)
    if env_obs_enabled is not None:
        parsed = _parse_bool(env_obs_enabled)
        if parsed is not None:
            result['observability']['enabled'] = parsed
    if env_obs_level:
        result['observability']['level'] = _normalize_level(env_obs_level)
    if env_obs_log_path:
        result['observability']['logPath'] = os.path.expanduser(env_obs_log_path)

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
    logger = _db_logger(config, db_path=db_path)
    started = observability.now_perf()
    logger.emit('connection_open', 'start', level='debug')

    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        _create_schema(conn, logger=logger)
        logger.emit(
            'connection_open',
            'success',
            duration_ms=observability.elapsed_ms(started),
        )
        return conn
    except Exception as exc:
        logger.emit(
            'connection_open',
            'error',
            level='error',
            duration_ms=observability.elapsed_ms(started),
            error=exc,
        )
        raise


def _create_schema(
    conn: sqlite3.Connection,
    logger: observability.StructuredLogger | None = None,
) -> None:
    started = observability.now_perf()
    if logger:
        logger.emit('schema_init', 'start', level='debug')
    try:
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
        if logger:
            logger.emit(
                'schema_init',
                'success',
                duration_ms=observability.elapsed_ms(started),
            )
    except Exception as exc:
        if logger:
            logger.emit(
                'schema_init',
                'error',
                level='error',
                duration_ms=observability.elapsed_ms(started),
                error=exc,
            )
        raise


def get_embedding(text: str, config: Optional[Dict[str, Any]] = None):
    """Lazy-load sentence-transformers model and return embedding as list."""
    global _model
    logger = _db_logger(config, text_length=len(text))
    encode_started = observability.now_perf()

    with _model_lock:
        if _model is None:
            model_cache = os.path.expanduser(
                '~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2'
            )
            load_started = observability.now_perf()
            logger.emit(
                'embedding_model_load',
                'start',
                level='debug',
                model='all-MiniLM-L6-v2',
            )
            if not os.path.exists(model_cache):
                print("Downloading embedding model (one-time, ~80MB)...", file=sys.stderr)
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer('all-MiniLM-L6-v2')
            logger.emit(
                'embedding_model_load',
                'success',
                duration_ms=observability.elapsed_ms(load_started),
                model='all-MiniLM-L6-v2',
            )
    try:
        embedding = _model.encode(text, normalize_embeddings=True)
        logger.emit(
            'embedding_encode',
            'success',
            level='debug',
            duration_ms=observability.elapsed_ms(encode_started),
            counts={'dimensions': len(embedding)},
        )
        return embedding.tolist()
    except Exception as exc:
        logger.emit(
            'embedding_encode',
            'error',
            level='error',
            duration_ms=observability.elapsed_ms(encode_started),
            error=exc,
        )
        raise


def upsert_document(
    conn: sqlite3.Connection,
    doc_id: str,
    content: str,
    metadata: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> None:
    """Atomic upsert into learnings + vec_learnings + fts_learnings."""
    from datetime import datetime, timezone

    logger = _db_logger(
        config,
        doc_id=doc_id,
        scope=metadata.get('scope', 'global'),
        repo=metadata.get('repo', ''),
    )
    started = observability.now_perf()
    logger.emit('upsert', 'start', level='debug')

    try:
        embedding = get_embedding(content, config=config)
        created_at = metadata.get('created_at', datetime.now(timezone.utc).isoformat())

        # Delete-then-insert strategy (virtual tables don't support ON CONFLICT cleanly)
        delete_document(conn, doc_id, config=config)

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
        logger.emit(
            'upsert',
            'success',
            duration_ms=observability.elapsed_ms(started),
            counts={'embedding_dimensions': len(embedding), 'content_bytes': len(content.encode('utf-8'))},
        )
    except Exception as exc:
        logger.emit(
            'upsert',
            'error',
            level='error',
            duration_ms=observability.elapsed_ms(started),
            error=exc,
        )
        raise


def delete_document(
    conn: sqlite3.Connection,
    doc_id: str,
    config: Optional[Dict[str, Any]] = None,
) -> None:
    """Remove a document from all three tables."""
    logger = _db_logger(config, doc_id=doc_id)
    started = observability.now_perf()
    try:
        primary_deleted = conn.execute("DELETE FROM learnings WHERE id = ?", (doc_id,)).rowcount
        vec_deleted = conn.execute("DELETE FROM vec_learnings WHERE id = ?", (doc_id,)).rowcount
        fts_deleted = conn.execute("DELETE FROM fts_learnings WHERE id = ?", (doc_id,)).rowcount
        conn.commit()
        logger.emit(
            'delete',
            'success',
            level='debug',
            duration_ms=observability.elapsed_ms(started),
            counts={
                'learnings': max(0, primary_deleted),
                'vec_learnings': max(0, vec_deleted),
                'fts_learnings': max(0, fts_deleted),
            },
        )
    except Exception as exc:
        logger.emit(
            'delete',
            'error',
            level='error',
            duration_ms=observability.elapsed_ms(started),
            error=exc,
        )
        raise


def search(
    conn: sqlite3.Connection,
    query_text: str,
    scope_repos: List[str],
    n_results: int = 10,
    threshold: float = 1.0,
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    KNN vector search filtered by scope, returns list of result dicts.

    Each result has: id, document, metadata (dict), distance.
    scope_repos: list of repo names in scope (global always included).
    """
    import struct

    logger = _db_logger(
        config,
        query_preview=query_text[:120],
        scope_repo_count=len(scope_repos),
        n_results=n_results,
        threshold=threshold,
    )
    started = observability.now_perf()
    logger.emit('vector_search', 'start', level='debug')

    try:
        query_emb = get_embedding(query_text, config=config)
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

        logger.emit(
            'vector_search',
            'success',
            duration_ms=observability.elapsed_ms(started),
            counts={'raw_rows': len(rows), 'results_returned': len(results)},
        )
        return results
    except Exception as exc:
        logger.emit(
            'vector_search',
            'error',
            level='error',
            duration_ms=observability.elapsed_ms(started),
            error=exc,
        )
        raise


def get_all_documents(
    conn: sqlite3.Connection,
    include_content: bool = True,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return all documents in a dict format compatible with consolidate-discovery."""
    logger = _db_logger(config, include_content=include_content)
    started = observability.now_perf()
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

    logger.emit(
        'get_all_documents',
        'success',
        level='debug',
        duration_ms=observability.elapsed_ms(started),
        counts={'documents': len(ids)},
    )
    return {'ids': ids, 'documents': documents, 'metadatas': metadatas}


def get_documents_by_ids(
    conn: sqlite3.Connection,
    ids: List[str],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fetch specific documents by ID list."""
    logger = _db_logger(config, requested_ids=len(ids))
    started = observability.now_perf()
    if not ids:
        logger.emit(
            'get_documents_by_ids',
            'success',
            level='debug',
            duration_ms=observability.elapsed_ms(started),
            counts={'documents': 0},
        )
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

    logger.emit(
        'get_documents_by_ids',
        'success',
        level='debug',
        duration_ms=observability.elapsed_ms(started),
        counts={'documents': len(result_ids)},
    )
    return {'ids': result_ids, 'documents': result_docs, 'metadatas': result_metas}


def count_documents(conn: sqlite3.Connection, config: Optional[Dict[str, Any]] = None) -> int:
    """Return total number of indexed documents."""
    logger = _db_logger(config)
    started = observability.now_perf()
    row = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()
    total = row[0] if row else 0
    logger.emit(
        'count_documents',
        'success',
        level='debug',
        duration_ms=observability.elapsed_ms(started),
        counts={'documents': total},
    )
    return total
