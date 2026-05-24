#!/usr/bin/env python3
"""
Autonomous Sunday consolidation pass.

Conservative auto-merge criteria (no human in the loop):
  - Same canonicalized topic
  - Same H1 (case-insensitive, slug-compared)
  - All pairwise cosine similarities in the cluster >= AUTO_MERGE_COSINE_THRESHOLD

Lower-confidence clusters (cosine >= REVIEW_FLAG_COSINE_THRESHOLD but failing
the auto-merge gate) get appended to a review queue file for periodic human
attention. Nothing is destructive without --apply.

Designed to run weekly from cron. Idempotent: re-running surfaces only
clusters that still exist after prior merges.
"""

import argparse
import importlib.util
import json
import os
import struct
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_PLUGIN_ROOT = os.environ.get(
    'CLAUDE_PLUGIN_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

import lib.db as db
from lib.topic_mapping import canonicalize_topic


# Auto-merge requires near-identical content with consistent headlines.
# Conservative on purpose: false-positive merges in autonomous mode are
# harder to undo than queuing a borderline pair for human review.
AUTO_MERGE_COSINE_THRESHOLD = 0.92
REVIEW_FLAG_COSINE_THRESHOLD = 0.85
MAX_CLUSTER_SIZE_AUTO_MERGE = 5  # safety cap; bigger clusters always go to review

DEFAULT_REVIEW_FILE = Path.home() / '.claude' / 'plugins' / 'compound-learning' / 'review-queue.md'
DEFAULT_LOG_FILE = Path.home() / '.claude' / 'plugins' / 'compound-learning' / 'auto-consolidate.log'


def _load_action_merge():
    """Import action_merge from the consolidate-actions skill (hyphenated dir)."""
    path = Path(_PLUGIN_ROOT) / 'skills' / 'consolidate-actions' / 'consolidate-actions.py'
    spec = importlib.util.spec_from_file_location("consolidate_actions", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load consolidate-actions from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.action_merge


def _normalize_h1(content: str) -> str:
    """Extract the H1 (or YAML frontmatter `name:` field), lowercase, strip.

    Handles two file shapes:
    - Markdown-first: first non-empty line starts with `# `, use it
    - YAML frontmatter: starts with `---`, find `name:` field or first `# ` after the closing `---`
    Returns '' if neither pattern matches (which prevents accidental same-H1 matches
    on files that have no real headline, e.g. two frontmatter-only stubs).
    """
    lines = content.split('\n', 50)
    if not lines:
        return ''

    if lines[0].strip() == '---':
        # YAML frontmatter: look for name: inside, or first '# ' line after closing ---
        end_idx = None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == '---':
                end_idx = i
                break
            if line.lstrip().lower().startswith('name:'):
                return line.split(':', 1)[1].strip().lower()
        if end_idx is not None:
            for line in lines[end_idx + 1:]:
                if line.startswith('# '):
                    return line[2:].strip().lower()
        return ''

    for line in lines:
        if line.startswith('# '):
            return line[2:].strip().lower()
        if line.strip() and not line.startswith('#'):
            # No H1 found; use first prose line as a weak signal
            return line.strip().lower()
    return ''


def _slug_h1_for_name(h1: str) -> str:
    """Truncated slug usable as a merged-file name."""
    import re
    s = re.sub(r'[^a-z0-9]+', '-', h1.lower()).strip('-')
    return s[:50] or 'merged-learning'


def find_clusters(conn) -> list[dict]:
    """Load global-scope embeddings, build similarity matrix, return clusters with metadata."""
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError("numpy is required: pip install numpy")

    rows = conn.execute(
        """
        SELECT l.id, l.file_path, l.content, l.topic, v.embedding
        FROM learnings l JOIN vec_learnings v ON l.id = v.id
        WHERE l.scope = 'global'
        """
    ).fetchall()

    if not rows:
        return []

    embs = []
    meta = []
    for r in rows:
        e = struct.unpack(f'{384}f', r['embedding'])
        embs.append(e)
        topic_canonical, _ = canonicalize_topic(r['topic'] or '')
        meta.append({
            'id': r['id'],
            'file_path': r['file_path'],
            'file': os.path.basename(r['file_path']),
            'topic_raw': r['topic'] or '',
            'topic_canonical': topic_canonical,
            'h1': _normalize_h1(r['content']),
            'content_len': len(r['content']),
        })

    M = np.array(embs, dtype=np.float32)
    # Embeddings come from sentence-transformers normalize=True so dot = cosine
    sim = M @ M.T

    # Union-find over pairs above the review threshold
    n = len(M)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if sim[i, j] >= REVIEW_FLAG_COSINE_THRESHOLD:
                union(i, j)

    cluster_idxs = defaultdict(list)
    for i in range(n):
        cluster_idxs[find(i)].append(i)

    clusters = []
    for indices in cluster_idxs.values():
        if len(indices) < 2:
            continue
        # Compute pairwise sim stats within this cluster
        pair_sims = []
        for a_i, a in enumerate(indices):
            for b in indices[a_i + 1:]:
                pair_sims.append(float(sim[a, b]))
        min_sim = min(pair_sims) if pair_sims else 1.0
        avg_sim = sum(pair_sims) / len(pair_sims) if pair_sims else 1.0
        clusters.append({
            'members': [meta[i] for i in indices],
            'size': len(indices),
            'min_sim': round(min_sim, 4),
            'avg_sim': round(avg_sim, 4),
        })

    clusters.sort(key=lambda c: (-c['size'], -c['avg_sim']))
    return clusters


def classify_cluster(cluster: dict) -> tuple[str, str]:
    """Decide AUTO / REVIEW for a cluster. Returns (decision, reason)."""
    if cluster['size'] > MAX_CLUSTER_SIZE_AUTO_MERGE:
        return ('REVIEW', f"cluster size {cluster['size']} > auto-merge cap {MAX_CLUSTER_SIZE_AUTO_MERGE}")

    topics = {m['topic_canonical'] for m in cluster['members']}
    if len(topics) > 1:
        return ('REVIEW', f"topics differ after canonicalization: {sorted(topics)}")

    h1s = {m['h1'] for m in cluster['members']}
    if len(h1s) > 1:
        return ('REVIEW', f"H1 headings differ: {sorted(h1s)[:3]}")

    if cluster['min_sim'] < AUTO_MERGE_COSINE_THRESHOLD:
        return ('REVIEW', f"min pairwise sim {cluster['min_sim']} < auto-merge threshold {AUTO_MERGE_COSINE_THRESHOLD}")

    return ('AUTO', f"same topic={list(topics)[0]!r}, same H1, min_sim={cluster['min_sim']}")


def write_review_queue(review_clusters: list[tuple[dict, str]], path: Path) -> None:
    """Append review-queue clusters to a markdown file the human can scan periodically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with path.open('a', encoding='utf-8') as f:
        f.write(f"\n## Review pass {timestamp}\n\n")
        if not review_clusters:
            f.write("(no clusters needed review)\n\n")
            return
        for cluster, reason in review_clusters:
            f.write(f"### Cluster ({cluster['size']} files, avg_sim={cluster['avg_sim']})\n")
            f.write(f"_reason: {reason}_\n\n")
            for m in cluster['members']:
                f.write(f"- `{m['file']}` (topic={m['topic_canonical']!r})\n")
            f.write("\n")


def main(apply: bool, review_file: Path, log_file: Path, verbose: bool) -> int:
    config = db.load_config()
    conn = db.get_connection(config)
    clusters = find_clusters(conn)

    auto_clusters: list[dict] = []
    review_clusters: list[tuple[dict, str]] = []
    for cluster in clusters:
        decision, reason = classify_cluster(cluster)
        if decision == 'AUTO':
            cluster['_reason'] = reason
            auto_clusters.append(cluster)
        else:
            review_clusters.append((cluster, reason))

    summary: dict = {
        'timestamp': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'apply': apply,
        'clusters_total': len(clusters),
        'auto_candidates': len(auto_clusters),
        'review_candidates': len(review_clusters),
        'auto_results': [],
        'errors': [],
    }

    if verbose:
        print(json.dumps({'phase': 'discovery', **summary}, indent=2))

    # Execute or dry-run the auto-merges
    action_merge = _load_action_merge()
    for cluster in auto_clusters:
        ids = [m['id'] for m in cluster['members']]
        name = _slug_h1_for_name(cluster['members'][0]['h1'])
        if apply:
            result = action_merge(ids, name, config, dry_run=False)
        else:
            result = action_merge(ids, name, config, dry_run=True)
        summary['auto_results'].append({
            'name': name,
            'cluster_size': cluster['size'],
            'min_sim': cluster['min_sim'],
            'status': result.get('status'),
            'merged_path': result.get('merged_path') or result.get('would_create'),
            'reason': cluster['_reason'],
        })
        if result.get('status') == 'error':
            summary['errors'].append({'name': name, 'error': result.get('message')})

    conn.close()

    # Always write review queue (even in dry-run, so user can see what's queued)
    write_review_queue(review_clusters, review_file)
    summary['review_file'] = str(review_file)

    # Append a one-line digest to the log file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open('a', encoding='utf-8') as f:
        f.write(f"{summary['timestamp']} mode={'apply' if apply else 'dry-run'} "
                f"clusters={summary['clusters_total']} auto={summary['auto_candidates']} "
                f"review={summary['review_candidates']} errors={len(summary['errors'])}\n")

    print(json.dumps(summary, indent=2))
    return 0 if not summary['errors'] else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Autonomous consolidation pass')
    parser.add_argument('--apply', action='store_true',
                        help='Actually perform merges. Default is dry-run.')
    parser.add_argument('--review-file', type=Path, default=DEFAULT_REVIEW_FILE,
                        help=f'Review queue output (default: {DEFAULT_REVIEW_FILE})')
    parser.add_argument('--log-file', type=Path, default=DEFAULT_LOG_FILE,
                        help=f'Run log (default: {DEFAULT_LOG_FILE})')
    parser.add_argument('--verbose', action='store_true',
                        help='Print discovery summary before executing')
    args = parser.parse_args()
    sys.exit(main(args.apply, args.review_file, args.log_file, args.verbose))
