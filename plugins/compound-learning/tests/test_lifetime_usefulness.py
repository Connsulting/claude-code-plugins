"""Lifetime usefulness accumulation in analyze-peeks.py --persist.

`peek_usefulness` is a 30-day rolling window that --persist FULL-REPLACES, and
that full replace is load-bearing: build-pinned.py needs an entry to decay out of
the pinned set once it stops earning citations. Nothing here may change it.

The retention signal needs a long window instead, and it cannot get one by
widening the rolling job -- the source transcripts only span ~31 days and every
--persist run destroys history that cannot be recovered. Hence a second,
append-only `learning_usefulness_lifetime` table that --persist merges into.

Successive 30-day windows overlap by ~23 days on a weekly timer, so a naive
`injections += window_injections` double-counts by roughly 4x. The merge is
therefore gated on a stored watermark: only events strictly newer than the
watermark are folded in, and the watermark then advances. The counter merge and
the watermark advance must commit TOGETHER -- if the counters land and the
watermark write does not, the next run re-folds the identical event set into a
table that is append-only by design and can never be rebuilt.

Every test drives the real script over synthetic transcripts against an isolated
SQLite file. Nothing here reads ~/.claude/projects or the live database.
"""

import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.db as db  # noqa: E402


def _load_analyze():
    spec = importlib.util.spec_from_file_location(
        "analyze_peeks", PLUGIN_ROOT / "scripts" / "analyze-peeks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ap = _load_analyze()

ALPHA = "alpha-cache-stampede.md"
BETA = "beta-token-rotation.md"


# --- synthetic transcripts --------------------------------------------------


def _peek_event(timestamp, file_name: str, session: str = "s1") -> Dict[str, Any]:
    """One UserPromptSubmit hook event carrying an auto-peek injection.

    Shape matches what extract_peek() parses out of a real transcript: the
    `[auto-peek] N learning(s) found for:` header, one `-> file : summary` line
    and one `[<32 hex>]` body block per injected learning.
    """
    stdout = (
        "[auto-peek] 1 learning(s) found for: caching\n"
        f"-> {file_name} : one line summary\n"
        f"[{'a' * 32}]\n"
        "# Heading\n"
        "body text\n"
    )
    event: Dict[str, Any] = {
        "type": "user",
        "sessionId": session,
        "uuid": f"{session}-{file_name}-{timestamp}",
        "attachment": {
            "type": "hook_success",
            "hookName": "UserPromptSubmit",
            "stdout": stdout,
        },
    }
    if timestamp is not None:
        event["timestamp"] = timestamp
    return event


def _reply_event(text: str = "acknowledged, proceeding") -> Dict[str, Any]:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _write_transcript(projects: Path, name: str, injections: List[Any]) -> Path:
    """injections: list of (timestamp, file_name) or (timestamp, file_name, reply)."""
    events: List[Dict[str, Any]] = []
    for item in injections:
        timestamp, file_name = item[0], item[1]
        reply = item[2] if len(item) > 2 else "acknowledged, proceeding"
        events.append(_peek_event(timestamp, file_name))
        events.append(_reply_event(reply))
    path = projects / name
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


# --- reading the two tables -------------------------------------------------


def _lifetime(conn) -> Dict[str, Dict[str, int]]:
    try:
        rows = conn.execute(
            "SELECT file_name, injections, substantive, ack_only "
            "FROM learning_usefulness_lifetime"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"learning_usefulness_lifetime is not readable: {exc}")
    return {
        r[0]: {"injections": r[1], "substantive": r[2], "ack_only": r[3]} for r in rows
    }


def _rolling(conn) -> Dict[str, int]:
    rows = conn.execute("SELECT file_name, injections FROM peek_usefulness").fetchall()
    return {r[0]: r[1] for r in rows}


def _injections(conn, file_name: str) -> int:
    return _lifetime(conn).get(file_name, {}).get("injections", 0)


@pytest.fixture()
def peek_env(isolated_db, tmp_path, monkeypatch):
    """Point analyze-peeks.py at a synthetic transcript tree and an isolated DB."""
    config, conn = isolated_db
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(ap, "PROJECTS", projects)
    monkeypatch.setattr(db, "load_config", lambda: config)
    return config, conn, projects


def _persist(days_back: int = 30) -> None:
    ap.analyze(days_back, False, True)


# --- accumulation -----------------------------------------------------------


def test_lifetime_accumulates_across_runs(peek_env) -> None:
    """Disjoint windows sum. The rolling table cannot express this: it is
    replaced wholesale on every run, so window 1's evidence is gone."""
    _config, conn, projects = peek_env
    first = _write_transcript(
        projects,
        "run1.jsonl",
        [("2026-07-01T00:00:01+00:00", ALPHA), ("2026-07-01T00:00:02+00:00", ALPHA)],
    )
    _persist()
    first.unlink()
    _write_transcript(
        projects,
        "run2.jsonl",
        [
            ("2026-07-20T00:00:01+00:00", BETA),
            ("2026-07-20T00:00:02+00:00", BETA),
            ("2026-07-20T00:00:03+00:00", BETA),
        ],
    )

    _persist()

    assert _injections(conn, ALPHA) == 2
    assert _injections(conn, BETA) == 3


def test_overlapping_windows_do_not_double_count(peek_env) -> None:
    """THE WATERMARK'S REASON FOR EXISTING.

    Consecutive weekly runs of a 30-day window share ~23 days of events. Folding
    the whole window in every time inflates `injections` by ~4x, and `injections`
    is the denominator the prune opportunity floor reads -- an inflated count
    eventually archives learnings that were never actually over-injected.
    """
    _config, conn, projects = peek_env
    _write_transcript(
        projects,
        "overlap.jsonl",
        [
            ("2026-07-05T00:00:01+00:00", ALPHA),
            ("2026-07-05T00:00:02+00:00", ALPHA),
            ("2026-07-05T00:00:03+00:00", ALPHA),
        ],
    )

    _persist()
    after_first = _lifetime(conn)
    _persist()
    after_second = _lifetime(conn)

    assert after_first[ALPHA]["injections"] == 3
    assert after_second == after_first


def test_peek_usefulness_still_full_replaces(peek_env) -> None:
    """Decay in the rolling table is the one rule that must not regress: it is
    what lets an entry lose its pinned slot. Guarded here as the boundary of the
    lifetime change, alongside
    test_pinned_selection.py::test_entry_can_lose_its_slot_when_usefulness_disappears.
    """
    _config, conn, projects = peek_env
    first = _write_transcript(projects, "old.jsonl", [("2026-07-01T00:00:01+00:00", ALPHA)])
    _persist()
    assert _rolling(conn) == {ALPHA: 1}
    first.unlink()
    _write_transcript(projects, "new.jsonl", [("2026-07-20T00:00:01+00:00", BETA)])

    _persist()

    assert _rolling(conn) == {BETA: 1}
    assert set(_lifetime(conn)) == {ALPHA, BETA}


def test_empty_window_clears_rolling_but_not_lifetime(peek_env) -> None:
    """An empty rolling window means "nothing recent", never "the accumulated
    history is void". The empty-window branch deliberately clears
    peek_usefulness; it must not touch the accumulator."""
    _config, conn, projects = peek_env
    transcript = _write_transcript(
        projects,
        "only.jsonl",
        [("2026-07-01T00:00:01+00:00", ALPHA), ("2026-07-01T00:00:02+00:00", ALPHA)],
    )
    _persist()
    before = _lifetime(conn)
    transcript.unlink()

    _persist()

    assert _rolling(conn) == {}
    assert _lifetime(conn) == before
    assert before[ALPHA]["injections"] == 2


def test_events_without_timestamps_are_skipped_not_double_counted(peek_env) -> None:
    """An event with no timestamp cannot be watermarked, so it is skipped from
    the accumulator. Skipping undercounts slightly; counting it would double-count
    forever, on every subsequent run. The rolling tally still sees both."""
    _config, conn, projects = peek_env
    _write_transcript(
        projects,
        "mixed.jsonl",
        [("2026-07-05T00:00:01+00:00", ALPHA), (None, ALPHA)],
    )

    _persist()

    assert _rolling(conn) == {ALPHA: 2}
    assert _injections(conn, ALPHA) == 1

    _persist()

    assert _injections(conn, ALPHA) == 1


# --- transactional atomicity ------------------------------------------------


_WRITE_STATEMENT = re.compile(r"^\s*(insert|update|replace|delete)\b", re.I)


def _flatten(value) -> List[Any]:
    out: List[Any] = []

    def walk(item) -> None:
        if isinstance(item, (list, tuple)):
            for sub in item:
                walk(sub)
        elif isinstance(item, dict):
            for sub in item.values():
                walk(sub)
        else:
            out.append(item)

    walk(value)
    return out


class _WatermarkBlocker:
    """A real connection that refuses to WRITE the watermark.

    Reads are left alone on purpose: blocking the watermark read would abort the
    run before any counter was touched, and the test would pass trivially without
    ever exercising the commit boundary.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _guard(self, sql, params=()) -> None:
        text = str(sql)
        if not _WRITE_STATEMENT.match(text):
            return
        blob = " ".join([text] + [str(p) for p in _flatten(params)]).lower()
        if "watermark" in blob or "lifetime_meta" in blob:
            raise sqlite3.OperationalError("injected failure: watermark write")

    def execute(self, sql, *args):
        self._guard(sql, args)
        return self._conn.execute(sql, *args)

    def executemany(self, sql, seq_of_params):
        rows = list(seq_of_params)
        self._guard(sql, rows)
        return self._conn.executemany(sql, rows)

    def executescript(self, script):
        self._guard(script)
        return self._conn.executescript(script)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_failure_between_merge_and_watermark_rolls_back_both(peek_env, monkeypatch) -> None:
    """THE SINGLE-TRANSACTION PROOF.

    If the counter merge commits and the watermark advance then fails -- crash,
    lock timeout, power loss -- the next run re-folds the identical event set and
    double-counts PERMANENTLY, into a table that can never be rebuilt because the
    source transcripts age out at 31 days. There is no repair path.

    So: a run that cannot write the watermark must leave the counters exactly
    where they were, and a later clean run must fold the window in exactly once.
    Splitting the two writes across separate commits makes this test fail at the
    first assertion (2 -> 5 too early) or at the last (5 -> 8).
    """
    config, conn, projects = peek_env
    _write_transcript(
        projects,
        "first.jsonl",
        [("2026-07-05T00:00:01+00:00", ALPHA), ("2026-07-05T00:00:02+00:00", ALPHA)],
    )
    _persist()
    assert _injections(conn, ALPHA) == 2, "setup: first window must fold in normally"

    _write_transcript(
        projects,
        "second.jsonl",
        [
            ("2026-07-12T00:00:01+00:00", ALPHA),
            ("2026-07-12T00:00:02+00:00", ALPHA),
            ("2026-07-12T00:00:03+00:00", ALPHA),
        ],
    )

    blocked = {"on": True}
    real_get_connection = db.get_connection

    def maybe_blocking(cfg):
        opened = real_get_connection(cfg)
        return _WatermarkBlocker(opened) if blocked["on"] else opened

    monkeypatch.setattr(db, "get_connection", maybe_blocking)

    try:
        _persist()
    except Exception:
        pass  # the run may surface the failure or swallow it; the end state is what matters

    assert _injections(conn, ALPHA) == 2, (
        "counters advanced without the watermark: the next run will re-fold the "
        "same events and double-count permanently"
    )

    blocked["on"] = False
    _persist()

    assert _injections(conn, ALPHA) == 5, (
        "the rolled-back window must fold in exactly once on the clean re-run"
    )
