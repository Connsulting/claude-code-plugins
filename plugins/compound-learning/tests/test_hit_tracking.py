"""
Tests for the hit_tracker module's file write-back logic.

Uses real SQLite and real files (no mocks for internal modules).
"""

import hashlib
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = str(Path(__file__).parent.parent)
sys.path.insert(0, PLUGIN_ROOT)

import lib.db as db
from lib.hit_tracker import record_hits


SAMPLE_LEARNING = """\
# Test Learning Title

**Type:** pattern
**Topic:** testing
**Tags:** test, example

## Problem

This is the problem description.

## Solution

This is the solution.
"""

SAMPLE_LEARNING_WITH_HITS = """\
# Test Learning Title

**Type:** pattern
**Topic:** testing
**Tags:** test, example
**Hits:** 5
**Last Accessed:** 2026-01-01

## Problem

This is the problem description.

## Solution

This is the solution.
"""


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
    yield config, conn, tmp_path
    conn.close()


def _make_result(file_path: Path):
    doc_id = hashlib.md5(str(file_path.resolve()).encode()).hexdigest()
    return [{
        "id": doc_id,
        "document": "test content",
        "metadata": {"file_path": str(file_path)},
        "distance": 0.3,
    }]


def test_record_hits_adds_hits_to_new_file(tmp_db):
    config, conn, tmp_path = tmp_db
    tmp_file = tmp_path / "learning-new.md"
    tmp_file.write_text(SAMPLE_LEARNING, encoding="utf-8")

    doc_id = hashlib.md5(str(tmp_file.resolve()).encode()).hexdigest()
    db.upsert_document(conn, doc_id, "test content", {
        "scope": "global", "repo": "", "file_path": str(tmp_file),
        "topic": "testing", "keywords": "test",
    })

    results = _make_result(tmp_file)
    record_hits(config, results, today="2026-03-13")

    content = tmp_file.read_text(encoding="utf-8")
    assert "**Hits:** 1" in content
    assert "**Last Accessed:** 2026-03-13" in content


def test_record_hits_increments_existing(tmp_db):
    config, conn, tmp_path = tmp_db
    tmp_file = tmp_path / "learning-existing.md"
    tmp_file.write_text(SAMPLE_LEARNING_WITH_HITS, encoding="utf-8")

    doc_id = hashlib.md5(str(tmp_file.resolve()).encode()).hexdigest()
    db.upsert_document(conn, doc_id, "test content", {
        "scope": "global", "repo": "", "file_path": str(tmp_file),
        "topic": "testing", "keywords": "test", "access_count": 5,
    })

    results = _make_result(tmp_file)
    record_hits(config, results, today="2026-03-13")

    content = tmp_file.read_text(encoding="utf-8")
    assert "**Hits:** 6" in content
    assert "**Hits:** 5" not in content


def test_record_hits_updates_last_accessed(tmp_db):
    config, conn, tmp_path = tmp_db
    tmp_file = tmp_path / "learning-date.md"
    tmp_file.write_text(SAMPLE_LEARNING_WITH_HITS, encoding="utf-8")

    doc_id = hashlib.md5(str(tmp_file.resolve()).encode()).hexdigest()
    db.upsert_document(conn, doc_id, "test content", {
        "scope": "global", "repo": "", "file_path": str(tmp_file),
        "topic": "testing", "keywords": "test",
    })

    results = _make_result(tmp_file)
    record_hits(config, results, today="2026-03-13")

    content = tmp_file.read_text(encoding="utf-8")
    assert "**Last Accessed:** 2026-03-13" in content
    assert "**Last Accessed:** 2026-01-01" not in content


def test_record_hits_preserves_body(tmp_db):
    config, conn, tmp_path = tmp_db
    tmp_file = tmp_path / "learning-body.md"
    tmp_file.write_text(SAMPLE_LEARNING, encoding="utf-8")

    doc_id = hashlib.md5(str(tmp_file.resolve()).encode()).hexdigest()
    db.upsert_document(conn, doc_id, "test content", {
        "scope": "global", "repo": "", "file_path": str(tmp_file),
        "topic": "testing", "keywords": "test",
    })

    results = _make_result(tmp_file)
    record_hits(config, results, today="2026-03-13")

    content = tmp_file.read_text(encoding="utf-8")
    assert "## Problem" in content
    assert "This is the problem description." in content
    assert "## Solution" in content
    assert "This is the solution." in content


def test_record_hits_missing_file_no_raise(tmp_db):
    config, conn, tmp_path = tmp_db
    missing_file = tmp_path / "nonexistent.md"

    doc_id = hashlib.md5(str(missing_file.resolve()).encode()).hexdigest()
    db.upsert_document(conn, doc_id, "test content", {
        "scope": "global", "repo": "", "file_path": str(missing_file),
        "topic": "testing", "keywords": "test",
    })

    results = _make_result(missing_file)
    record_hits(config, results, today="2026-03-13")


def test_record_hits_insertion_position(tmp_db):
    config, conn, tmp_path = tmp_db
    tmp_file = tmp_path / "learning-position.md"
    tmp_file.write_text(SAMPLE_LEARNING, encoding="utf-8")

    doc_id = hashlib.md5(str(tmp_file.resolve()).encode()).hexdigest()
    db.upsert_document(conn, doc_id, "test content", {
        "scope": "global", "repo": "", "file_path": str(tmp_file),
        "topic": "testing", "keywords": "test",
    })

    results = _make_result(tmp_file)
    record_hits(config, results, today="2026-03-13")

    content = tmp_file.read_text(encoding="utf-8")
    tags_pos = content.index("**Tags:**")
    hits_pos = content.index("**Hits:**")
    problem_pos = content.index("## Problem")
    assert hits_pos > tags_pos, "**Hits:** should appear after **Tags:**"
    assert hits_pos < problem_pos, "**Hits:** should appear before ## Problem"
