"""Tests for scripts/prune-stale.py.

This script archives learnings and deletes rows, so the tests focus on the
guards rather than the happy path: what must NOT be swept, and the refusal that
prevents a sweep against stale usefulness data.
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

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


# ---------------------------------------------------------------------------
# Opportunity floor, lifetime protection, and the dry-run projection.
#
# `access_count = 0` as a hard precondition grants permanent immunity to anything
# ever searched once: measured, 5,487 injections over 30 days produced 44
# substantive uses (0.80%), and the 93-row cohort with zero citations across 10+
# injections and 30+ days of age cannot be reached at all today. The fix is a
# second, independent candidate clause that does not consult access_count.
#
# The inverse must hold at the same time: eligibility is WINDOWED but protection
# is LIFETIME. A learning cited 45 days ago has no rolling-window evidence left,
# and archiving it would take the corpus's best entries first.
# ---------------------------------------------------------------------------


def _seed_lifetime(conn, rows) -> None:
    """rows: iterable of (file_name, injections, substantive)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS learning_usefulness_lifetime (
               file_name TEXT PRIMARY KEY,
               injections INTEGER NOT NULL DEFAULT 0,
               substantive INTEGER NOT NULL DEFAULT 0,
               ack_only INTEGER NOT NULL DEFAULT 0,
               last_substantive TEXT,
               first_seen TEXT NOT NULL,
               last_updated TEXT NOT NULL
           )"""
    )
    now = "2026-07-29T00:00:00+00:00"
    conn.executemany(
        "INSERT OR REPLACE INTO learning_usefulness_lifetime "
        "(file_name, injections, substantive, ack_only, first_seen, last_updated) "
        "VALUES (?,?,?,0,?,?)",
        [(f, i, s, now, now) for f, i, s in rows],
    )
    conn.commit()


def _prune_config(config, tmp_path, **learnings) -> Dict[str, Any]:
    """The isolated config, plus the keys prune-stale.py's main() reads."""
    merged: Dict[str, Any] = {
        "sqlite": dict(config["sqlite"]),
        "learnings": dict(config["learnings"]),
    }
    merged["learnings"]["archiveDir"] = str(tmp_path / "archive")
    merged["learnings"].update(learnings)
    return merged


def _run_main(monkeypatch, capsys, config, argv):
    """Drive prune-stale.py's CLI against the isolated DB. Returns (code, stdout, stderr)."""
    monkeypatch.setattr(db, "load_config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["prune-stale.py", *argv])
    code = ps.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_opportunity_floor_archives_high_injection_zero_substantive(isolated_db) -> None:
    """THE DEFECT TEST.

    A file injected 57 times over 40 days with zero citations is the exact cohort
    the retention rule exists to reach, and today it is permanently immune
    because a single explicit search set access_count to 91. Reintroducing
    `COALESCE(access_count, 0) = 0` as a precondition on the opportunity clause
    makes this fail again.
    """
    config, conn = isolated_db
    _seed_usefulness(conn, [("unrelated.md", 1, 0)])
    _seed_lifetime(conn, [("burner.md", 57, 0)])
    _add_row(conn, "burner", "/l/burner.md", _iso(40), 91)

    ids = {c["id"] for c in ps.find_candidates(conn, 14, None)}

    assert ids == {"burner"}


def test_below_opportunity_floor_is_not_a_candidate(isolated_db) -> None:
    """Nine injections is not evidence of anything.

    Guards the floor-5 rule that was measured and rejected: it took the cohort
    from 93 rows to 239, roughly 96% of the additional rows being noise. The
    high-injection sibling is present so this cannot pass by returning nothing.
    """
    config, conn = isolated_db
    _seed_usefulness(conn, [("unrelated.md", 1, 0)])
    _seed_lifetime(conn, [("quiet.md", 9, 0), ("loud.md", 57, 0)])
    _add_row(conn, "quiet", "/l/quiet.md", _iso(40), 91)
    _add_row(conn, "loud", "/l/loud.md", _iso(40), 91)

    ids = {c["id"] for c in ps.find_candidates(conn, 14, None)}

    assert ids == {"loud"}


def test_lifetime_substantive_protects_across_window_expiry(isolated_db) -> None:
    """Protection is LIFETIME even though eligibility is windowed.

    Reproduces monitoring-patterns.md's real shape: injections=57,
    substantive=2, and no row at all in the 30-day rolling table once its
    citations age out. 2 citations in 57 injections is ~4x the corpus base rate,
    so archiving it would be a false positive. Its uncited twin must still go.
    """
    config, conn = isolated_db
    _seed_usefulness(conn, [("unrelated.md", 1, 0)])
    _seed_lifetime(conn, [("monitoring-patterns.md", 57, 2), ("burner.md", 57, 0)])
    _add_row(conn, "monitoring", "/l/monitoring-patterns.md", _iso(40), 91)
    _add_row(conn, "burner", "/l/burner.md", _iso(40), 91)

    ids = {c["id"] for c in ps.find_candidates(conn, 14, None)}

    assert ids == {"burner"}


def test_pinned_file_is_never_a_candidate(isolated_db, tmp_path, monkeypatch) -> None:
    """pinned.md entries are already resident in every context.

    All four currently-pinned files have substantive >= 2 and are structurally
    ineligible anyway -- but that is an accident of today's data, not a
    guarantee, so the exclusion is explicit. Removing it makes this fail.
    """
    config, conn = isolated_db
    pinned_md = tmp_path / "pinned.md"
    pinned_md.write_text(
        "# Pinned\n\n"
        "## Some Pinned Learning\n"
        "_source: pinned-entry.md (2 substantive uses / 5 injections, 30d window)_\n"
    )
    monkeypatch.setenv("COMPOUND_LEARNING_PINNED_MD", str(pinned_md))
    _seed_usefulness(conn, [("unrelated.md", 1, 0)])
    _seed_lifetime(conn, [("pinned-entry.md", 50, 0), ("burner.md", 50, 0)])
    _add_row(conn, "pinned", "/l/pinned-entry.md", _iso(40), 91)
    _add_row(conn, "burner", "/l/burner.md", _iso(40), 91)

    ids = {c["id"] for c in ps.find_candidates(conn, 14, None)}

    assert ids == {"burner"}


def test_dry_run_reports_projected_corpus_after(isolated_db, tmp_path, monkeypatch, capsys) -> None:
    """A dry run has to answer "how big would the corpus be", not repeat the
    current size. Today corpus_after only moves under --apply, which makes the
    dry run useless for deciding whether to run the sweep."""
    config, conn = isolated_db
    _seed_usefulness(conn, [("unrelated.md", 1, 0)])
    _add_row(conn, "old1", "/l/old1.md", _iso(40), 0)
    _add_row(conn, "old2", "/l/old2.md", _iso(40), 0)
    _add_row(conn, "recent", "/l/recent.md", _iso(2), 0)

    code, out, _err = _run_main(
        monkeypatch, capsys, _prune_config(config, tmp_path), ["--json"]
    )
    report = json.loads(out)

    assert code == 0
    assert report["status"] == "dry-run"
    assert report["corpus_before"] == 3
    assert report["candidates"] == 2
    assert report["corpus_after"] < report["corpus_before"]
    assert report["corpus_after"] == report["corpus_before"] - report["candidates"]
    assert conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == 3


def test_refuses_to_apply_without_usefulness_data(isolated_db, tmp_path, monkeypatch, capsys) -> None:
    """With no measured usefulness anywhere, the substantive guard protects
    nothing and a cited file is indistinguishable from one nobody read."""
    config, conn = isolated_db
    _add_row(conn, "old", "/l/old.md", _iso(40), 0)

    code, _out, _err = _run_main(
        monkeypatch, capsys, _prune_config(config, tmp_path), ["--apply", "--json"]
    )

    assert code == 2
    assert conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == 1


@pytest.mark.parametrize("floor", [0, 4])
def test_refuses_to_apply_below_minimum_opportunity_floor(
    isolated_db, tmp_path, monkeypatch, capsys, floor
) -> None:
    """The floor is the only thing between the opportunity clause and a
    guillotine: at 0 it degenerates to plain `substantive = 0`, which takes the
    corpus from 2,328 rows to 34. That would fire unattended on the weekly timer
    at 04:10 with --limit 300, three times before anyone noticed. 5 is a value
    already measured and rejected, so refusing below it forbids nothing useful.

    Usefulness data is seeded so the only refusal reachable here is the floor.
    """
    config, conn = isolated_db
    _seed_usefulness(conn, [("unrelated.md", 1, 0)])
    _add_row(conn, "old", "/l/old.md", _iso(40), 0)

    code, _out, err = _run_main(
        monkeypatch,
        capsys,
        _prune_config(config, tmp_path, pruneOpportunityFloor=floor),
        ["--apply", "--json"],
    )

    assert code == 2
    assert conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == 1
    assert str(floor) in err and "5" in err


@pytest.mark.parametrize("floor", [5, 10])
def test_apply_proceeds_at_or_above_the_minimum_opportunity_floor(
    isolated_db, tmp_path, monkeypatch, capsys, floor
) -> None:
    """The clamp must not become a blanket refusal: at a sane floor the sweep
    still runs."""
    config, conn = isolated_db
    _seed_usefulness(conn, [("unrelated.md", 1, 0)])
    _add_row(conn, "old", "/l/old.md", _iso(40), 0)

    code, out, _err = _run_main(
        monkeypatch,
        capsys,
        _prune_config(config, tmp_path, pruneOpportunityFloor=floor),
        ["--apply", "--json"],
    )
    report = json.loads(out)

    assert code == 0
    assert report["status"] == "applied"
    assert conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == 0


def test_dry_run_still_reports_at_a_refused_floor(
    isolated_db, tmp_path, monkeypatch, capsys
) -> None:
    """Exploring the tradeoff is exactly what a dry run is for, so the clamp
    gates --apply only."""
    config, conn = isolated_db
    _seed_usefulness(conn, [("unrelated.md", 1, 0)])
    _add_row(conn, "old", "/l/old.md", _iso(40), 0)

    code, out, _err = _run_main(
        monkeypatch,
        capsys,
        _prune_config(config, tmp_path, pruneOpportunityFloor=0),
        ["--json"],
    )
    report = json.loads(out)

    assert code == 0
    assert report["status"] == "dry-run"
    assert conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == 1
