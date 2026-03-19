"""Shared pytest fixtures for compound-learning tests."""

import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.db as db

from harness_utils import index_single_file, run_search_pipeline


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: marks tests as slow (indexing real corpus)")


@pytest.fixture(scope="session")
def embedding_model():
    """Warm the sentence-transformer model once per session."""
    db.get_embedding("warmup")
    yield


@pytest.fixture(scope="session")
def learning_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy ~/.projects/learnings/*.md into a temp dir. Returns the temp path.

    Falls back to bundled sample learnings when the user has no personal learnings.
    """
    source = Path.home() / ".projects" / "learnings"
    md_files = list(source.glob("*.md")) if source.exists() else []

    if not md_files:
        source = PLUGIN_ROOT / "tests" / "fixtures" / "sample_learnings"
        md_files = list(source.glob("*.md"))

    assert len(md_files) > 0, (
        "No learning files found (checked ~/.projects/learnings/ and tests/fixtures/sample_learnings/)"
    )

    dest = tmp_path_factory.mktemp("learnings")
    for f in md_files:
        shutil.copy2(f, dest / f.name)
    return dest


def _make_config(db_path: str, learnings_dir: str, tmp_dir: str) -> Dict[str, Any]:
    """Build a test config dict pointing at the given paths."""
    return {
        "sqlite": {"dbPath": db_path},
        "learnings": {
            "globalDir": learnings_dir,
            "repoSearchPath": tmp_dir,
            "highConfidenceThreshold": 0.40,
            "possiblyRelevantThreshold": 0.55,
            "keywordBoostWeight": 0.65,
        },
    }


@pytest.fixture()
def isolated_db(
    tmp_path: Path, embedding_model: Any
) -> Tuple[Dict[str, Any], Any]:
    """Fresh SQLite DB in tmp_path. Returns (config, conn)."""
    db_path = str(tmp_path / "test-compound-learning.db")
    config = _make_config(db_path, str(tmp_path / "learnings"), str(tmp_path))
    conn = db.get_connection(config)
    yield config, conn
    conn.close()


@pytest.fixture(scope="session")
def indexed_corpus(
    learning_snapshot: Path, embedding_model: Any, tmp_path_factory: pytest.TempPathFactory
) -> Tuple[Dict[str, Any], Any, Dict[str, Any]]:
    """Index all snapshotted learning files into a session-scoped DB.

    Returns (config, conn, stats) where stats has total_files, indexed_count,
    failed_count, and elapsed_seconds.
    """
    db_dir = tmp_path_factory.mktemp("corpus_db")
    db_path = str(db_dir / "corpus.db")
    config = _make_config(db_path, str(learning_snapshot), str(db_dir))

    md_files = list(learning_snapshot.glob("*.md"))
    indexed_count = 0
    failed_count = 0

    start = time.monotonic()
    for f in md_files:
        if index_single_file(f, config):
            indexed_count += 1
        else:
            failed_count += 1
    elapsed = time.monotonic() - start

    conn = db.get_connection(config)

    stats = {
        "total_files": len(md_files),
        "indexed_count": indexed_count,
        "failed_count": failed_count,
        "elapsed_seconds": round(elapsed, 2),
    }

    yield config, conn, stats
    conn.close()


@pytest.fixture()
def search_fn():
    """Convenience callable: search_fn(config, conn, query, **kwargs) -> full search results."""

    def _search(
        config: Dict[str, Any],
        conn: Any,
        query: str,
        n: int = 20,
        peek: bool = False,
    ) -> Dict[str, Any]:
        results = run_search_pipeline(config, conn, query)

        if peek:
            high_confidence = results["high_confidence"]
            possibly_relevant = results["possibly_relevant"]
            max_results = 5
            peek_results = high_confidence[:max_results]
            if len(peek_results) < max_results:
                peek_results.extend(possibly_relevant[: max_results - len(peek_results)])
            return {
                "peek_results": peek_results,
                "high_confidence": high_confidence,
                "possibly_relevant": possibly_relevant,
            }

        return results

    return _search
