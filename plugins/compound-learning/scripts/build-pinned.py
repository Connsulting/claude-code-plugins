#!/usr/bin/env python3
"""
Generate ~/.claude/plugins/compound-learning/pinned.md from the top-N
most-hit learnings. Intended to run on a schedule (cron) so the pinned set
reflects current usage.

The output file is injected into the conversation context by auto-peek.sh
once per session. Keep the file small: every entry costs tokens on every
session start.
"""
import argparse
import os
import sys
from pathlib import Path

_PLUGIN_ROOT = os.environ.get(
    'CLAUDE_PLUGIN_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

import lib._site_packages  # noqa: F401  -- ensures site-packages on sys.path before third-party imports

import lib.db as db


DEFAULT_OUTPUT = Path.home() / '.claude' / 'plugins' / 'compound-learning' / 'pinned.md'
DEFAULT_TOP_N = 10
DEFAULT_MIN_HITS = 5


def build_pinned(top_n: int, min_hits: int, output: Path) -> int:
    config = db.load_config()
    conn = db.get_connection(config)

    rows = conn.execute(
        """
        SELECT id, content, file_path, topic, access_count, last_accessed
        FROM learnings
        WHERE scope = 'global'
          AND access_count >= ?
        ORDER BY access_count DESC, last_accessed DESC
        LIMIT ?
        """,
        (min_hits, top_n),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No learnings with access_count >= {min_hits}; not writing pinned.md")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "These learnings are pinned because they have been retrieved frequently.",
        "Apply when relevant; one-line acknowledgment if applied.",
        "",
    ]
    for row in rows:
        title = row['content'].split('\n', 1)[0].lstrip('# ').strip()
        fname = os.path.basename(row['file_path'])
        lines.append(f"## {title}  ({row['access_count']} hits)")
        lines.append(f"_source: {fname}_")
        lines.append("")
        body = row['content'].split('\n', 1)[1].strip() if '\n' in row['content'] else ''
        # Strip the **Field:** metadata lines (Type/Topic/Tags/Hits/Last Accessed
        # etc) — they're indexing signal, not actionable rule content.
        cleaned = []
        for line in body.split('\n'):
            stripped = line.lstrip()
            if stripped.startswith('**') and ':**' in stripped[:40]:
                continue
            cleaned.append(line)
        # Collapse runs of blank lines from removed metadata
        compact = []
        prev_blank = False
        for line in cleaned:
            is_blank = not line.strip()
            if is_blank and prev_blank:
                continue
            compact.append(line)
            prev_blank = is_blank
        lines.append('\n'.join(compact).strip())
        lines.append("")

    output.write_text('\n'.join(lines), encoding='utf-8')
    print(f"Wrote {len(rows)} pinned learnings ({output.stat().st_size} bytes) to {output}")
    return len(rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Build pinned.md from top-hit learnings')
    parser.add_argument('--top-n', type=int, default=DEFAULT_TOP_N,
                        help=f'Max entries to include (default: {DEFAULT_TOP_N})')
    parser.add_argument('--min-hits', type=int, default=DEFAULT_MIN_HITS,
                        help=f'Minimum access_count to be eligible (default: {DEFAULT_MIN_HITS})')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT,
                        help=f'Output path (default: {DEFAULT_OUTPUT})')
    args = parser.parse_args()
    sys.exit(0 if build_pinned(args.top_n, args.min_hits, args.output) >= 0 else 1)
