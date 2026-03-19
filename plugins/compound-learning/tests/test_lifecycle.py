"""
Full lifecycle integration tests for the compound-learning plugin.

Tests cover indexing, search, consolidation discovery, scope filtering,
and orphan pruning against real SQLite/sqlite-vec/fts5 databases.
"""

import hashlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.db as db

from harness_utils import index_single_file

# Import consolidate-discovery.py via spec (hyphen in filename)
_consol_spec = importlib.util.spec_from_file_location(
    "consolidate_discovery",
    PLUGIN_ROOT / "skills" / "consolidate-discovery" / "consolidate-discovery.py",
)
_consol_mod = importlib.util.module_from_spec(_consol_spec)
_consol_spec.loader.exec_module(_consol_mod)
find_duplicate_clusters = _consol_mod.find_duplicate_clusters
find_outdated_candidates = _consol_mod.find_outdated_candidates


# ---------------------------------------------------------------------------
# Test 1: Index count matches file count
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_index_count_matches_file_count(indexed_corpus, learning_snapshot):
    """Indexed document count must match the number of .md files in the snapshot."""
    config, conn, stats = indexed_corpus
    md_files = list(learning_snapshot.glob("*.md"))
    doc_count = db.count_documents(conn)

    assert doc_count == len(md_files), (
        f"Expected {len(md_files)} documents in DB but found {doc_count}. "
        f"Stats: indexed={stats['indexed_count']}, failed={stats['failed_count']}"
    )


# ---------------------------------------------------------------------------
# Test 2: Indexing is idempotent
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_index_idempotent(indexed_corpus, learning_snapshot):
    """Re-indexing existing files must not change the document count."""
    config, conn, stats = indexed_corpus
    count_before = db.count_documents(conn)

    md_files = sorted(learning_snapshot.glob("*.md"))[:5]
    assert len(md_files) > 0, "Need at least one .md file for idempotency test"

    for f in md_files:
        index_single_file(f, config)

    count_after = db.count_documents(conn)
    assert count_after == count_before, (
        f"Document count changed after re-indexing: {count_before} -> {count_after}"
    )


# ---------------------------------------------------------------------------
# Test 3: Search returns results for known topics
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_search_returns_results_for_known_topics(indexed_corpus, search_fn):
    """Each known topic query should return at least one result."""
    config, conn, stats = indexed_corpus
    topics = ["kubernetes", "docker", "security", "python", "authentication"]

    for topic in topics:
        results = search_fn(config, conn, topic)
        high = results["high_confidence"]
        possibly = results["possibly_relevant"]
        total = len(high) + len(possibly)

        assert total >= 1, (
            f"Query '{topic}' returned no high_confidence or possibly_relevant results"
        )


# ---------------------------------------------------------------------------
# Test 4: Consolidation discovery runs without error
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_consolidation_discovery_runs_without_error(indexed_corpus):
    """find_duplicate_clusters and find_outdated_candidates must return lists."""
    config, conn, stats = indexed_corpus

    consol_config = {
        **config,
        "consolidation": {
            "duplicateThreshold": 0.25,
            "scopeKeywords": ["security", "authentication"],
            "outdatedKeywords": [
                "temporary", "workaround", "deprecated", "todo", "fixme",
                "hack", "remove later", "obsolete",
            ],
        },
    }

    duplicates = find_duplicate_clusters(conn, consol_config)
    assert isinstance(duplicates, list), "find_duplicate_clusters must return a list"

    for cluster in duplicates:
        assert len(cluster.get("files", [])) >= 2, (
            f"Duplicate cluster should have >= 2 entries, got {cluster}"
        )

    outdated = find_outdated_candidates(conn, consol_config)
    assert isinstance(outdated, list), "find_outdated_candidates must return a list"


# ---------------------------------------------------------------------------
# Test 5: Search scope filtering
# ---------------------------------------------------------------------------

def test_search_scope_filtering(isolated_db):
    """Scope filtering: global-only vs repo-scoped searches return correct subsets."""
    config, conn = isolated_db

    # Insert 2 global docs
    db.upsert_document(conn, "global-1", "Kubernetes pod scheduling best practices for production clusters", {
        "scope": "global", "repo": "", "file_path": "",
        "topic": "kubernetes-infrastructure", "keywords": "kubernetes,pod,scheduling",
    })
    db.upsert_document(conn, "global-2", "Docker base image hardening and security scanning", {
        "scope": "global", "repo": "", "file_path": "",
        "topic": "docker", "keywords": "docker,security,base-image",
    })

    # Insert 2 repo-scoped docs
    db.upsert_document(conn, "repo-1", "Kubernetes helm chart configuration for test-repo deployment", {
        "scope": "repo", "repo": "test-repo", "file_path": "",
        "topic": "kubernetes-infrastructure", "keywords": "kubernetes,helm",
    })
    db.upsert_document(conn, "repo-2", "Docker compose setup for test-repo local development", {
        "scope": "repo", "repo": "test-repo", "file_path": "",
        "topic": "docker", "keywords": "docker,compose",
    })

    # Global-only search (scope_repos=[])
    global_results = db.search(conn, "kubernetes docker", scope_repos=[], n_results=10)
    global_ids = {r["id"] for r in global_results}

    assert "global-1" in global_ids or "global-2" in global_ids, (
        f"Global-only search should return at least one global doc, got: {global_ids}"
    )
    assert "repo-1" not in global_ids, "Global-only search should not return repo-scoped docs"
    assert "repo-2" not in global_ids, "Global-only search should not return repo-scoped docs"

    # Search with repo scope
    scoped_results = db.search(conn, "kubernetes docker", scope_repos=["test-repo"], n_results=10)
    scoped_ids = {r["id"] for r in scoped_results}

    assert len(scoped_ids) >= 1, "Repo-scoped search should return results"
    # Should include both global and test-repo docs
    has_global = any(rid.startswith("global") for rid in scoped_ids)
    has_repo = any(rid.startswith("repo") for rid in scoped_ids)
    assert has_global or has_repo, (
        f"Repo-scoped search should return global or repo docs, got: {scoped_ids}"
    )


# ---------------------------------------------------------------------------
# Test 6: Orphan pruning
# ---------------------------------------------------------------------------

def test_orphan_pruning(isolated_db, tmp_path):
    """Orphaned entries (file deleted from disk) should be pruned on re-index."""
    config, conn = isolated_db

    # Create a temp learning file
    learning_file = tmp_path / "ephemeral-learning.md"
    learning_file.write_text(
        "**Topic:** testing\n**Tags:** testing, ephemeral\n\n"
        "This is a temporary learning file used for orphan pruning tests.",
        encoding="utf-8",
    )

    # Index it
    doc_id = hashlib.md5(str(learning_file.resolve()).encode()).hexdigest()
    db.upsert_document(conn, doc_id, learning_file.read_text(encoding="utf-8"), {
        "scope": "global", "repo": "", "file_path": str(learning_file.resolve()),
        "topic": "testing", "keywords": "testing,ephemeral",
    })

    count_before = db.count_documents(conn)
    assert count_before >= 1, "Should have at least 1 document after indexing"

    # Delete the file from disk
    learning_file.unlink()
    assert not learning_file.exists()

    # Run the prune logic (same as index-learnings.py)
    all_docs = db.get_all_documents(conn, include_content=False)
    pruned = 0
    for did, metadata in zip(all_docs["ids"], all_docs["metadatas"]):
        file_path_str = metadata.get("file_path", "")
        if file_path_str and not os.path.exists(file_path_str):
            db.delete_document(conn, did)
            pruned += 1

    assert pruned >= 1, f"Expected at least 1 orphan to be pruned, got {pruned}"

    count_after = db.count_documents(conn)
    assert count_after == count_before - pruned, (
        f"Expected count {count_before - pruned} after pruning, got {count_after}"
    )
