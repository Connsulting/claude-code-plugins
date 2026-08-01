"""Schema-migration contract: the lifetime tables and the `repo_root` identity column.

Two things are guarded here, and both are live-DB safety properties rather than
feature tests:

  * `_create_schema` runs on EVERY `get_connection`, so every additive change it
    makes must be safe to re-run forever against a populated 126MB database.
  * `repo_root` is what makes a repo-scoped learning reachable. `upsert_document`
    does not write it, so the recurring backfill in `_create_schema` is the only
    write path. Any run-once guard on that backfill (sentinel row, schema-version
    check, module flag) still satisfies "no-op on the second run" while making
    every learning written after the migration permanently unreachable -- a
    silent, fail-closed retrieval outage. The tests below assert through SEARCH
    RESULTS rather than by reading the column, so a backfill that populates the
    column but never runs again cannot pass them.

`repo` is a bare directory name and is NOT an identity: three different
repositories across two clients are all named `platform`. The isolation test is
the security test for that leak.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.db as db  # noqa: E402

# Absolute roots that do not exist on disk and are not inside any git repo, so
# git_utils.resolve_repo_root returns them unchanged and the expected value is
# deterministic on any machine.
ALPHA_ROOT = "/synthetic/clients/alpha/platform"
BETA_ROOT = "/synthetic/clients/beta/platform"

ALPHA_BODY = (
    "Redis cache stampede on deploy: every pod invalidates the same key at once "
    "and the origin database sees a thundering herd. Use a jittered TTL."
)
BETA_BODY = (
    "Redis cache stampede on rollout: all workers expire the same key together "
    "and the primary database takes a thundering herd. Add TTL jitter."
)
GLOBAL_BODY = (
    "Redis cache stampede in general: synchronized expiry produces a thundering "
    "herd against the backing store. Jitter every TTL."
)
QUERY = "redis cache stampede thundering herd ttl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exec(conn, sql: str, params: Sequence[Any] = ()):
    """Run *sql*, turning a missing table/column into a readable failure."""
    try:
        return conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001 - any DB error is a missing-feature failure
        pytest.fail(f"statement failed against the migrated schema: {sql.strip()[:80]!r}: {exc}")


def _search_scoped(conn, query: str, roots: List[str]) -> List[Dict[str, Any]]:
    """db.search filtered by absolute repo roots.

    Keyword-only on purpose: the migration renames `scope_repos` to
    `scope_repo_roots` with no compatibility path, so an un-migrated signature
    surfaces here rather than silently filtering on the wrong column.
    """
    try:
        return db.search(conn, query, scope_repo_roots=roots, n_results=10)
    except TypeError as exc:
        pytest.fail(f"db.search does not take scope_repo_roots (no compat path allowed): {exc}")


def _scoped_ids(conn, roots: List[str]) -> set:
    return {r["id"] for r in _search_scoped(conn, QUERY, roots)}


def _repo_row(conn, doc_id: str, root: str, body: str, repo: str = "platform") -> None:
    """Write a repo-scoped learning through the real write path.

    `repo_root` is deliberately absent from the metadata: upsert_document does
    not accept it, which is exactly why the backfill has to recur.
    """
    db.upsert_document(
        conn,
        doc_id,
        body,
        {
            "scope": "repo",
            "repo": repo,
            "file_path": f"{root}/.projects/learnings/{doc_id}.md",
            "created_at": _now(),
        },
    )


def _global_row(conn, doc_id: str, body: str) -> None:
    db.upsert_document(
        conn,
        doc_id,
        body,
        {
            "scope": "global",
            "repo": "",
            "file_path": f"/synthetic/home/.projects/learnings/{doc_id}.md",
            "created_at": _now(),
        },
    )


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- lifetime tables --------------------------------------------------------


def test_lifetime_tables_created_on_fresh_db(isolated_db) -> None:
    """Both accumulator tables must be usable straight out of get_connection."""
    _config, conn = isolated_db
    now = _now()

    _exec(
        conn,
        "INSERT INTO learning_usefulness_lifetime "
        "(file_name, injections, substantive, ack_only, first_seen, last_updated) "
        "VALUES (?,?,?,?,?,?)",
        ("x.md", 3, 1, 2, now, now),
    )
    _exec(
        conn,
        "INSERT INTO learning_usefulness_lifetime_meta (key, value) VALUES (?,?)",
        ("watermark", now),
    )
    conn.commit()

    row = _exec(
        conn,
        "SELECT injections, substantive, ack_only FROM learning_usefulness_lifetime "
        "WHERE file_name = ?",
        ("x.md",),
    ).fetchone()
    meta = _exec(
        conn,
        "SELECT value FROM learning_usefulness_lifetime_meta WHERE key = ?",
        ("watermark",),
    ).fetchone()

    assert tuple(row) == (3, 1, 2)
    assert meta[0] == now


def test_migration_is_idempotent(isolated_db, tmp_path) -> None:
    """THE LIVE-DB SAFETY CONTRACT.

    _create_schema runs on every single get_connection against a populated
    126MB database. Re-running it must raise nothing and must not add, drop, or
    lose a row anywhere -- including in the append-only lifetime table, which
    can never be rebuilt because the source transcripts age out at 31 days.
    """
    config, conn = isolated_db
    _repo_row(conn, "alpha", ALPHA_ROOT, ALPHA_BODY)
    _global_row(conn, "glob", GLOBAL_BODY)
    now = _now()
    _exec(
        conn,
        "INSERT INTO learning_usefulness_lifetime "
        "(file_name, injections, substantive, ack_only, first_seen, last_updated) "
        "VALUES (?,?,?,?,?,?)",
        ("alpha.md", 57, 2, 6, now, now),
    )
    conn.commit()

    def snapshot(c):
        return {
            "learnings": _count(c, "learnings"),
            "fts": _count(c, "fts_learnings"),
            "lifetime": _count(c, "learning_usefulness_lifetime"),
            "lifetime_rows": [
                tuple(r)
                for r in c.execute(
                    "SELECT file_name, injections, substantive, ack_only "
                    "FROM learning_usefulness_lifetime ORDER BY file_name"
                ).fetchall()
            ],
        }

    before = snapshot(conn)

    for _ in range(3):
        again = db.get_connection(config)  # re-runs _create_schema, including the backfill
        again.close()

    after = snapshot(db.get_connection(config))

    assert after == before


# --- repo_root backfill -----------------------------------------------------


def test_repo_root_backfilled_from_file_path(isolated_db) -> None:
    """A pre-existing corpus row gets its root derived from file_path, once."""
    config, conn = isolated_db
    conn.execute(
        "INSERT INTO learnings (id, content, scope, repo, file_path, topic, keywords, created_at) "
        "VALUES (?,?,'repo','platform',?,'other','',?)",
        ("legacy", ALPHA_BODY, f"{ALPHA_ROOT}/.projects/learnings/legacy.md", _now()),
    )
    conn.commit()

    reopened = db.get_connection(config)
    first = _exec(
        reopened, "SELECT repo_root FROM learnings WHERE id = ?", ("legacy",)
    ).fetchone()[0]

    reopened_again = db.get_connection(config)
    second = _exec(
        reopened_again, "SELECT repo_root FROM learnings WHERE id = ?", ("legacy",)
    ).fetchone()[0]

    assert first == ALPHA_ROOT
    assert second == ALPHA_ROOT


def test_backfill_never_deletes_rows(isolated_db) -> None:
    """The backfill is an UPDATE. A row whose path has no learnings segment is
    stranded with an empty root, never removed."""
    config, conn = isolated_db
    conn.executemany(
        "INSERT INTO learnings (id, content, scope, repo, file_path, topic, keywords, created_at) "
        "VALUES (?,?,?,?,?,'other','',?)",
        [
            ("r1", ALPHA_BODY, "repo", "platform", f"{ALPHA_ROOT}/.projects/learnings/r1.md", _now()),
            ("r2", BETA_BODY, "repo", "platform", f"{BETA_ROOT}/.projects/learnings/r2.md", _now()),
            ("r3", GLOBAL_BODY, "repo", "odd", "/synthetic/nowhere/r3.md", _now()),
            ("g1", GLOBAL_BODY, "global", "", "/synthetic/home/.projects/learnings/g1.md", _now()),
        ],
    )
    conn.commit()
    before = _count(conn, "learnings")

    for _ in range(3):
        db.get_connection(config).close()

    assert _count(db.get_connection(config), "learnings") == before == 4


def test_new_row_acquires_repo_root_on_next_connection(isolated_db) -> None:
    """THE POST-MIGRATION WRITE PATH.

    upsert_document never sets repo_root, so a learning written after the
    migration lands with an empty root and is invisible to scoped search until
    the recurring backfill heals it on the next connection open. Asserting
    through search (not through the column) is deliberate: a backfill wrapped in
    any run-once guard populates nothing here and this test goes red.
    """
    config, conn = isolated_db
    _repo_row(conn, "fresh", ALPHA_ROOT, ALPHA_BODY)
    conn.commit()

    reopened = db.get_connection(config)

    assert "fresh" in _scoped_ids(reopened, [ALPHA_ROOT])


def test_bulk_reindex_rows_recover_repo_root(isolated_db) -> None:
    """index-learnings.py is delete-then-insert through upsert_document, so a
    bulk reindex resets every repo-scoped row's root to ''. The next connection
    open must heal them, or one routine reindex silently unpublishes the whole
    repo-scoped corpus."""
    config, conn = isolated_db
    _repo_row(conn, "reindexed", ALPHA_ROOT, ALPHA_BODY)
    healed = db.get_connection(config)
    assert "reindexed" in _scoped_ids(healed, [ALPHA_ROOT])

    _repo_row(healed, "reindexed", ALPHA_ROOT, ALPHA_BODY)  # the reindex
    healed.commit()

    after_reindex = db.get_connection(config)

    assert "reindexed" in _scoped_ids(after_reindex, [ALPHA_ROOT])


# --- scope isolation --------------------------------------------------------


def test_search_isolates_same_named_repos_in_different_roots(isolated_db) -> None:
    """THE CROSS-CLIENT LEAK.

    Two different repositories, both named `platform`, owned by two different
    clients. Filtering on the bare `repo` name makes them one scope: measured,
    266 of 276 `repo='platform'` rows come from three distinct repositories
    across two clients. A search scoped to one root must return only that root.
    """
    config, conn = isolated_db
    _repo_row(conn, "alpha-doc", ALPHA_ROOT, ALPHA_BODY, repo="platform")
    _repo_row(conn, "beta-doc", BETA_ROOT, BETA_BODY, repo="platform")
    conn.commit()

    reopened = db.get_connection(config)
    from_alpha = _scoped_ids(reopened, [ALPHA_ROOT])
    from_beta = _scoped_ids(reopened, [BETA_ROOT])

    assert from_alpha == {"alpha-doc"}
    assert from_beta == {"beta-doc"}


def test_search_still_returns_global_scope(isolated_db) -> None:
    """Global learnings are scope-free and must survive the identity change."""
    config, conn = isolated_db
    _global_row(conn, "global-doc", GLOBAL_BODY)
    _repo_row(conn, "alpha-doc", ALPHA_ROOT, ALPHA_BODY)
    conn.commit()

    reopened = db.get_connection(config)

    assert _scoped_ids(reopened, []) == {"global-doc"}
    assert _scoped_ids(reopened, [ALPHA_ROOT]) == {"global-doc", "alpha-doc"}
