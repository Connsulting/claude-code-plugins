#!/usr/bin/env python3
"""
Shared SQLite + sqlite-vec database module for compound-learning plugin.
All four Python scripts import from here instead of duplicating database boilerplate.
"""

import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Set

try:
    import sqlite3
    _test_conn = sqlite3.connect(':memory:')
    if not hasattr(_test_conn, 'enable_load_extension'):
        raise AttributeError('no enable_load_extension')
    _test_conn.close()
except AttributeError:
    try:
        import pysqlite3 as sqlite3  # type: ignore[no-redef]
    except ImportError:
        import platform
        is_arm = platform.machine().startswith('arm') or platform.machine() == 'aarch64'
        pkg = 'pysqlite3' if is_arm else 'pysqlite3-binary'
        print(
            '[ERROR] sqlite3 extension loading is not available.\n'
            'Install the required dependency:\n'
            f'  pip install {pkg}'
        )
        raise

_model = None
_model_lock = threading.Lock()


def load_config() -> Dict[str, Any]:
    """Load configuration from environment variables, then config file, then defaults."""
    home = os.path.expanduser('~')

    defaults: Dict[str, Any] = {
        'sqlite': {
            'dbPath': os.path.join(home, '.claude', 'compound-learning.db'),
        },
        'learnings': {
            'globalDir': os.path.join(home, '.projects/learnings'),
            'repoSearchPath': home,
            'archiveDir': os.path.join(home, '.projects/archive/learnings'),
            'highConfidenceThreshold': 0.40,
            'possiblyRelevantThreshold': 0.55,
            'keywordBoostWeight': 0.65,
            # Peek-mode ceiling on the RAW embedding distance (pre-rerank). The
            # keyword/FTS boost re-orders candidates but must not promote a weak
            # semantic match into an auto-peek injection. Measured over 7 days:
            # 79% of injections were raw-distance 0.50-0.75 matches the boost had
            # pushed under highConfidenceThreshold. Gating raw distance < 0.50
            # keeps genuine matches (~21% of prior volume) and drops the rest.
            #
            # THIS IS A DISTANCE: lower is stricter. Tightened 0.50 -> 0.45
            # because the 0.50 gate still yields 0.80% substantive use (44 uses
            # in 5,487 injections over 30d). Re-measured 2026-08-01 by replaying
            # every `RESULT:` line carrying an orig_distance out of
            # ~/.claude/plugins/compound-learning/activity.log (n=6,632 over the
            # last 30d), counting what each ceiling would admit:
            #
            #   ceiling   injections   vs today   distinct files
            #    0.50        2,493        100%         511
            #    0.45          550         22%          89
            #    0.40           17          0.7%         6
            #    0.35            0          0%           0
            #
            # 0.40 and below effectively disable peek, so they are not options.
            # 0.45 is the only meaningful tightening available, and it is a
            # large one -- roughly a 78% further cut on top of what 0.50 already
            # removed. Raise back toward 0.50 if genuine matches start going
            # missing; the numbers above are the table to re-run first.
            'peekHighConfidenceThreshold': 0.45,
            # Measured-use suppression for peek: a file injected at least this
            # many times inside the peek_usefulness window with ZERO substantive
            # uses stops being offered. Same principle build-pinned.py applies to
            # pinned.md -- rank on measured use, never on retrieval confidence.
            #
            # 30 is a statistical floor, not a preference. Substantive use is rare
            # (base rate 0.886% over the 30d window), so "zero citations" is weak
            # evidence at low injection counts -- for a perfectly average file,
            # P(0 citations | 10 injections) = 91%. Suppressing at 10 would drop
            # 100 files chosen essentially at random. Citations-per-file by
            # injection bucket, measured 2026-07-29, against what the base rate
            # predicts:
            #
            #   bucket   files   inj   cited files   expected cited
            #    1-4      923   1665       15            14.7
            #    5-9      180   1177        9            10.2
            #   10-29      87   1381        9            11.4
            #   30-49      12    457        0             3.4
            #    50+       11    850        1             5.4
            #
            # Below 30 the observed and expected columns agree, so there is no
            # signal to act on. The deficit only appears at 30+, where 23 files
            # burned 1,307 injections and earned 1 citation against 8.8 expected.
            # Raise this if false positives show up; do not lower it below 30
            # without re-running that table.
            #
            # Self-healing, because a per-file verdict here is still uncertain:
            # peek_usefulness is a rolling window recomputed by
            # `analyze-peeks.py --persist`, so a suppressed file ages out once it
            # stops being injected and becomes eligible again. Suppression also
            # only affects peek -- `/compound search` still returns these files in
            # full. 0 disables suppression entirely.
            'peekZeroValueMinInjections': 30,
            # Ceiling on the chars of any single learning injected by peek.
            # Corpus p50 is 2,095 chars and p90 is 3,216, but the tail runs to
            # 43,946 -- and the tail is where the merge-bloat lives (186 files
            # carrying `## Source:` headers average 5,182 chars vs 2,108 for the
            # rest). Truncation cuts at a section boundary and points at the full
            # file, so nothing is lost, it just is not resident.
            #
            # Unlike the suppression gate above, this needs no per-file verdict --
            # it never decides WHETHER a learning is useful, only how much of it
            # is resident, so it carries no false-positive risk. Measured over the
            # 30d window: 4,000 cuts injected volume 16.42M -> 13.27M chars
            # (19.2%) across 112 files, and only 4.9% of what it cuts comes from
            # files that earned a citation. 3,000 cuts 26.6% but starts biting p90.
            # 0 disables truncation.
            'peekMaxInjectedChars': 4000,
            # Hard cap on learnings auto-peek may inject across one session.
            # Measured over the 30d window: 5,487 injections produced 44
            # substantive uses (0.80%) and 2,228 (41%) drew only an
            # acknowledgment, so marginal injections late in a long session are
            # close to pure context cost. This is a stop, not a rate limit --
            # once hit, nothing else is injected for the session's life. The
            # per-session ledger is pruned at 24h (hooks/auto-peek.sh), so it
            # self-heals daily. Log the suppression before tuning this.
            'peekMaxPerSession': 12,
            # Age a never-retrieved learning must reach before prune-stale.py
            # will consider it. Was DEFAULT_GRACE_DAYS in prune-stale.py; the
            # module constant becomes the fallback only.
            'pruneGraceDays': 14,
            # Injections a learning must have accumulated with ZERO substantive
            # uses before prune-stale.py treats it as measurably not earning its
            # context cost. Measured on the live corpus: floor 10 selects 93
            # files; floor 5 selects 239, and the extra 146 are ~96% noise --
            # "zero citations" is weak evidence at low injection counts against
            # a 0.886% base rate. prune-stale.py --apply refuses below 5,
            # because at 0 this clause degenerates to plain `substantive = 0`
            # and takes the corpus from 2,328 rows to 34.
            'pruneOpportunityFloor': 10,
            # Companion age gate for the opportunity-floor clause: a file needs
            # time in the corpus to accumulate injections at all, so a young
            # file with few injections is not evidence of anything.
            'pruneOpportunityMinAgeDays': 30,
            # Write-time near-duplicate gate (scripts/dedupe-on-write.py).
            # NOTE THE DIRECTION: this is a cosine SIMILARITY (higher is more
            # similar), the opposite of every peek*Threshold above, which are
            # distances.
            #
            # THE STORED METRIC IS L2, NOT COSINE. vec_learnings is declared
            # with no distance metric, so sqlite-vec computes L2, and db.search
            # halves the raw value at the `dist = float(row['distance']) / 2.0`
            # line below. For the unit-normalized embeddings this plugin stores,
            # the cosine is recovered from that halved distance d as:
            #
            #     similarity = 1 - 2 * d**2
            #
            # It is NOT `1 - d`. scripts/dedupe-on-write.py cosine_from() is the
            # reference implementation. Worked values, showing how far the wrong
            # conversion drifts:
            #
            #   true cosine   raw L2   d (halved)   1-2d^2   1-d (WRONG)
            #      0.6000     0.8944     0.4472     0.6000     0.5528
            #      0.9309     0.3718     0.1859     0.9309     0.8141
            #      0.1925     1.2708     0.6354     0.1925     0.3646
            #      0.0000     1.4142     0.7071     0.0000     0.2929
            #
            # CALIBRATION IS ALREADY CORRECT. 0.88, and every peek threshold
            # above, were tuned empirically against the values db.search
            # actually returns, so shipped behavior is right. Correcting the
            # metric label above is a documentation fix only. Do NOT re-tune any
            # number here on the strength of the corrected formula.
            'writeDedupeCosineThreshold': 0.88,
            # Title-slug Jaccard floor for the second, independent clustering
            # signal in scripts/auto-consolidate.py. Also a SIMILARITY.
            #
            # Set by the TEAMMATE trio, which is the only cluster that actually
            # depends on this signal: its minimum pairwise Jaccard is 0.5000,
            # and its minimum pairwise cosine is 0.7313 -- below any union floor
            # we would accept, so without the Jaccard arm it draws no edge at
            # all. The npm trio does NOT need this arm: it already clusters at
            # cosine floor 0.85 as the identical 5-member cluster. An earlier
            # 0.33 value was justified by npm's 0.3333 minimum; that reasoning
            # is superseded, because npm never depended on Jaccard.
            #
            # Noise budget decided the operating point. Against a baseline of
            # 15 clusters: floor 0.85 + Jaccard 0.50 gives 43 clusters (~2.9x),
            # while floor 0.82 + Jaccard 0.33 gives 74 (~4.9x, an unreadable
            # review queue). Both trios land in one cluster each and classify
            # REVIEW either way, and auto_candidates stays 0. At 0.50 the
            # teammate cluster tightens to exactly the trio with no passengers
            # and the corpus's largest cluster drops from 11 members to 6.
            # So the cosine floor stays 0.85 and the Jaccard arm carries the
            # work.
            'mergeTitleJaccardThreshold': 0.50,
            # Cosine-similarity conjunct on that Jaccard arm, so files sharing
            # only generic title tokens cannot be unioned. Measured: the
            # teammate trio's weakest pair is 0.7313 and today draws no edge at
            # all (union floor 0.85), so 0.70 admits it while staying well
            # clear of the corpus noise floor.
            'mergeJaccardMinCosine': 0.70,
        },
        'pinned': {
            # Selection is by measured substantive use (peek_usefulness), never
            # by access_count. See scripts/build-pinned.py.
            'maxEntries': 6,
            'tokenBudget': 1400,
            'maxEntryTokens': 320,
            # 1 substantive use in a 30d window is inside the noise floor; require 2.
            'minSubstantive': 2,
            'windowDays': 30,
            # File names (basename) never eligible for pinning regardless of score.
            'denylist': [
                # Prescribes claude-opus-4-5-20251101 for PDF parsing; wrong model
                # generation for this environment.
                'opus-pdf-parsing-2026-01-27.md',
            ],
        },
        'consolidation': {
            'duplicateThreshold': 0.25,
            'scopeKeywords': ['security', 'authentication', 'jwt', 'oauth', 'encryption',
                              'password', 'token', 'api-key', 'secret', 'xss', 'sql-injection'],
            'outdatedKeywords': ['temporary', 'workaround', 'deprecated', 'todo', 'fixme',
                                 'hack', 'remove later', 'obsolete'],
        },
    }

    # Determine plugin root for config file location
    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT')
    if not plugin_root:
        # Auto-detect: this file lives at <plugin_root>/lib/db.py
        plugin_root = str(Path(__file__).parent.parent)

    config_file = Path(plugin_root) / '.claude-plugin' / 'config.json'
    file_config: Dict[str, Any] = {}

    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                file_config = json.load(f)
            # Expand ${HOME} in all string values recursively
            file_config = _expand_home(file_config, home)
        except Exception as e:
            print(f"Warning: Failed to load config from {config_file}: {e}", file=sys.stderr)

    # Env var overrides
    env_db_path = os.environ.get('SQLITE_DB_PATH')
    env_global_dir = os.environ.get('LEARNINGS_GLOBAL_DIR')
    env_repo_path = os.environ.get('LEARNINGS_REPO_SEARCH_PATH')

    result = _deep_merge(defaults, file_config)

    if env_db_path:
        result['sqlite']['dbPath'] = os.path.expanduser(env_db_path)
    if env_global_dir:
        result['learnings']['globalDir'] = os.path.expanduser(env_global_dir)
    if env_repo_path:
        result['learnings']['repoSearchPath'] = os.path.expanduser(env_repo_path)

    return result


def _expand_home(obj: Any, home: str) -> Any:
    if isinstance(obj, str):
        return obj.replace('${HOME}', home)
    if isinstance(obj, dict):
        return {k: _expand_home(v, home) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_home(v, home) for v in obj]
    return obj


def _deep_merge(base: Dict, override: Dict) -> Dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def get_connection(config: Dict[str, Any]) -> sqlite3.Connection:
    """Open SQLite DB, load sqlite-vec extension, create schema if missing."""
    try:
        import sqlite_vec
    except ImportError:
        print(
            '[ERROR] sqlite-vec is not installed.\n'
            'Install the required dependency:\n'
            '  pip install sqlite-vec'
        )
        raise

    db_path = config['sqlite']['dbPath']
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    _create_schema(conn)
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS learnings (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            scope TEXT NOT NULL,
            repo TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL,
            topic TEXT NOT NULL DEFAULT 'other',
            keywords TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS vec_learnings USING vec0(
            id TEXT PRIMARY KEY,
            embedding float[384]
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS fts_learnings USING fts5(
            id UNINDEXED,
            content,
            tokenize='porter unicode61'
        );

        -- Usefulness evidence, recomputed in full by analyze-peeks.py --persist.
        -- One row per learning file name observed in auto-peek injections during
        -- the scoring window. This is the ONLY selection signal build-pinned.py
        -- uses; access_count (incremented at injection time) is not evidence that
        -- anything was read or used.
        CREATE TABLE IF NOT EXISTS peek_usefulness (
            file_name TEXT PRIMARY KEY,
            injections INTEGER NOT NULL DEFAULT 0,
            substantive INTEGER NOT NULL DEFAULT 0,
            ack_only INTEGER NOT NULL DEFAULT 0,
            last_substantive TEXT,
            window_days INTEGER NOT NULL DEFAULT 30,
            computed_at TEXT NOT NULL
        );

        -- Lifetime usefulness accumulator. This table is APPEND/ACCUMULATE
        -- ONLY and is NEVER DELETEd from -- not by an empty window, not by a
        -- reindex, not by a re-run. analyze-peeks.py --persist folds in only
        -- the peek events newer than the `watermark` row in
        -- learning_usefulness_lifetime_meta, so totals grow monotonically and
        -- overlapping 30-day windows (a weekly job shares ~23 days with the
        -- previous run) never double-count.
        --
        -- The accumulation is ONE-WAY: source transcripts age out of
        -- ~/.claude/projects at ~31 days, so once an event is folded in it can
        -- never be recomputed and this table can never be rebuilt from
        -- scratch. Back the DB up before changing how it is written.
        --
        -- peek_usefulness above deliberately keeps FULL-REPLACE semantics:
        -- build-pinned.py needs that decay so a formerly useful learning can
        -- lose its pinned slot. Protection (this table) is lifetime while
        -- eligibility (that table) is windowed; do not point build-pinned.py
        -- here, and do not give this table replace semantics.
        CREATE TABLE IF NOT EXISTS learning_usefulness_lifetime (
            file_name TEXT PRIMARY KEY,
            injections INTEGER NOT NULL DEFAULT 0,
            substantive INTEGER NOT NULL DEFAULT 0,
            ack_only INTEGER NOT NULL DEFAULT 0,
            last_substantive TEXT,
            first_seen TEXT NOT NULL,
            last_updated TEXT NOT NULL
        );

        -- Holds the `watermark` row: the maximum peek-event timestamp already
        -- folded into learning_usefulness_lifetime. Also never DELETEd from.
        -- The counter merge and the watermark advance must commit together, or
        -- the next run re-folds the same events and double-counts permanently.
        CREATE TABLE IF NOT EXISTS learning_usefulness_lifetime_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()

    # Idempotent migration: add hit-count columns, then the repo identity column
    for col, col_def in [
        ('access_count', 'INTEGER DEFAULT 0'),
        ('last_accessed', 'TEXT'),
        ('repo_root', "TEXT NOT NULL DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE learnings ADD COLUMN {col} {col_def}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Partial index over exactly the rows the recurring backfill looks for.
    # Once every row carries a repo_root the index holds zero entries, so the
    # backfill's SELECT is an empty index probe rather than a full scan of a
    # multi-hundred-megabyte content table. That scan ran once per
    # get_connection, and indexers open one connection per file.
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_learnings_repo_root_pending "
            "ON learnings(id) WHERE scope = 'repo' AND repo_root = ''"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass

    _backfill_repo_roots(conn)


# A learning always lives at <repo_root>/.projects/learnings/<basename>.md, so
# the repo root is recoverable from file_path alone.
_LEARNINGS_PATH_SEGMENT = '/.projects/learnings/'

# resolve_repo_root walks up the filesystem doing syscalls per level, and a
# whole corpus usually collapses onto a handful of distinct repo roots, so the
# same walk repeats thousands of times per reindex. Keyed on the full absolute
# input path, so two tmp dirs (or two tests) can never share an entry.
_REPO_ROOT_RESOLVE_CACHE: Dict[str, str] = {}


def _resolve_repo_root_cached(root: str) -> str:
    cached = _REPO_ROOT_RESOLVE_CACHE.get(root)
    if cached is not None:
        return cached

    import lib.git_utils as git_utils
    resolved = git_utils.resolve_repo_root(root) or root
    _REPO_ROOT_RESOLVE_CACHE[root] = resolved
    return resolved


def _derive_repo_root(file_path: str) -> str:
    """Repo root implied by a learning's file_path, or '' when undecidable.

    The derived path is passed through git_utils.resolve_repo_root so a
    learning written from inside a worktree resolves to the same main-checkout
    root that detect_learning_roots reports at query time. Without that, a
    worktree-authored learning is stranded under a root nothing ever searches.
    """
    if not file_path:
        return ''
    idx = file_path.rfind(_LEARNINGS_PATH_SEGMENT)
    if idx <= 0:
        return ''
    root = file_path[:idx]

    try:
        return _resolve_repo_root_cached(root)
    except Exception:
        # Path resolution must never take the schema down; the literal root is
        # still correct for every non-worktree learning.
        return root


def _backfill_repo_roots(conn: sqlite3.Connection) -> None:
    """Populate `learnings.repo_root` for repo-scoped rows that still lack it.

    THIS BACKFILL IS RECURRING BY DESIGN. _create_schema runs on every
    get_connection, and that recurrence is load-bearing: it is the repair pass
    for every row that reaches the table without a root -- rows written before
    the migration, and rows from any writer that bypasses upsert_document.

    upsert_document now derives repo_root at write time through the same
    _derive_repo_root helper, so a freshly written or re-indexed row is
    scoped-searchable immediately instead of one connection later. That makes
    this pass find nothing in the normal case; it does NOT make it optional,
    and it is not a licence to add a guard.

    NEVER add a run-once guard -- no module flag, no sentinel row, no
    schema-version check. Such a guard still satisfies "no-op on the second
    run" while making every future repo-scoped learning permanently
    unreachable: a silent, fail-closed retrieval outage with no error anywhere.
    The `repo_root = ''` predicate IS the idempotency mechanism and is
    sufficient -- it matches zero rows once every row is healed, and
    idx_learnings_repo_root_pending (a partial index over exactly that
    predicate) makes the healed case an empty index probe rather than a scan
    of the whole content table.

    Only ever an UPDATE. A row whose file_path has no learnings segment keeps
    repo_root = '' and is counted and reported, never deleted.
    """
    try:
        cursor = conn.execute(
            "SELECT id, file_path FROM learnings "
            "WHERE scope = 'repo' AND repo_root = ''"
        )
    except sqlite3.OperationalError:
        return

    updates = []
    stranded = 0
    for row in cursor:
        root = _derive_repo_root(row[1] or '')
        if root:
            updates.append((root, row[0]))
        else:
            stranded += 1

    if updates:
        conn.executemany(
            "UPDATE learnings SET repo_root = ? WHERE id = ? AND repo_root = ''",
            updates,
        )
        conn.commit()

    if stranded:
        print(
            f"[compound-learning] {stranded} repo-scoped learning(s) have a file_path "
            f"with no '{_LEARNINGS_PATH_SEGMENT}' segment, so no repo root can be "
            "derived; they stay unreachable by scoped search until their file_path "
            "is corrected.",
            file=sys.stderr,
        )


# Pinned learnings are already loaded into every context via pinned.md, so peek
# mode must not inject them a second time. Shared here rather than in
# search-learnings.py because prune-stale.py needs the same set to exclude
# pinned files from archival -- two copies would drift, and drift silently
# reintroduces the double-injection loop.
DEFAULT_PINNED_MD_PATH = '~/.claude/plugins/compound-learning/pinned.md'
# Matches both the bare form (`_source: foo.md_`) and the annotated form
# build-pinned.py emits (`_source: foo.md (2 substantive uses / 5 injections, ...)_`).
# Keep this tolerant: if it stops matching, pinned entries get auto-peeked a second
# time and the double-injection feedback loop comes back.
PINNED_SOURCE_RE = re.compile(r'^_source:\s*(.+?\.md)(?:\s.*)?_\s*$')


def load_pinned_sources() -> Set[str]:
    """Return the learning file basenames listed in pinned.md.

    Returns an empty set on any read failure: retrieval must keep working when
    pinned.md is absent or unreadable.
    """
    path = os.environ.get('COMPOUND_LEARNING_PINNED_MD') or os.path.expanduser(DEFAULT_PINNED_MD_PATH)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        return set()

    sources: Set[str] = set()
    for line in lines:
        match = PINNED_SOURCE_RE.match(line.strip())
        if match:
            sources.add(match.group(1).strip())
    return sources


def get_embedding(text: str):
    """Lazy-load sentence-transformers model and return embedding as list."""
    global _model
    with _model_lock:
        if _model is None:
            model_cache = os.path.expanduser(
                '~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2'
            )
            if not os.path.exists(model_cache):
                print("Downloading embedding model (one-time, ~80MB)...", file=sys.stderr)
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                print(
                    '[ERROR] sentence-transformers is not installed.\n'
                    'Install the required dependency:\n'
                    '  pip install sentence-transformers'
                )
                raise
            _model = SentenceTransformer('all-MiniLM-L6-v2')
    return _model.encode(text, normalize_embeddings=True).tolist()


def upsert_document(
    conn: sqlite3.Connection,
    doc_id: str,
    content: str,
    metadata: Dict[str, Any],
) -> None:
    """Atomic upsert into learnings + vec_learnings + fts_learnings."""
    from datetime import datetime, timezone

    embedding = get_embedding(content)
    created_at = metadata.get('created_at', datetime.now(timezone.utc).isoformat())

    scope = metadata.get('scope', 'global')
    file_path = metadata.get('file_path', '')

    # Populate repo_root here rather than leaving it to the next connection's
    # backfill. Same helper, same derivation, so the two write paths can never
    # disagree; the backfill stays as the repair pass for rows written before
    # this and for any writer that bypasses upsert_document. Only repo-scoped
    # rows carry a root -- search matches global rows on scope alone.
    repo_root = _derive_repo_root(file_path) if scope == 'repo' else ''

    # Delete-then-insert strategy (virtual tables don't support ON CONFLICT cleanly)
    delete_document(conn, doc_id)

    conn.execute(
        """INSERT INTO learnings (id, content, scope, repo, file_path, topic, keywords, created_at, access_count, last_accessed, repo_root)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            doc_id,
            content,
            scope,
            metadata.get('repo', ''),
            file_path,
            metadata.get('topic', 'other'),
            metadata.get('keywords', ''),
            created_at,
            metadata.get('access_count', 0),
            metadata.get('last_accessed', None),
            repo_root,
        ),
    )

    import struct
    embedding_blob = struct.pack(f'{len(embedding)}f', *embedding)
    conn.execute(
        "INSERT INTO vec_learnings (id, embedding) VALUES (?, ?)",
        (doc_id, embedding_blob),
    )

    conn.execute(
        "INSERT INTO fts_learnings (id, content) VALUES (?, ?)",
        (doc_id, content),
    )

    conn.commit()


def delete_document(conn: sqlite3.Connection, doc_id: str) -> None:
    """Remove a document from all three tables."""
    conn.execute("DELETE FROM learnings WHERE id = ?", (doc_id,))
    conn.execute("DELETE FROM vec_learnings WHERE id = ?", (doc_id,))
    conn.execute("DELETE FROM fts_learnings WHERE id = ?", (doc_id,))
    conn.commit()


def search(
    conn: sqlite3.Connection,
    query_text: str,
    scope_repo_roots: List[str],
    n_results: int = 10,
    threshold: float = 1.0,
) -> List[Dict[str, Any]]:
    """
    KNN vector search filtered by scope, returns list of result dicts.

    Each result has: id, document, metadata (dict), distance.
    scope_repo_roots: absolute repo roots in scope (global always included).
    """
    import struct

    query_emb = get_embedding(query_text)
    embedding_blob = struct.pack(f'{len(query_emb)}f', *query_emb)

    # Build scope WHERE clause.
    #
    # Matched on repo_root (an absolute path), never on `repo` (a bare
    # directory name). A basename is not an identity: three different
    # repositories across two clients are all named `platform`, and matching on
    # the name injected one client's learnings into the other's session.
    # `repo` is retained as a display label only.
    scope_conditions = ["l.scope = 'global'"]
    params: List[Any] = []
    for repo_root in scope_repo_roots:
        scope_conditions.append("(l.scope = 'repo' AND l.repo_root = ?)")
        params.append(repo_root)
    scope_sql = ' OR '.join(scope_conditions)

    # KNN query joined with learnings for scope filtering.
    #
    # vec_learnings is declared with NO distance metric, so sqlite-vec returns
    # an L2 distance here, not a cosine distance. See the normalization note
    # below for how a cosine is recovered from it.
    rows = conn.execute(
        f"""
        SELECT l.id, l.content, l.scope, l.repo, l.file_path, l.topic, l.keywords,
               l.access_count, l.last_accessed, v.distance
        FROM vec_learnings v
        JOIN learnings l ON l.id = v.id
        WHERE v.embedding MATCH ?
          AND k = ?
          AND ({scope_sql})
        ORDER BY v.distance
        """,
        [embedding_blob, n_results * 3] + params,
    ).fetchall()

    results = []
    for row in rows:
        # L2 distance over unit-normalized embeddings is in [0, 2]; halve it to
        # [0, 1] so existing thresholds (0.5, 0.6, 0.25) work unchanged.
        #
        # The halved value is an L2 distance, not a cosine distance, so
        # `1 - dist` is NOT a cosine similarity. Recover cosine as
        # `1 - 2 * dist**2` (reference: scripts/dedupe-on-write.py cosine_from).
        # Every threshold compared against this value was tuned empirically
        # against the numbers this line produces, so the calibration is correct
        # as shipped; do not shift any of them because of the metric label.
        dist = float(row['distance']) / 2.0
        if dist > threshold:
            continue
        results.append({
            'id': row['id'],
            'document': row['content'],
            'metadata': {
                'scope': row['scope'],
                'repo': row['repo'],
                'file_path': row['file_path'],
                'topic': row['topic'],
                'keywords': row['keywords'],
                'access_count': row['access_count'],
                'last_accessed': row['last_accessed'],
            },
            'distance': round(dist, 4),
        })
        if len(results) >= n_results:
            break

    return results


def get_all_documents(
    conn: sqlite3.Connection, include_content: bool = True
) -> Dict[str, Any]:
    """Return all documents in a dict format compatible with consolidate-discovery."""
    rows = conn.execute(
        "SELECT id, content, scope, repo, file_path, topic, keywords FROM learnings"
    ).fetchall()

    ids = []
    documents = []
    metadatas = []

    for row in rows:
        ids.append(row['id'])
        documents.append(row['content'] if include_content else '')
        metadatas.append({
            'scope': row['scope'],
            'repo': row['repo'],
            'file_path': row['file_path'],
            'topic': row['topic'],
            'keywords': row['keywords'],
        })

    return {'ids': ids, 'documents': documents, 'metadatas': metadatas}


def get_documents_by_ids(
    conn: sqlite3.Connection, ids: List[str]
) -> Dict[str, Any]:
    """Fetch specific documents by ID list."""
    if not ids:
        return {'ids': [], 'documents': [], 'metadatas': []}

    placeholders = ','.join('?' * len(ids))
    rows = conn.execute(
        f"SELECT id, content, scope, repo, file_path, topic, keywords FROM learnings WHERE id IN ({placeholders})",
        ids,
    ).fetchall()

    # Preserve requested order
    row_map = {row['id']: row for row in rows}
    result_ids = []
    result_docs = []
    result_metas = []

    for doc_id in ids:
        if doc_id in row_map:
            row = row_map[doc_id]
            result_ids.append(row['id'])
            result_docs.append(row['content'])
            result_metas.append({
                'scope': row['scope'],
                'repo': row['repo'],
                'file_path': row['file_path'],
                'topic': row['topic'],
                'keywords': row['keywords'],
            })

    return {'ids': result_ids, 'documents': result_docs, 'metadatas': result_metas}


def increment_hit_count(conn: sqlite3.Connection, doc_id: str, timestamp: str) -> None:
    """Increment access_count by 1 and update last_accessed for a document."""
    conn.execute(
        "UPDATE learnings SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
        (timestamp, doc_id),
    )
    conn.commit()


def count_documents(conn: sqlite3.Connection) -> int:
    """Return total number of indexed documents."""
    row = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()
    return row[0] if row else 0
