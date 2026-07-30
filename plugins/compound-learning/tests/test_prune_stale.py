"""Tests for scripts/prune-stale.py.

This script archives learnings and deletes rows, so the tests focus on the
guards rather than the happy path: what must NOT be swept, and the refusal that
prevents a sweep against stale usefulness data.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.db as db  # noqa: E402


def _load_prune():
    spec = importlib.util.spec_from_file_location(
        "prune_stale", PLUGIN_ROOT / "scripts" / "prune-stale.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ps = _load_prune()


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _add_row(conn, doc_id, file_path, created_at, access_count):
    conn.execute(
        'INSERT INTO learnings '
        '(id, content, scope, repo, file_path, topic, keywords, created_at, access_count) '
        "VALUES (?,?,'global','',?,'other','',?,?)",
        (doc_id, 'body text for ' + doc_id, file_path, created_at, access_count),
    )
    conn.commit()


def _seed_usefulness(conn, rows):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS peek_usefulness (
               file_name TEXT PRIMARY KEY,
               injections INTEGER NOT NULL DEFAULT 0,
               substantive INTEGER NOT NULL DEFAULT 0,
               ack_only INTEGER NOT NULL DEFAULT 0,
               last_substantive TEXT,
               window_days INTEGER NOT NULL DEFAULT 30,
               computed_at TEXT NOT NULL
           )"""
    )
    conn.executemany(
        'INSERT OR REPLACE INTO peek_usefulness '
        '(file_name, injections, substantive, computed_at) VALUES (?,?,?,?)',
        [(f, i, s, '2026-07-29T00:00:00+00:00') for f, i, s in rows],
    )
    conn.commit()


def test_grace_protects_recent_rows(isolated_db) -> None:
    config, conn = isolated_db
    _seed_usefulness(conn, [("x.md", 1, 0)])
    _add_row(conn, "old", "/l/old.md", _iso(20), 0)
    _add_row(conn, "new", "/l/new.md", _iso(3), 0)

    ids = {c["id"] for c in ps.find_candidates(conn, 14, None)}

    assert ids == {"old"}


def test_grace_boundary_uses_iso_t_comparison(isolated_db) -> None:
    """A row timestamped midnight on the boundary day must still be a candidate.

    Regression guard for a real defect: `created_at < datetime('now','-14 days')`
    compares an ISO 'T' timestamp against SQLite's space-separated one, and 'T'
    (0x54) sorts above ' ' (0x20), so every row dated on the boundary day was
    silently excluded. That undercounted the aged-out cohort by 76 of 381 rows.
    """
    config, conn = isolated_db
    _seed_usefulness(conn, [("x.md", 1, 0)])
    boundary_day = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
    _add_row(conn, "boundary", "/l/boundary.md", f"{boundary_day}T00:00:00+00:00", 0)

    ids = {c["id"] for c in ps.find_candidates(conn, 14, None)}

    assert "boundary" in ids


def test_accessed_rows_are_never_candidates(isolated_db) -> None:
    config, conn = isolated_db
    _seed_usefulness(conn, [("x.md", 1, 0)])
    _add_row(conn, "used", "/l/used.md", _iso(90), 3)

    assert ps.find_candidates(conn, 14, None) == []


def test_substantive_use_protects_a_never_accessed_row(isolated_db) -> None:
    """access_count is not the only usefulness signal.

    Peek injections deliberately do not increment access_count, so a learning
    cited through auto-peek looks untouched by that counter alone. Sweeping on
    access_count only would delete exactly the learnings that proved useful.
    """
    config, conn = isolated_db
    _seed_usefulness(conn, [("cited.md", 40, 2), ("uncited.md", 40, 0)])
    _add_row(conn, "cited", "/l/cited.md", _iso(90), 0)
    _add_row(conn, "uncited", "/l/uncited.md", _iso(90), 0)

    ids = {c["id"] for c in ps.find_candidates(conn, 14, None)}

    assert ids == {"uncited"}


def test_limit_bounds_the_batch(isolated_db) -> None:
    config, conn = isolated_db
    _seed_usefulness(conn, [("x.md", 1, 0)])
    for i in range(5):
        _add_row(conn, f"r{i}", f"/l/r{i}.md", _iso(30 + i), 0)

    assert len(ps.find_candidates(conn, 14, 2)) == 2


def test_usefulness_availability_reported(isolated_db) -> None:
    config, conn = isolated_db

    assert ps.usefulness_available(conn) is False

    _seed_usefulness(conn, [("x.md", 1, 0)])

    assert ps.usefulness_available(conn) is True


def test_archive_moves_file_and_reports_destination(isolated_db, tmp_path) -> None:
    config, conn = isolated_db
    src = tmp_path / "doomed.md"
    src.write_text("# doomed\n")
    target = tmp_path / "archive"
    target.mkdir()

    outcome = ps.archive_one({"file_path": str(src)}, str(target))

    assert not src.exists()
    assert (target / "doomed.md").exists()
    assert outcome["archived_to"] == str(target / "doomed.md")


def test_archive_does_not_clobber_an_existing_file(isolated_db, tmp_path) -> None:
    config, conn = isolated_db
    src = tmp_path / "dupe.md"
    src.write_text("# second\n")
    target = tmp_path / "archive"
    target.mkdir()
    (target / "dupe.md").write_text("# first\n")

    outcome = ps.archive_one({"file_path": str(src)}, str(target))

    assert (target / "dupe.md").read_text() == "# first\n"
    assert outcome["archived_to"].endswith("dupe_1.md")


def test_missing_file_is_row_only(isolated_db, tmp_path) -> None:
    config, conn = isolated_db

    outcome = ps.archive_one({"file_path": str(tmp_path / "gone.md")}, str(tmp_path))

    assert "note" in outcome
    assert "archived_to" not in outcome
