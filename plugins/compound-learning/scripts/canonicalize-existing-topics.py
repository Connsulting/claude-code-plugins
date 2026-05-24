#!/usr/bin/env python3
"""
One-time backfill: apply canonicalize_topic() to every existing learning's
topic column. Pure metadata rewrite (no re-embedding required), so this runs
in seconds even on a large corpus.

Default is dry-run (prints proposed changes). Pass --apply to commit.
"""

import argparse
import os
import sys
from collections import Counter

_PLUGIN_ROOT = os.environ.get(
    'CLAUDE_PLUGIN_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

import lib._site_packages  # noqa: F401

import lib.db as db
from lib.topic_mapping import canonicalize_topic


def main(apply: bool, limit_print: int) -> int:
    config = db.load_config()
    conn = db.get_connection(config)

    rows = conn.execute(
        "SELECT id, topic FROM learnings"
    ).fetchall()

    changes: list[tuple[str, str, str]] = []  # (id, old, new)
    no_change = 0
    for r in rows:
        old = r['topic'] or ''
        new, _ = canonicalize_topic(old)
        if new and new != old:
            changes.append((r['id'], old, new))
        else:
            no_change += 1

    print(f"Total rows scanned: {len(rows)}")
    print(f"Unchanged: {no_change}")
    print(f"Would canonicalize: {len(changes)}")
    print()

    # Show top transitions by count for human eyeballing
    transition_counts = Counter((old, new) for _, old, new in changes)
    if transition_counts:
        print(f"TOP TRANSITIONS (showing {limit_print}):")
        for (old, new), count in transition_counts.most_common(limit_print):
            print(f"  {count:4d}  {old!r}  ->  {new!r}")
        print()

    if not changes:
        conn.close()
        return 0

    if not apply:
        print("DRY-RUN: pass --apply to commit these changes.")
        conn.close()
        return 0

    for doc_id, _old, new in changes:
        conn.execute("UPDATE learnings SET topic = ? WHERE id = ?", (new, doc_id))
    conn.commit()
    conn.close()
    print(f"APPLIED: updated {len(changes)} rows.")
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backfill topic canonicalization on existing DB rows')
    parser.add_argument('--apply', action='store_true',
                        help='Commit the changes. Default is dry-run.')
    parser.add_argument('--limit-print', type=int, default=20,
                        help='Top N transitions to display (default: 20)')
    args = parser.parse_args()
    sys.exit(main(args.apply, args.limit_print))
