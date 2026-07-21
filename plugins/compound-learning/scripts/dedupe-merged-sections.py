#!/usr/bin/env python3
"""
Remove near-verbatim duplicate sections inside merged learning files.

`consolidate-actions.py merge` is deliberately non-lossy: it concatenates each
source under a `## Source: <file>` header. When the merged sources were already
restatements of the same fact, the result is one file that says the same thing
two or three times -- pure context cost with no added signal.

This script finds those repeats and keeps the longest representative of each
near-duplicate group. Similarity is prose-Jaccard with fenced code blocks
stripped, because the restatements usually differ in their code sample while the
prose is the same claim. The default threshold (0.6) was calibrated against this
corpus: at 0.6 the flagged pairs are verbatim rewordings; below ~0.5 genuinely
complementary sections start being flagged, so those are reported, not touched.

Run:
  python3 dedupe-merged-sections.py --dry-run          # report only
  python3 dedupe-merged-sections.py --apply            # rewrite + reindex
  python3 dedupe-merged-sections.py --dry-run --report-threshold 0.4
"""
import argparse
import os
import re
import sys
from pathlib import Path
from typing import List

_PLUGIN_ROOT = os.environ.get(
    'CLAUDE_PLUGIN_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

import lib._site_packages  # noqa: F401

import lib.db as db

SOURCE_SPLIT_RE = re.compile(r'(?m)^(## Source:.*)$')
CODE_FENCE_RE = re.compile(r'(?s)```.*?```')


def prose_tokens(text: str) -> List[str]:
    text = CODE_FENCE_RE.sub(' ', text)
    text = re.sub(r'[^A-Za-z0-9 ]', ' ', text.lower())
    return [w for w in text.split() if len(w) > 2]


def jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def split_sections(content: str):
    """Return (preamble, [(header, body), ...]) for a merged file."""
    parts = SOURCE_SPLIT_RE.split(content)
    if len(parts) < 3:
        return content, []
    preamble = parts[0]
    sections = [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
    return preamble, sections


def dedupe(sections, threshold: float):
    """Keep the longest member of each near-duplicate group."""
    kept: list = []
    dropped: list = []
    for header, body in sections:
        toks = prose_tokens(body)
        match_idx = None
        for i, (_kh, kb, ktoks) in enumerate(kept):
            if jaccard(toks, ktoks) >= threshold:
                match_idx = i
                break
        if match_idx is None:
            kept.append((header, body, toks))
            continue
        # Keep whichever restatement is longer; drop the other.
        kh, kb, ktoks = kept[match_idx]
        if len(body) > len(kb):
            kept[match_idx] = (header, body, toks)
            dropped.append((kh, kb))
        else:
            dropped.append((header, body))
    return [(h, b) for h, b, _ in kept], dropped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--threshold', type=float, default=0.6,
                        help='Prose-Jaccard at or above which sections are duplicates (default: 0.6)')
    parser.add_argument('--report-threshold', type=float, default=0.4,
                        help='Also report (never touch) pairs at or above this (default: 0.4)')
    parser.add_argument('--apply', action='store_true', help='Rewrite files and reindex')
    parser.add_argument('--dry-run', action='store_true', help='Report only (default)')
    args = parser.parse_args()
    apply_changes = args.apply and not args.dry_run

    config = db.load_config()
    conn = db.get_connection(config)
    rows = conn.execute(
        "SELECT id, file_path, content, scope, repo, topic, keywords, created_at, "
        "access_count, last_accessed FROM learnings WHERE content LIKE '%## Source:%'"
    ).fetchall()

    changed = 0
    sections_removed = 0
    flagged_only = 0
    for row in rows:
        preamble, sections = split_sections(row['content'])
        if len(sections) < 2:
            continue
        kept, dropped = dedupe(sections, args.threshold)
        if dropped:
            changed += 1
            sections_removed += len(dropped)
            print(f"dedupe {os.path.basename(row['file_path'])}: "
                  f"{len(sections)} sections -> {len(kept)} "
                  f"(dropped {len(dropped)})")
            if apply_changes:
                new_content = preamble + ''.join(h + b for h, b in kept)
                new_content = re.sub(r'\n{3,}', '\n\n', new_content).rstrip() + '\n'
                path = Path(row['file_path'])
                if path.exists():
                    path.write_text(new_content, encoding='utf-8')
                db.upsert_document(conn, row['id'], new_content, {
                    'scope': row['scope'], 'repo': row['repo'],
                    'file_path': row['file_path'], 'topic': row['topic'],
                    'keywords': row['keywords'], 'created_at': row['created_at'],
                    'access_count': row['access_count'],
                    'last_accessed': row['last_accessed'],
                })
        else:
            # Softer band: same claim restated but not mechanically safe to cut.
            toks = [prose_tokens(b) for _h, b in sections]
            if any(jaccard(toks[i], toks[j]) >= args.report_threshold
                   for i in range(len(toks)) for j in range(i + 1, len(toks))):
                flagged_only += 1
                print(f"flag   {os.path.basename(row['file_path'])}: "
                      f"overlapping sections in the "
                      f"{args.report_threshold}-{args.threshold} band (human judgment)")

    conn.close()
    mode = 'APPLIED' if apply_changes else 'DRY RUN'
    print(f"\n{mode}: merged files scanned={len(rows)} "
          f"deduped={changed} sections_removed={sections_removed} "
          f"flagged_for_human={flagged_only}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
