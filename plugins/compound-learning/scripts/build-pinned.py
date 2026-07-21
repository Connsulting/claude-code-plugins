#!/usr/bin/env python3
"""
Generate ~/.claude/plugins/compound-learning/pinned.md from learnings with
MEASURED SUBSTANTIVE USE.

Selection rule (read this before changing anything):
  A learning is pinned because a transcript shows the model actually engaged
  with it after it was injected -- not because the embedding matched often.
  The evidence lives in the `peek_usefulness` table, which analyze-peeks.py
  writes with `--persist` by scoring each auto-peek injection against the
  assistant reply that followed it.

  `access_count` is deliberately NOT a selection input. It is incremented at
  injection time by lib/hit_tracker.py, before the model has read anything, so
  it measures embedding recall only. Worse, until 2026-07-15 a pinned entry was
  also auto-peeked, so pinning inflated the very counter that justified the pin.

  peek_usefulness is FULL-REPLACED on every `analyze-peeks.py --persist` run over
  a rolling window, so a learning that stops earning substantive use drops out of
  the table and loses its slot. Rank cannot freeze.

The output file is injected into every session (pinned.md is imported by the
user's CLAUDE.md and read by auto-peek.sh). It is hard-capped by a token budget
-- every entry costs tokens on every single session start.
"""
import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

_PLUGIN_ROOT = os.environ.get(
    'CLAUDE_PLUGIN_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

import lib._site_packages  # noqa: F401  -- ensures site-packages on sys.path before third-party imports

import lib.db as db


DEFAULT_OUTPUT = Path.home() / '.claude' / 'plugins' / 'compound-learning' / 'pinned.md'
DEFAULT_CHANGELOG = Path(_PLUGIN_ROOT) / 'pinned-changes.log'

HEADER = [
    "These learnings are pinned because transcripts show they were actually used,",
    "not because they were retrieved often. Apply when relevant; one-line",
    "acknowledgment if applied.",
    "",
]


def est_tokens(text: str) -> int:
    """4-chars-per-token rule of thumb, same estimate analyze-peeks.py uses."""
    return len(text) // 4


MERGE_NOTE_RE = re.compile(r'^\*Merged from \d+ learnings on [\d-]+\*$')
SOURCE_HEADER_RE = re.compile(r'^#+\s*Source:\s')
HEADING_RE = re.compile(r'^(#{1,5})(\s+\S)')


def clean_body(content: str, max_tokens: int, source: str) -> str:
    """Strip metadata lines, collapse blank runs, truncate to a token budget."""
    body = content.split('\n', 1)[1] if '\n' in content else ''
    cleaned = []
    in_code = False
    for line in body.split('\n'):
        stripped = line.strip()
        if stripped.startswith('```'):
            in_code = not in_code
            cleaned.append(line)
            continue
        if not in_code:
            # Drop **Field:** metadata (Type/Topic/Tags/Hits/Last Accessed) -- indexing
            # signal, not actionable rule content.
            if stripped.startswith('**') and ':**' in stripped[:40]:
                continue
            # Drop consolidation bookkeeping left by merge runs.
            if MERGE_NOTE_RE.match(stripped) or SOURCE_HEADER_RE.match(stripped) or stripped == '---':
                continue
        cleaned.append(line)

    # Re-level inner headings so the shallowest becomes ### -- they must sit under
    # the entry's own ## header and never masquerade as a pinned entry.
    depths = [len(m.group(1)) for line in cleaned
              for m in [HEADING_RE.match(line.strip())] if m]
    if depths:
        shift = 3 - min(depths)
        if shift:
            releveled = []
            in_code = False
            for line in cleaned:
                if line.strip().startswith('```'):
                    in_code = not in_code
                m = HEADING_RE.match(line.strip()) if not in_code else None
                if m:
                    depth = max(3, min(6, len(m.group(1)) + shift))
                    line = '#' * depth + line.strip()[len(m.group(1)):]
                releveled.append(line)
            cleaned = releveled

    compact = []
    prev_blank = False
    for line in cleaned:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        compact.append(line)
        prev_blank = is_blank

    text = '\n'.join(compact).strip()
    if est_tokens(text) <= max_tokens:
        return text

    # Truncate on a paragraph boundary so we never cut mid-sentence.
    kept: List[str] = []
    for para in text.split('\n\n'):
        candidate = '\n\n'.join(kept + [para])
        if kept and est_tokens(candidate) > max_tokens:
            break
        kept.append(para)
    # Drop a trailing paragraph that is only a heading with nothing under it.
    while kept and kept[-1].strip().startswith('#'):
        kept.pop()
    if len(kept) <= 1 and (not kept or est_tokens(kept[0]) > max_tokens):
        # A single paragraph longer than the whole cap: hard-slice it on a word
        # boundary. Without this the cap is silently unenforced for one-paragraph
        # learnings, and the total budget can be blown by a single entry.
        words = text.split()
        sliced: List[str] = []
        for word in words:
            if sliced and est_tokens(' '.join(sliced + [word])) > max_tokens:
                break
            sliced.append(word)
        kept = [' '.join(sliced)]
    truncated = '\n\n'.join(kept).strip()
    return f"{truncated}\n\n_(truncated; full text in {source})_"


def parse_current_members(output: Path) -> List[str]:
    """Read the `_source: <file>...` lines out of an existing pinned.md."""
    if not output.exists():
        return []
    members = []
    for line in output.read_text(encoding='utf-8').splitlines():
        s = line.strip()
        if s.startswith('_source:'):
            rest = s[len('_source:'):].strip().lstrip('_').strip()
            members.append(rest.split()[0].rstrip('_').strip() if rest else '')
    return [m for m in members if m]


def select(
    conn,
    min_substantive: int,
    max_entries: int,
    denylist: Set[str],
) -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    """Rank global learnings by measured usefulness.

    Returns (selected, rejection_notes, next_contenders).
    """
    usefulness = {
        row['file_name']: row
        for row in conn.execute(
            "SELECT file_name, injections, substantive, last_substantive, window_days "
            "FROM peek_usefulness"
        ).fetchall()
    }
    if not usefulness:
        return [], ["peek_usefulness is empty -- run: analyze-peeks.py <days> --persist"], []

    learnings = conn.execute(
        "SELECT id, content, file_path, topic, access_count FROM learnings "
        "WHERE scope = 'global'"
    ).fetchall()

    candidates: List[Dict[str, Any]] = []
    rejected: List[str] = []
    for row in learnings:
        fname = os.path.basename(row['file_path'])
        use = usefulness.get(fname)
        if use is None:
            continue
        if fname in denylist:
            rejected.append(f"{fname}: on the pinned denylist (known stale)")
            continue
        if use['substantive'] < min_substantive:
            continue
        candidates.append({
            'row': row,
            'file_name': fname,
            'substantive': use['substantive'],
            'injections': use['injections'],
            'rate': use['substantive'] / max(use['injections'], 1),
            'last_substantive': use['last_substantive'],
            'window_days': use['window_days'],
        })

    candidates.sort(
        key=lambda c: (c['substantive'], c['rate'], -c['injections']),
        reverse=True,
    )
    return candidates[:max_entries], rejected, candidates[max_entries:max_entries + 3]


def build_pinned(args: argparse.Namespace) -> int:
    config = db.load_config()
    pin_cfg = config.get('pinned', {})
    max_entries = args.max_entries if args.max_entries is not None else pin_cfg.get('maxEntries', 6)
    token_budget = args.token_budget if args.token_budget is not None else pin_cfg.get('tokenBudget', 1400)
    max_entry_tokens = pin_cfg.get('maxEntryTokens', 320)
    min_substantive = (args.min_substantive if args.min_substantive is not None
                       else pin_cfg.get('minSubstantive', 1))
    denylist = set(pin_cfg.get('denylist', []))

    conn = db.get_connection(config)
    had_evidence = bool(
        conn.execute("SELECT 1 FROM peek_usefulness LIMIT 1").fetchone()
    )
    selected, rejected, contenders = select(conn, min_substantive, max_entries, denylist)
    conn.close()

    if not selected:
        # No evidence at all (analyze-peeks never ran, or its DB write failed) is a
        # broken measurement, not a verdict -- keep the previous file. But fresh
        # evidence in which nothing qualifies IS a verdict: clear the pins, or a
        # stale entry keeps its slot until some replacement happens along.
        if had_evidence and not args.dry_run:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text('\n'.join(HEADER).rstrip() + '\n', encoding='utf-8')
            print("Fresh evidence, but no learning met the usefulness bar; "
                  f"cleared {args.output}.")
        else:
            print("No usefulness evidence; leaving pinned.md untouched "
                  "(run analyze-peeks.py <days> --persist first).")
        for note in rejected:
            print(f"  rejected: {note}")
        return 0

    lines = list(HEADER)
    included: List[Dict[str, Any]] = []
    for cand in selected:
        row = cand['row']
        title = row['content'].split('\n', 1)[0].lstrip('# ').strip()
        entry = [
            f"## {title}",
            f"_source: {cand['file_name']} "
            f"({cand['substantive']} substantive uses / {cand['injections']} injections, "
            f"{cand['window_days']}d window)_",
            "",
            clean_body(row['content'], max_entry_tokens, cand['file_name']),
            "",
        ]
        projected = '\n'.join(lines + entry)
        if included and est_tokens(projected) > token_budget:
            break
        lines = lines + entry
        included.append(cand)

    body = '\n'.join(lines)
    previous = parse_current_members(args.output)
    now_members = [c['file_name'] for c in included]
    promoted = [m for m in now_members if m not in previous]
    dropped = [m for m in previous if m not in now_members]

    report = [
        f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] build-pinned",
        f"  entries: {len(included)} (cap {max_entries}), "
        f"~{est_tokens(body)} tokens (budget {token_budget})",
    ]
    for c in included:
        report.append(
            f"  in      : {c['file_name']}  substantive={c['substantive']} "
            f"injections={c['injections']} rate={c['rate']:.1%} "
            f"last={c['last_substantive'] or '-'}"
        )
    for m in promoted:
        report.append(f"  PROMOTED: {m}  (earned substantive use in window)")
    for m in dropped:
        reason = "on denylist" if m in denylist else "no substantive use in window"
        report.append(f"  DROPPED : {m}  ({reason})")
    for note in rejected:
        report.append(f"  REJECTED: {note}")
    for c in contenders:
        report.append(
            f"  contender (below cutoff): {c['file_name']} substantive={c['substantive']}"
        )

    if args.dry_run:
        proposed = args.output.parent / (args.output.name + '.proposed')
        proposed.parent.mkdir(parents=True, exist_ok=True)
        proposed.write_text(body, encoding='utf-8')
        report.append(f"  DRY RUN: proposal written to {proposed}; pinned.md untouched")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding='utf-8')
        report.append(f"  wrote {args.output} ({args.output.stat().st_size} bytes)")

    text = '\n'.join(report)
    print(text)
    try:
        args.changelog.parent.mkdir(parents=True, exist_ok=True)
        with args.changelog.open('a', encoding='utf-8') as f:
            f.write(text + '\n')
    except OSError as e:
        print(f"[WARN] could not append to {args.changelog}: {e}", file=sys.stderr)

    return len(included)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Build pinned.md from measured substantive use (peek_usefulness)')
    parser.add_argument('--max-entries', type=int, default=None,
                        help='Hard cap on entries (default: config pinned.maxEntries)')
    parser.add_argument('--token-budget', type=int, default=None,
                        help='Hard cap on total pinned.md tokens (default: config pinned.tokenBudget)')
    parser.add_argument('--min-substantive', type=int, default=None,
                        help='Minimum substantive uses to be eligible (default: config pinned.minSubstantive)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Write pinned.md.proposed instead of pinned.md')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT,
                        help=f'Output path (default: {DEFAULT_OUTPUT})')
    parser.add_argument('--changelog', type=Path, default=DEFAULT_CHANGELOG,
                        help=f'Append the change report here (default: {DEFAULT_CHANGELOG})')
    args = parser.parse_args()
    sys.exit(0 if build_pinned(args) >= 0 else 1)
