#!/usr/bin/env python3
"""
Inspect the topic landscape in the learnings DB.

Shows distinct topics ordered by file count, flagging which would be
canonicalized differently than they currently are (drift indicator) and
which are new vocabulary not yet in TOPIC_ALIASES (review candidates).

Run after canonicalization changes or periodically to spot fragmentation.
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

import lib._site_packages  # noqa: F401  -- ensures site-packages on sys.path before third-party imports

import lib.db as db
from lib.topic_mapping import TOPIC_ALIASES, TOPIC_TAG_MAP, canonicalize_topic


def main(limit: int) -> int:
    config = db.load_config()
    conn = db.get_connection(config)
    rows = conn.execute("SELECT topic FROM learnings WHERE scope='global'").fetchall()
    conn.close()

    counts = Counter(r['topic'] or '' for r in rows)
    total = sum(counts.values())
    distinct = len(counts)
    singletons = sum(1 for v in counts.values() if v == 1)

    print(f"Total entries: {total}")
    print(f"Distinct topics: {distinct}")
    print(f"Singletons: {singletons} ({100*singletons/distinct:.1f}% of distinct)")
    print()

    # Drift: topics whose current stored value differs from what canonicalize_topic would produce now
    drift = []
    for topic, count in counts.items():
        canonical, _ = canonicalize_topic(topic)
        if canonical and canonical != topic:
            drift.append((count, topic, canonical))

    if drift:
        print(f"DRIFT (would canonicalize differently if re-indexed): {len(drift)}")
        for count, old, new in sorted(drift, key=lambda x: -x[0])[:limit]:
            print(f"  {count:4d}x  {old!r}  ->  {new!r}")
        print()

    # Top by count
    print(f"TOP TOPICS by entry count (showing {limit}):")
    known_canonicals = set(TOPIC_TAG_MAP.keys()) | set(TOPIC_ALIASES.values())
    for topic, count in counts.most_common(limit):
        flag = ''
        if topic and topic not in known_canonicals and count >= 3:
            flag = '  [NEW: consider adding to TOPIC_ALIASES or TOPIC_TAG_MAP]'
        print(f"  {count:4d}  {topic!r}{flag}")
    print()

    # New vocab worth reviewing
    new_with_volume = [
        (topic, count) for topic, count in counts.items()
        if topic and topic not in known_canonicals and count >= 3
    ]
    if new_with_volume:
        print(f"NEW TOPICS with >= 3 entries ({len(new_with_volume)} total):")
        for topic, count in sorted(new_with_volume, key=lambda x: -x[1])[:limit]:
            print(f"  {count:4d}  {topic!r}")
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Inspect topic landscape')
    parser.add_argument('--limit', type=int, default=30, help='Max rows per section')
    args = parser.parse_args()
    sys.exit(main(args.limit))
