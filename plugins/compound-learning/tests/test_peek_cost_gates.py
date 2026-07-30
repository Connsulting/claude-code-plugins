"""Tests for the outcome-driven peek gates: zero-value suppression and size capping.

These guard the RULES, not the tuning constants:
  * suppression must be driven by measured substantive use, never by retrieval
    confidence, and must fail open when there is no usefulness data yet;
  * truncation must never change WHICH learnings are selected, must preserve the
    head of the document, and must point at the full file.

Both gates are peek-only. An explicit search must still return full content --
that separation is the whole reason suppression is tolerable despite per-file
verdicts being statistically weak (see lib/db.py peekZeroValueMinInjections).
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.db as db  # noqa: E402


def _load_search():
    spec = importlib.util.spec_from_file_location(
        "search_learnings", PLUGIN_ROOT / "scripts" / "search-learnings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sl = _load_search()


def _seed_usefulness(conn: sqlite3.Connection, rows) -> None:
    """rows: iterable of (file_name, injections, substantive)."""
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


# --- suppression ------------------------------------------------------------


def test_suppresses_only_zero_substantive_over_threshold(isolated_db) -> None:
    config, conn = isolated_db
    _seed_usefulness(conn, [
        ("dead.md", 40, 0),        # over threshold, never cited -> suppress
        ("borderline.md", 30, 0),  # exactly at threshold -> suppress
        ("young.md", 29, 0),       # under threshold -> keep
        ("earned.md", 90, 1),      # heavily injected but cited -> keep
    ])

    suppressed = sl.load_zero_value_sources(config, 30)

    assert suppressed == {"dead.md", "borderline.md"}


def test_a_single_citation_rescues_a_file(isolated_db) -> None:
    """One substantive use must be enough to keep a file eligible.

    Guards the asymmetry deliberately: injections are cheap to accumulate and
    citations are rare (0.886% base rate), so the bar to survive has to be low.
    """
    config, conn = isolated_db
    _seed_usefulness(conn, [("noisy.md", 500, 1)])

    assert sl.load_zero_value_sources(config, 30) == set()


def test_suppression_disabled_at_zero(isolated_db) -> None:
    config, conn = isolated_db
    _seed_usefulness(conn, [("dead.md", 999, 0)])

    assert sl.load_zero_value_sources(config, 0) == set()


def test_fails_open_without_usefulness_table(isolated_db) -> None:
    """A fresh install has no usefulness data and must still retrieve normally."""
    config, _conn = isolated_db

    assert sl.load_zero_value_sources(config, 30) == set()


# --- truncation -------------------------------------------------------------


def test_short_document_untouched() -> None:
    doc = "# Small\n\n**Type:** gotcha\n\nbody text"

    assert sl.truncate_for_peek(doc, 4000, "/x/small.md") == doc


def test_truncation_disabled_at_zero() -> None:
    doc = "x" * 50_000

    assert sl.truncate_for_peek(doc, 0, "/x/big.md") == doc


def test_truncation_preserves_head_and_points_at_file() -> None:
    head = "# Title\n\n**Type:** pattern\n\n## Problem\nthe actual claim lives here\n"
    doc = head + "\n## Solution\n" + ("y" * 20_000)

    out = sl.truncate_for_peek(doc, 4000, "/x/big.md")

    assert out.startswith(head)
    assert len(out) < len(doc)
    assert "/x/big.md" in out
    assert "truncated for injection" in out


def test_truncation_cuts_at_section_boundary() -> None:
    doc = "# T\n\n## Keep\n" + ("a" * 3000) + "\n## Drop\n" + ("b" * 5000)

    out = sl.truncate_for_peek(doc, 4000, "/x/f.md")

    assert "## Keep" in out
    assert "## Drop" not in out
    assert "b" * 100 not in out


def test_truncation_falls_back_when_boundary_is_too_early() -> None:
    """A boundary in the first half must not shrink the budget to nothing.

    Without the guard, a document whose only `##` sits at char 20 would be cut
    to 20 chars, discarding almost the entire allowance.
    """
    doc = "# T\n\n## Only\n" + ("a" * 10_000)

    out = sl.truncate_for_peek(doc, 4000, "/x/f.md")

    # Kept content should be close to the budget, not truncated back to char ~12.
    assert len(out) > 3000


def test_truncated_output_stays_within_budget_plus_notice() -> None:
    doc = "# T\n\n" + ("a" * 40_000)
    cap = 4000

    out = sl.truncate_for_peek(doc, cap, "/x/f.md")

    notice_slack = 200
    assert len(out) <= cap + notice_slack


def test_reports_accurate_omitted_count() -> None:
    doc = "# T\n\n" + ("a" * 10_000)

    out = sl.truncate_for_peek(doc, 4000, "/x/f.md")

    kept = out.split("\n\n_[truncated")[0]
    omitted = len(doc) - len(kept)
    assert f"{omitted} of {len(doc)} chars omitted" in out


# --- config contract --------------------------------------------------------


def test_defaults_expose_both_gates() -> None:
    """The gates must be configurable, and their defaults must be the calibrated ones."""
    learnings: Dict[str, Any] = db.load_config()["learnings"]

    assert learnings.get("peekZeroValueMinInjections") == 30
    assert learnings.get("peekMaxInjectedChars") == 4000
