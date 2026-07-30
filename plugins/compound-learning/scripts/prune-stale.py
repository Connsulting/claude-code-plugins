#!/usr/bin/env python3
"""
Archive learnings that have measurably never been used, once past a grace period.

Why this exists: the 2026-07-21 corpus crawl pruned 3,291 rows down to 2,151 and
deliberately held back ~669 recently-created zero-hit rows on a 14-day grace, on
the assumption that a later review would sweep them when the grace expired. That
review never swept them. By 2026-07-29 the corpus was back to 2,631 rows with 305
zero-hit rows sitting past grace, because "sweep it next time" was a note in a
findings doc rather than a job. This script is that job.

Two independent signals must BOTH say unused before anything moves:

  * `learnings.access_count` == 0 -- never explicitly retrieved by a search.
  * `peek_usefulness.substantive` == 0 -- never cited after an auto-peek
    injection. A file injected 50 times and cited twice has earned its place even
    though its access_count is 0, because peek injections deliberately do not
    feed access_count (that self-reinforcing loop was removed on purpose).

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


def substantive_files(conn) -> set:
    """Basenames with at least one recorded substantive use.

    Returns an empty set if peek_usefulness is absent, which makes the guard
    fail CLOSED (nothing is protected) -- so callers must treat a missing table
    as a reason not to apply. See `usefulness_available`.
    """
    try:
        rows = conn.execute(
            'SELECT file_name FROM peek_usefulness WHERE substantive > 0'
        ).fetchall()
    except Exception:
        return set()
    return {r[0] for r in rows if r and r[0]}


def usefulness_available(conn) -> bool:
    """True when peek_usefulness exists and has been populated at least once."""
    try:
        n, = conn.execute('SELECT COUNT(*) FROM peek_usefulness').fetchone()
        return n > 0
    except Exception:
        return False


def find_candidates(conn, grace_days: int, limit: int | None) -> List[Dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=grace_days)).isoformat()
    protected = substantive_files(conn)

    rows = conn.execute(
        'SELECT id, file_path, created_at, LENGTH(content) '
        'FROM learnings '
        'WHERE COALESCE(access_count, 0) = 0 AND created_at < ? '
        'ORDER BY created_at',
        (cutoff,),
    ).fetchall()

    candidates: List[Dict[str, Any]] = []
    for doc_id, file_path, created_at, size in rows:
        if os.path.basename(file_path or '') in protected:
            continue
        candidates.append({
            'id': doc_id,
            'file_path': file_path,
            'created_at': created_at,
            'chars': size or 0,
        })
        if limit is not None and len(candidates) >= limit:
            break
    return candidates


def archive_one(candidate: Dict[str, Any], target_dir: str) -> Dict[str, Any]:
    """Move the backing markdown aside. Returns the action taken for reporting."""
    file_path = candidate['file_path'] or ''
    if not file_path or not os.path.exists(file_path):
        return {'note': 'row only (file already gone)'}

    filename = os.path.basename(file_path)
    archive_path = os.path.join(target_dir, filename)
    counter = 1
    while os.path.exists(archive_path):
        name, ext = os.path.splitext(filename)
        archive_path = os.path.join(target_dir, f'{name}_{counter}{ext}')
        counter += 1

    shutil.move(file_path, archive_path)
    return {'archived_to': archive_path}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true',
                        help='actually archive; default is a dry run')
    parser.add_argument('--grace-days', type=int, default=DEFAULT_GRACE_DAYS,
                        help=f'protect rows newer than this (default {DEFAULT_GRACE_DAYS})')
    parser.add_argument('--limit', type=int, default=None,
                        help='cap the number of learnings processed')
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    args = parser.parse_args()

    config = db.load_config()
    conn = db.get_connection(config)
    try:
        total, = conn.execute('SELECT COUNT(*) FROM learnings').fetchone()
        have_usefulness = usefulness_available(conn)
        candidates = find_candidates(conn, args.grace_days, args.limit)

        # Refuse to apply without usefulness data: with peek_usefulness empty the
        # substantive-use guard silently protects nothing, and a file cited ten
        # times through peek looks identical to one nobody ever read.
        if args.apply and not have_usefulness:
            msg = ('REFUSING to apply: peek_usefulness is empty or missing, so the '
                   'substantive-use guard cannot protect anything. Run '
                   '`analyze-peeks.py 30 --persist` first, then retry.')
            print(json.dumps({'status': 'refused', 'reason': msg}) if args.json else msg,
                  file=sys.stderr)
            return 2

        archived: List[Dict[str, Any]] = []
        if args.apply and candidates:
            target_dir = os.path.join(
                config['learnings']['archiveDir'],
                datetime.now().strftime('%Y-%m-%d'),
            )
            os.makedirs(target_dir, exist_ok=True)
            for c in candidates:
                outcome = archive_one(c, target_dir)
                db.delete_document(conn, c['id'])
                archived.append({**c, **outcome})

        reclaimed = sum(c['chars'] for c in candidates)
        report = {
            'status': 'applied' if args.apply else 'dry-run',
            'corpus_before': total,
            'corpus_after': total - (len(archived) if args.apply else 0),
            'grace_days': args.grace_days,
            'candidates': len(candidates),
            'chars_reclaimed': reclaimed,
            'usefulness_data': have_usefulness,
        }

        if args.json:
            print(json.dumps({**report, 'items': archived or candidates}, indent=2))
        else:
            verb = 'Archived' if args.apply else 'Would archive'
            print(f"corpus {total} rows, grace {args.grace_days}d, "
                  f"usefulness_data={'yes' if have_usefulness else 'NO'}")
            print(f"{verb} {len(candidates)} never-used learning(s), "
                  f"{reclaimed:,} chars")
            for c in (archived or candidates)[:20]:
                where = c.get('archived_to') or c.get('note', '')
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
