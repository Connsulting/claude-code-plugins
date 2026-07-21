"""Tests for usefulness-based pinned.md selection (scripts/build-pinned.py).

These guard the selection RULE, not the formatting: pinning must follow measured
substantive use, must be capped, and must be able to eject an entry.
"""

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.db as db  # noqa: E402


def _load_build_pinned():
    spec = importlib.util.spec_from_file_location(
        "build_pinned", PLUGIN_ROOT / "scripts" / "build-pinned.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bp = _load_build_pinned()

# build-pinned closes the connection it opens, so hand it a fresh one each call.
# Captured before monkeypatching, since bp.db IS lib.db (patching it would recurse).
_REAL_GET_CONNECTION = db.get_connection


def _add_learning(conn, doc_id: str, file_name: str, title: str, body: str = "Body text.") -> None:
    conn.execute(
        """INSERT INTO learnings (id, content, scope, repo, file_path, topic, keywords,
                                  created_at, access_count, last_accessed)
           VALUES (?, ?, 'global', '', ?, 'other', '', '2026-01-01', ?, '2026-01-01')""",
        (doc_id, f"# {title}\n\n{body}", f"/tmp/learnings/{file_name}", 9999),
    )
    conn.commit()


def _add_usefulness(conn, file_name: str, injections: int, substantive: int) -> None:
    conn.execute(
        """INSERT INTO peek_usefulness
           (file_name, injections, substantive, ack_only, last_substantive, window_days, computed_at)
           VALUES (?, ?, ?, 0, '2026-07-20T00:00:00Z', 30, '2026-07-21T00:00:00Z')""",
        (file_name, injections, substantive),
    )
    conn.commit()


def _args(output: Path, **overrides) -> argparse.Namespace:
    base: Dict[str, Any] = {
        "max_entries": None,
        "token_budget": None,
        "min_substantive": None,
        "dry_run": False,
        "output": output,
        "changelog": output.parent / "pinned-changes.log",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_high_access_count_with_no_substantive_use_is_not_pinned(isolated_db, monkeypatch, tmp_path):
    """The core inversion: retrieval frequency alone must never earn a pin."""
    config, conn = isolated_db
    _add_learning(conn, "loud", "loud-but-unused.md", "Loud But Unused")
    _add_learning(conn, "useful", "quiet-but-used.md", "Quiet But Used")
    # `loud` is retrieved 20x more often but was never actually engaged with.
    _add_usefulness(conn, "loud-but-unused.md", injections=200, substantive=0)
    _add_usefulness(conn, "quiet-but-used.md", injections=10, substantive=3)

    monkeypatch.setattr(bp.db, "load_config", lambda: config)
    monkeypatch.setattr(bp.db, "get_connection", lambda cfg: _REAL_GET_CONNECTION(config))
    out = tmp_path / "pinned.md"
    assert bp.build_pinned(_args(out)) == 1

    text = out.read_text()
    assert "quiet-but-used.md" in text
    assert "loud-but-unused.md" not in text


def test_entry_can_lose_its_slot_when_usefulness_disappears(isolated_db, monkeypatch, tmp_path):
    """Rank must not freeze: dropping out of peek_usefulness ejects the entry."""
    config, conn = isolated_db
    _add_learning(conn, "a", "was-useful.md", "Was Useful")
    _add_usefulness(conn, "was-useful.md", injections=10, substantive=4)
    monkeypatch.setattr(bp.db, "load_config", lambda: config)
    monkeypatch.setattr(bp.db, "get_connection", lambda cfg: _REAL_GET_CONNECTION(config))

    out = tmp_path / "pinned.md"
    bp.build_pinned(_args(out))
    assert "was-useful.md" in out.read_text()

    # analyze-peeks --persist full-replaces the table; the entry stops earning use.
    conn.execute("DELETE FROM peek_usefulness")
    _add_learning(conn, "b", "now-useful.md", "Now Useful")
    _add_usefulness(conn, "now-useful.md", injections=4, substantive=2)
    conn.commit()

    bp.build_pinned(_args(out))
    text = out.read_text()
    assert "was-useful.md" not in text
    assert "now-useful.md" in text


def test_denylisted_file_is_never_pinned_regardless_of_score(isolated_db, monkeypatch, tmp_path):
    config, conn = isolated_db
    config = dict(config)
    config["pinned"] = {"maxEntries": 6, "tokenBudget": 1400, "maxEntryTokens": 320,
                        "minSubstantive": 2, "denylist": ["stale-advice.md"]}
    _add_learning(conn, "stale", "stale-advice.md", "Stale Advice")
    _add_usefulness(conn, "stale-advice.md", injections=50, substantive=40)
    monkeypatch.setattr(bp.db, "load_config", lambda: config)
    monkeypatch.setattr(bp.db, "get_connection", lambda cfg: _REAL_GET_CONNECTION(config))

    out = tmp_path / "pinned.md"
    assert bp.build_pinned(_args(out)) == 0
    assert not out.exists()


def test_token_budget_caps_output(isolated_db, monkeypatch, tmp_path):
    config, conn = isolated_db
    big = "\n\n".join(["Paragraph of filler prose about the system." * 8 for _ in range(6)])
    for i in range(6):
        _add_learning(conn, f"id{i}", f"file{i}.md", f"Title {i}", big)
        _add_usefulness(conn, f"file{i}.md", injections=10, substantive=6 - i + 2)
    monkeypatch.setattr(bp.db, "load_config", lambda: config)
    monkeypatch.setattr(bp.db, "get_connection", lambda cfg: _REAL_GET_CONNECTION(config))

    out = tmp_path / "pinned.md"
    n = bp.build_pinned(_args(out, token_budget=600))
    assert 0 < n < 6
    assert bp.est_tokens(out.read_text()) <= 600 + 320  # budget + one entry's slack


def test_dry_run_leaves_pinned_untouched(isolated_db, monkeypatch, tmp_path):
    config, conn = isolated_db
    _add_learning(conn, "a", "used.md", "Used")
    _add_usefulness(conn, "used.md", injections=5, substantive=3)
    monkeypatch.setattr(bp.db, "load_config", lambda: config)
    monkeypatch.setattr(bp.db, "get_connection", lambda cfg: _REAL_GET_CONNECTION(config))

    out = tmp_path / "pinned.md"
    out.write_text("ORIGINAL")
    bp.build_pinned(_args(out, dry_run=True))
    assert out.read_text() == "ORIGINAL"
    assert "used.md" in (tmp_path / "pinned.md.proposed").read_text()


def test_empty_usefulness_table_does_not_clobber_pinned(isolated_db, monkeypatch, tmp_path):
    """A missing --persist run must not silently blank the file."""
    config, conn = isolated_db
    _add_learning(conn, "a", "used.md", "Used")
    monkeypatch.setattr(bp.db, "load_config", lambda: config)
    monkeypatch.setattr(bp.db, "get_connection", lambda cfg: _REAL_GET_CONNECTION(config))

    out = tmp_path / "pinned.md"
    out.write_text("ORIGINAL")
    assert bp.build_pinned(_args(out)) == 0
    assert out.read_text() == "ORIGINAL"


def test_clean_body_truncates_on_paragraph_boundary():
    body = "# T\n\n**Type:** gotcha\n\n## Problem\n\n" + ("word " * 400) + "\n\n## Extra\n\nTail."
    out = bp.clean_body(body, max_tokens=50, source="x.md")
    assert "truncated; full text in x.md" in out
    assert "**Type:**" not in out
    assert bp.est_tokens(out) < 200


def test_clean_body_relevels_headings_under_entry_header():
    body = "# T\n\n# Inner H1\n\ntext\n\n## Inner H2\n\ntext"
    out = bp.clean_body(body, max_tokens=10000, source="x.md")
    assert out.startswith("### Inner H1")
    assert "#### Inner H2" in out
    assert not any(line.startswith("# ") or line.startswith("## ")
                   for line in out.splitlines())


def test_peek_usefulness_table_exists_after_schema_init(isolated_db):
    _config, conn = isolated_db
    cols = {r[1] for r in conn.execute("PRAGMA table_info(peek_usefulness)").fetchall()}
    assert {"file_name", "injections", "substantive", "window_days"} <= cols


@pytest.mark.parametrize("substantive,expected", [(0, 0), (1, 0), (2, 1)])
def test_min_substantive_floor(isolated_db, monkeypatch, tmp_path, substantive, expected):
    config, conn = isolated_db
    _add_learning(conn, "a", "used.md", "Used")
    if substantive:
        _add_usefulness(conn, "used.md", injections=10, substantive=substantive)
    else:
        _add_usefulness(conn, "used.md", injections=10, substantive=0)
    monkeypatch.setattr(bp.db, "load_config", lambda: config)
    monkeypatch.setattr(bp.db, "get_connection", lambda cfg: _REAL_GET_CONNECTION(config))
    assert bp.build_pinned(_args(tmp_path / "pinned.md", min_substantive=2)) == expected
