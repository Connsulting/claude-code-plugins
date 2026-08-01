#!/usr/bin/env python3
"""
Archive learnings that have measurably never been used, once past a grace period.

Why this exists: the 2026-07-21 corpus crawl pruned 3,291 rows down to 2,151 and
deliberately held back ~669 recently-created zero-hit rows on a 14-day grace, on
the assumption that a later review would sweep them when the grace expired. That
review never swept them. By 2026-07-29 the corpus was back to 2,631 rows with 305
zero-hit rows sitting past grace, because "sweep it next time" was a note in a
findings doc rather than a job. This script is that job.

A learning becomes a candidate through either of two independent clauses:

  * `never-retrieved` -- `access_count` == 0, past the grace period, and with no
    measured peek evidence at all. This is the original rule.
  * `opportunity-floor` -- zero lifetime substantive uses across at least
    `pruneOpportunityFloor` lifetime injections, past `pruneOpportunityMinAgeDays`.
    **`access_count` is deliberately not consulted here.** As a hard precondition
    it granted permanent immunity to anything ever searched once, which put the
    whole high-injection / zero-citation cohort out of reach.

Usefulness evidence is filed under a BASENAME, which is not a learning identity:
the same name exists under a global and a repo scope, and across client repos.
Basename matching is safe for protection (it over-retains) and unsafe for
eligibility, where one file's injections would push a same-named other file over
the floor. So the opportunity clause only judges rows whose basename identifies
exactly one learning; ambiguous rows are retained. See `attributable_basenames`.

Protection is LIFETIME while eligibility is WINDOWED. A learning cited 45 days
ago has no row left in the 30-day rolling `peek_usefulness` table, so the
protection set unions `learning_usefulness_lifetime` as well. Getting those two
backwards archives the corpus's best entries first. Files listed in `pinned.md`
are excluded outright.

Grace exists because a learning written yesterday has had no chance to be used;
pruning on age-since-creation alone would delete the newest learnings first.

Archive, never delete: files move to `learnings.archiveDir/<date>/` and the rows
leave all three tables (learnings, vec_learnings, fts_learnings). Recovering one
is a `mv` back plus a reindex.

Run:
  python3 prune-stale.py                    # dry run, report only (default)
  python3 prune-stale.py --apply            # archive + de-index
  python3 prune-stale.py --grace-days 30    # be more conservative
  python3 prune-stale.py --limit 50         # cap the batch
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

_PLUGIN_ROOT = os.environ.get(
    'CLAUDE_PLUGIN_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

import lib._site_packages  # noqa: F401

import lib.db as db

DEFAULT_GRACE_DAYS = 14
DEFAULT_OPPORTUNITY_FLOOR = 10
DEFAULT_OPPORTUNITY_MIN_AGE_DAYS = 30

# Below this the opportunity clause stops being a retention rule and becomes a
# guillotine: at 0 it degenerates to plain `substantive = 0`, which takes the
# corpus from 2,328 rows to 34 -- unattended, on the weekly timer.
MIN_OPPORTUNITY_FLOOR = 5

# Same hazard on the grace knob, which is the threshold for clause 1. Measured
# on the live corpus (2,333 rows, only 34 files carrying any protection at all):
# grace 14 selects 155 rows (6.6%), grace 7 selects 514 (22%), grace 1 selects
# 835 and grace 0 selects 851 (36%). One mistyped digit in config.json plus the
# weekly timer is a corpus-wide sweep, so the knob needs the same clamp its
# sibling got. 7 is the shortest window in which a newly written learning has
# had a full week of sessions to be retrieved at all; below that the clause is
# selecting on youth rather than on disuse.
MIN_GRACE_DAYS = 7

# Blast-radius breaker: an upper bound on how much of the corpus ONE unattended
# run may touch, independent of which knob went wrong. At the intended settings
# a run selects 155 of 2,333 rows (6.6%), so a 10% share limit (233 rows) leaves
# roughly 1.5x headroom for normal drift while refusing every measured runaway
# above (grace 7 at 514 rows, grace 0 at 851).
MAX_CANDIDATE_SHARE = 0.10
# The share alone would block a legitimate first sweep on a small or new corpus
# (10% of 300 rows is 30), so a flat allowance floors it. It is also the hard
# bound on what a single run can take when the corpus is tiny.
SMALL_CORPUS_ALLOWANCE = 50
# Absolute cap, overridable via `learnings.pruneMaxCandidates`. The effective
# limit is the MINIMUM of this and the share limit, so raising it can never
# widen the breaker past MAX_CANDIDATE_SHARE; config can only ever tighten.
# There is deliberately no sentinel for "unlimited": 0, null, or a non-integer
# is a typo, and a typo here must refuse rather than remove the guard.
DEFAULT_MAX_CANDIDATES = 300

USEFULNESS_TABLES = ('peek_usefulness', 'learning_usefulness_lifetime')


def substantive_files(conn) -> set:
    """Basenames with at least one recorded substantive use, in ANY window.

    Unions the 30-day rolling table with the lifetime accumulator: protection
    must be lifetime even though eligibility is windowed, or a learning cited 45
    days ago loses all protection the moment its rolling row expires.

    BASENAME MATCHING HERE IS DELIBERATE AND IS THE SAFE DIRECTION. Both
    usefulness tables are keyed on a basename because the source evidence is:
    the auto-peek hook prints `-> <basename>: <summary>`, so a citation can
    never be attributed to one of two same-named files after the fact. When a
    basename is ambiguous, crediting the citation to every row that shares it
    OVER-protects -- it retains a learning that may not have earned it, which is
    recoverable. Narrowing this to one row would strip protection from the row
    that actually was cited, which is not. Eligibility is where basename
    matching inverts and becomes unsafe; see `attributable_basenames`.

    Returns an empty set if both tables are absent, which makes the guard fail
    CLOSED (nothing is protected) -- so callers must treat missing tables as a
    reason not to apply. See `usefulness_available`.
    """
    names: set = set()
    for table in USEFULNESS_TABLES:
        try:
            rows = conn.execute(
                f'SELECT file_name FROM {table} WHERE substantive > 0'
            ).fetchall()
        except Exception:
            continue
        names.update(r[0] for r in rows if r and r[0])
    return names


def usefulness_available(conn) -> bool:
    """True when either usefulness table has been populated at least once.

    Accepting either matters after a quiet 30 days: the rolling table is empty
    then, and refusing on that alone would stop the pruner during exactly the
    periods when pruning is safest.
    """
    for table in USEFULNESS_TABLES:
        try:
            n, = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()
        except Exception:
            continue
        if n > 0:
            return True
    return False


def pinned_files() -> set:
    """Basenames listed in pinned.md, which are already resident in every context.

    All currently pinned files also carry substantive >= 2 and so are protected
    structurally -- but that is an accident of today's data, not a guarantee.

    Reads pinned.md. main() calls this ONCE and passes the result to both the
    candidate filter and the pinned.md refusal, so one run never reads the file
    twice and the two consumers can never see different contents.
    """
    return db.load_pinned_sources()


def resolve_thresholds(config: Dict[str, Any]) -> tuple:
    """(opportunity floor, opportunity min age) resolved from one config object.

    The single place either threshold is derived. main() resolves them here and
    passes them into find_candidates, so the value the sanity clamp polices is
    the same value that selects rows -- a second independent read would let the
    clamp guard a threshold nothing acts on.
    """
    learn = config.get('learnings', {}) or {}
    return (
        learn.get('pruneOpportunityFloor', DEFAULT_OPPORTUNITY_FLOOR),
        learn.get('pruneOpportunityMinAgeDays', DEFAULT_OPPORTUNITY_MIN_AGE_DAYS),
    )


def attributable_basenames(conn) -> set:
    """Basenames carried by exactly ONE learning row, so their evidence is theirs.

    The usefulness tables are keyed on a basename, but a basename is not a
    learning identity: the same name exists under a global scope and a repo
    scope, and across different client repositories. When two rows share one,
    the single usefulness row is the SUM of both files' events and cannot be
    split.

    That ambiguity is harmless for protection (it over-retains) and fatal for
    eligibility. The opportunity floor archives on `injections >= floor AND
    substantive = 0`, so file A's ten unused injections would push file B over
    the floor and the unattended weekly sweep would archive B -- a learning
    nobody ever injected -- permanently. Only rows whose basename appears once
    may be judged by the opportunity clause; every other row falls through to
    clause 1, which reads `access_count` off the row itself and is already
    identity-correct.

    Failing toward protection is the whole point: an unattributable row is
    retained, never archived.
    """
    rows = conn.execute(
        'SELECT file_path FROM learnings'
    ).fetchall()
    counts: Dict[str, int] = {}
    for (file_path,) in rows:
        name = os.path.basename(file_path or '')
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    return {name for name, n in counts.items() if n == 1}


def _usefulness_counts(conn, table: str) -> Dict[str, tuple]:
    """{basename: (injections, substantive)} for one usefulness table."""
    try:
        rows = conn.execute(
            f'SELECT file_name, injections, substantive FROM {table}'
        ).fetchall()
    except Exception:
        return {}
    return {r[0]: (r[1] or 0, r[2] or 0) for r in rows if r and r[0]}


def find_candidates(
    conn,
    grace_days: int,
    limit: int | None,
    *,
    floor: int,
    min_age_days: int,
    pinned: set,
) -> List[Dict[str, Any]]:
    """Rows eligible for archival, tagged with the clause that selected them.

    All three thresholds are REQUIRED and there is deliberately no default
    resolution here. Callers resolve them once (main() via resolve_thresholds
    and pinned_files) and pass them in, so the floor that selects rows here is
    the identical object the sanity clamp refused on. A defaulted second read
    would let the clamp guard a threshold nothing acts on.
    """
    now = datetime.now(timezone.utc)
    grace_cutoff = (now - timedelta(days=grace_days)).isoformat()
    opportunity_cutoff = (now - timedelta(days=min_age_days)).isoformat()

    protected = substantive_files(conn) | pinned
    lifetime = _usefulness_counts(conn, 'learning_usefulness_lifetime')
    # Only these basenames identify a single learning, so only these rows may be
    # judged on lifetime injection counts. See attributable_basenames.
    attributable = attributable_basenames(conn)

    # One pass over the corpus ordered by age, so `limit` cuts by age rather
    # than starving whichever clause happens to be evaluated second.
    rows = conn.execute(
        'SELECT id, file_path, created_at, LENGTH(content), COALESCE(access_count, 0) '
        'FROM learnings '
        'ORDER BY created_at'
    ).fetchall()

    candidates: List[Dict[str, Any]] = []
    for doc_id, file_path, created_at, size, access_count in rows:
        name = os.path.basename(file_path or '')
        if name in protected:
            continue

        # Lifetime evidence counts for this row ONLY when the basename it is
        # filed under belongs to this row alone. A shared basename reads as zero
        # injections, which makes the opportunity clause skip the row.
        life_injections, life_substantive = (
            lifetime.get(name, (0, 0)) if name in attributable else (0, 0)
        )
        created = created_at or ''

        # Clause 1 is today's rule, unchanged.
        if access_count == 0 and created < grace_cutoff:
            reason = 'never-retrieved'
        # Clause 2 is the fix, and it deliberately does NOT consult access_count:
        # as a precondition it granted permanent immunity to anything ever
        # searched once, putting this whole cohort out of reach.
        elif (life_substantive == 0 and life_injections >= floor
                and created < opportunity_cutoff):
            reason = 'opportunity-floor'
        else:
            continue

        candidates.append({
            'id': doc_id,
            'file_path': file_path,
            'created_at': created_at,
            'chars': size or 0,
            'reason': reason,
        })
        if limit is not None and len(candidates) >= limit:
            break
    return candidates


def _free_archive_path(target_dir: str, filename: str) -> str:
    """A path under target_dir that does not exist yet, suffixing _1, _2, ... ."""
    archive_path = os.path.join(target_dir, filename)
    counter = 1
    while os.path.exists(archive_path):
        name, ext = os.path.splitext(filename)
        archive_path = os.path.join(target_dir, f'{name}_{counter}{ext}')
        counter += 1
    return archive_path


def archive_one(candidate: Dict[str, Any], target_dir: str) -> Dict[str, Any]:
    """Move the backing markdown aside. Returns the action taken for reporting."""
    file_path = candidate['file_path'] or ''
    if not file_path or not os.path.exists(file_path):
        return {'note': 'row only (file already gone)'}

    archive_path = _free_archive_path(target_dir, os.path.basename(file_path))
    shutil.move(file_path, archive_path)
    return {'archived_to': archive_path}


def archive_row_content(conn, candidate: Dict[str, Any], target_dir: str) -> str:
    """Write the row's own `content` into the archive when its file is gone.

    `learnings.content` holds a full copy of the markdown, so when the backing
    file has already been removed the ROW is the last copy that exists. Deleting
    it without producing an artifact breaks "archive, never delete" on precisely
    the branch where nothing can be recovered afterwards. Called before
    delete_document, never after.
    """
    row = conn.execute(
        'SELECT content FROM learnings WHERE id = ?', (candidate['id'],)
    ).fetchone()
    content = (row[0] if row else None) or ''

    filename = os.path.basename(candidate['file_path'] or '') or f"{candidate['id']}.md"
    archive_path = _free_archive_path(target_dir, filename)
    with open(archive_path, 'w', encoding='utf-8') as handle:
        handle.write(content)
    return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true',
                        help='actually archive; default is a dry run')
    parser.add_argument('--grace-days', type=int, default=None,
                        help='protect rows newer than this '
                             f'(default: config pruneGraceDays, else {DEFAULT_GRACE_DAYS})')
    parser.add_argument('--limit', type=int, default=None,
                        help='cap the number of learnings processed')
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    args = parser.parse_args()

    config = db.load_config()
    learn = config.get('learnings', {}) or {}
    grace_days = (args.grace_days if args.grace_days is not None
                  else learn.get('pruneGraceDays', DEFAULT_GRACE_DAYS))
    # Resolved ONCE, here, and handed to find_candidates: the value the clamp
    # below refuses on has to be the value that selects rows.
    floor, min_age_days = resolve_thresholds(config)
    max_candidates = learn.get('pruneMaxCandidates', DEFAULT_MAX_CANDIDATES)
    max_pinned = (config.get('pinned', {}) or {}).get('maxEntries', 0)
    # Read once; both the candidate filter and the pinned.md refusal use it.
    pinned = pinned_files()

    conn = db.get_connection(config)
    try:
        total, = conn.execute('SELECT COUNT(*) FROM learnings').fetchone()
        have_usefulness = usefulness_available(conn)
        candidates = find_candidates(
            conn, grace_days, args.limit,
            floor=floor, min_age_days=min_age_days, pinned=pinned,
        )

        # A bad pruneMaxCandidates cannot widen the breaker: the effective limit
        # is the smaller of the configured cap and the share of the corpus.
        cap_is_valid = isinstance(max_candidates, int) and not isinstance(
            max_candidates, bool) and max_candidates > 0
        breaker = min(
            max_candidates if cap_is_valid else DEFAULT_MAX_CANDIDATES,
            max(SMALL_CORPUS_ALLOWANCE, int(total * MAX_CANDIDATE_SHARE)),
        )

        def refuse(msg: str) -> int:
            print(json.dumps({'status': 'refused', 'reason': msg}) if args.json else msg,
                  file=sys.stderr)
            return 2

        # Refuse to apply without usefulness data: with both usefulness tables
        # empty the substantive-use guard silently protects nothing, and a file
        # cited ten times through peek looks identical to one nobody ever read.
        if args.apply and not have_usefulness:
            return refuse(
                'REFUSING to apply: peek_usefulness and learning_usefulness_lifetime '
                'are empty or missing, so the substantive-use guard cannot protect '
                'anything. Run `analyze-peeks.py 30 --persist` first, then retry.')

        # The floor is the only thing between the opportunity clause and a sweep
        # that empties the corpus, and this runs unattended on a weekly timer.
        # Dry runs are not blocked: exploring the tradeoff is what they are for.
        if args.apply and floor < MIN_OPPORTUNITY_FLOOR:
            return refuse(
                f'REFUSING to apply: pruneOpportunityFloor is {floor}, below the '
                f'minimum of {MIN_OPPORTUNITY_FLOOR}. Below that the opportunity '
                'clause degenerates into a corpus-wide sweep.')

        # Same clamp on the other threshold knob. Clause 1 is the wider of the
        # two clauses on the live corpus, so an unclamped grace is the larger
        # hazard of the pair. Dry runs stay open for the same reason.
        if args.apply and grace_days < MIN_GRACE_DAYS:
            return refuse(
                f'REFUSING to apply: grace is {grace_days} days, below the minimum '
                f'of {MIN_GRACE_DAYS}. Measured, grace 7 already selects 22% of the '
                'corpus and grace 0 selects 36%, so below the minimum the clause '
                'selects on youth rather than on disuse.')

        # No sentinel means unlimited. 0, null, or a non-integer is a typo, and
        # a typo must not be the thing that removes the guard.
        if args.apply and not cap_is_valid:
            return refuse(
                f'REFUSING to apply: pruneMaxCandidates is {max_candidates!r}, which '
                'is not a positive integer. There is no value meaning "no limit".')

        # Blast-radius breaker. Independent of WHICH knob went wrong: whatever
        # the thresholds say, one unattended run may not take more than
        # MAX_CANDIDATE_SHARE of the corpus. At the intended settings a run
        # selects 6.6%, so this refuses only runaways.
        if args.apply and len(candidates) > breaker:
            return refuse(
                f'REFUSING to apply: {len(candidates)} candidates exceeds the '
                f'blast-radius limit of {breaker} for a corpus of {total} rows '
                f'(cap {max_candidates}, share {MAX_CANDIDATE_SHARE:.0%}, floor '
                f'{SMALL_CORPUS_ALLOWANCE}). Re-run with --limit to sweep in '
                'batches, or widen the thresholds.')

        # load_pinned_sources() returns an empty set when pinned.md is missing or
        # unreadable. That fails OPEN for retrieval (correct) but CLOSED for
        # protection (wrong), so a bad pinned.md deploy plus the weekly timer
        # would archive the pinned entries themselves.
        if args.apply and max_pinned > 0 and not pinned:
            return refuse(
                'REFUSING to apply: pinned.md yielded no entries while '
                f'pinned.maxEntries is {max_pinned}, so the pinned exclusion '
                'protects nothing. Run `build-pinned.py` first, then retry.')

        archived: List[Dict[str, Any]] = []
        if args.apply and candidates:
            target_dir = os.path.join(
                config['learnings']['archiveDir'],
                datetime.now().strftime('%Y-%m-%d'),
            )
            os.makedirs(target_dir, exist_ok=True)
            for c in candidates:
                outcome = archive_one(c, target_dir)
                if 'archived_to' not in outcome:
                    outcome = {
                        'note': 'file already gone; content recovered from the row',
                        'archived_to': archive_row_content(conn, c, target_dir),
                    }
                db.delete_document(conn, c['id'])
                archived.append({**c, **outcome})

        reclaimed = sum(c['chars'] for c in candidates)
        by_reason: Dict[str, int] = {}
        for c in candidates:
            reason = c.get('reason', 'unknown')
            by_reason[reason] = by_reason.get(reason, 0) + 1

        # corpus_after is the PROJECTED size in both modes: a dry run that just
        # repeats the current size cannot answer "should I run the sweep".
        report = {
            'status': 'applied' if args.apply else 'dry-run',
            'corpus_before': total,
            'corpus_after': total - len(candidates),
            'corpus_projected_delta': -len(candidates),
            'grace_days': grace_days,
            'opportunity_floor': floor,
            'blast_radius_limit': breaker,
            'candidates': len(candidates),
            'by_reason': by_reason,
            'chars_reclaimed': reclaimed,
            'usefulness_data': have_usefulness,
        }

        if args.json:
            print(json.dumps({**report, 'items': archived or candidates}, indent=2))
        else:
            verb = 'Archived' if args.apply else 'Would archive'
            print(f"corpus {total} rows, grace {grace_days}d, "
                  f"opportunity floor {floor}, "
                  f"usefulness_data={'yes' if have_usefulness else 'NO'}")
            print(f"corpus_before {total} -> corpus_after "
                  f"{total - len(candidates)} ({-len(candidates)})")
            print(f"{verb} {len(candidates)} learning(s), {reclaimed:,} chars")
            for reason in sorted(by_reason):
                print(f"  by_reason  {reason}: {by_reason[reason]}")
            for c in (archived or candidates)[:20]:
                where = c.get('archived_to', '')
                print(f"  {c['created_at'][:10]}  {c['chars']:>6}  "
                      f"{os.path.basename(c['file_path'] or c['id'])}"
                      f"{'  -> ' + where if where else ''}")
            if len(candidates) > 20:
                print(f"  ... and {len(candidates) - 20} more")
            if not args.apply and candidates:
                print("\nDry run. Re-run with --apply to archive.")
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
