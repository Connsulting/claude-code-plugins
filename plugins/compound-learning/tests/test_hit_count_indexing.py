"""
Tests for hit count schema migration, upsert, increment, and indexer integration.

Uses real SQLite (no mocks for internal modules).
"""

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = str(Path(__file__).parent.parent)
sys.path.insert(0, PLUGIN_ROOT)

import lib.db as db

_idx_spec = importlib.util.spec_from_file_location(
    "index_learnings",
    Path(PLUGIN_ROOT) / "skills" / "index-learnings" / "index-learnings.py",
)
_idx_mod = importlib.util.module_from_spec(_idx_spec)
_idx_spec.loader.exec_module(_idx_mod)

extract_hits = _idx_mod.extract_hits
extract_last_accessed = _idx_mod.extract_last_accessed
index_single_file = _idx_mod.index_single_file


@pytest.fixture(scope="module")
def embedding_model():
    db.get_embedding("warmup")
    yield


@pytest.fixture()
def tmp_db(tmp_path, embedding_model):
    db_path = str(tmp_path / "test.db")
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


def test_schema_has_access_count_and_last_accessed(tmp_db):
    _, conn = tmp_db
    rows = conn.execute("PRAGMA table_info(learnings)").fetchall()
    col_names = {row["name"] for row in rows}
    assert "access_count" in col_names
    assert "last_accessed" in col_names


def test_upsert_with_access_count(tmp_db):
    _, conn = tmp_db
    db.upsert_document(
        conn,
        "doc-ac-5",
        "test content for access count",
        {
            "scope": "global",
            "repo": "",
            "file_path": "/tmp/test.md",
            "topic": "testing",
            "keywords": "test",
            "access_count": 5,
            "last_accessed": "2026-03-13",
        },
    )
    row = conn.execute(
        "SELECT access_count, last_accessed FROM learnings WHERE id = ?",
        ("doc-ac-5",),
    ).fetchone()
    assert row["access_count"] == 5
    assert row["last_accessed"] == "2026-03-13"


def test_upsert_default_access_count(tmp_db):
    _, conn = tmp_db
    db.upsert_document(
        conn,
        "doc-default-ac",
        "test content no access count",
        {
            "scope": "global",
            "repo": "",
            "file_path": "/tmp/test2.md",
            "topic": "testing",
            "keywords": "test",
        },
    )
    row = conn.execute(
        "SELECT access_count, last_accessed FROM learnings WHERE id = ?",
        ("doc-default-ac",),
    ).fetchone()
    assert row["access_count"] == 0
    assert row["last_accessed"] is None


def test_increment_hit_count(tmp_db):
    _, conn = tmp_db
    db.upsert_document(
        conn,
        "doc-inc",
        "test content for increment",
        {
            "scope": "global",
            "repo": "",
            "file_path": "/tmp/test3.md",
            "topic": "testing",
            "keywords": "test",
        },
    )
    db.increment_hit_count(conn, "doc-inc", "2026-03-13")
    db.increment_hit_count(conn, "doc-inc", "2026-03-13")
    row = conn.execute(
        "SELECT access_count, last_accessed FROM learnings WHERE id = ?",
        ("doc-inc",),
    ).fetchone()
    assert row["access_count"] == 2
    assert row["last_accessed"] == "2026-03-13"


def test_search_returns_access_count(tmp_db):
    _, conn = tmp_db
    db.upsert_document(
        conn,
        "doc-search-ac",
        "unique xylophone content for search access count verification",
        {
            "scope": "global",
            "repo": "",
            "file_path": "/tmp/test4.md",
            "topic": "testing",
            "keywords": "xylophone",
            "access_count": 3,
            "last_accessed": "2026-03-10",
        },
    )
    results = db.search(conn, "unique xylophone content", scope_repos=[], n_results=10, threshold=1.0)
    matched = [r for r in results if r["id"] == "doc-search-ac"]
    assert len(matched) == 1
    assert matched[0]["metadata"]["access_count"] == 3


def test_extract_hits_parses_correctly():
    assert extract_hits("**Hits:** 42\n**Topic:** testing") == 42
    assert extract_hits("No hits field here") == 0
    assert extract_hits("**Hits:** notanumber") == 0


def test_extract_last_accessed_parses_correctly():
    assert extract_last_accessed("**Last Accessed:** 2026-03-13\n") == "2026-03-13"
    assert extract_last_accessed("No last accessed field") is None


def test_indexer_reads_hits_from_file(tmp_path, embedding_model):
    learnings_dir = tmp_path / "learnings"
    learnings_dir.mkdir(parents=True)
    learning_file = learnings_dir / "test-learning.md"
    learning_file.write_text(
        "# Test Learning\n\n"
        "**Type:** pattern\n"
        "**Topic:** testing\n"
        "**Tags:** test, example\n"
        "**Hits:** 10\n"
        "**Last Accessed:** 2026-03-01\n\n"
        "## Problem\n\nThis is the problem.\n\n"
        "## Solution\n\nThis is the solution.\n",
        encoding="utf-8",
    )

    db_path = str(tmp_path / "indexer-test.db")
    config = {
        "sqlite": {"dbPath": db_path},
        "learnings": {
            "globalDir": str(learnings_dir),
            "repoSearchPath": str(tmp_path),
            "highConfidenceThreshold": 0.40,
            "possiblyRelevantThreshold": 0.55,
            "keywordBoostWeight": 0.65,
        },
    }

    success = index_single_file(learning_file, config)
    assert success is True

    conn = db.get_connection(config)
    try:
        doc_id = hashlib.md5(str(learning_file.resolve()).encode()).hexdigest()
        row = conn.execute(
            "SELECT access_count, last_accessed FROM learnings WHERE id = ?",
            (doc_id,),
        ).fetchone()
        assert row is not None
        assert row["access_count"] == 10
        assert row["last_accessed"] == "2026-03-01"
    finally:
        conn.close()


def test_schema_migration_idempotent(tmp_path, embedding_model):
    db_path = str(tmp_path / "migration-test.db")
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

    conn1 = db.get_connection(config)
    conn1.close()

    conn2 = db.get_connection(config)
    try:
        rows = conn2.execute("PRAGMA table_info(learnings)").fetchall()
        col_names = {row["name"] for row in rows}
        assert "access_count" in col_names
        assert "last_accessed" in col_names
    finally:
        conn2.close()
