#!/usr/bin/env python3
"""
Index all learning markdown files into SQLite + sqlite-vec.
Generates a manifest summarizing learnings by topic for CLAUDE.md integration.
"""

import sys
import os

# Locate plugin root so lib/ is importable regardless of cwd
_PLUGIN_ROOT = os.environ.get('CLAUDE_PLUGIN_ROOT', os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _PLUGIN_ROOT)

import hashlib
import re
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, List, Tuple, Any

import lib.db as db
from lib.topic_mapping import infer_topic_from_tags


def _rglob_follow_symlinks(root: Path, target: str) -> List[Path]:
    """Like Path.rglob but follows symlinks (Path.rglob doesn't until Python 3.13)."""
    results = []
    target_parts = target.split('/')
    for dirpath, dirnames, _ in os.walk(str(root), followlinks=True):
        dirnames[:] = [d for d in dirnames if d not in
                       {'node_modules', '.git', '.venv', 'venv', '__pycache__', 'build', 'dist'}]
        p = Path(dirpath)
        if len(p.parts) >= len(target_parts):
            if list(p.parts[-len(target_parts):]) == target_parts:
                results.append(p)
    return results


def find_all_learning_files(config: Dict[str, Any]) -> Tuple[List[Path], Dict[str, List[Path]]]:
    """Find all learning files in global and repo locations."""
    global_dir = Path(config['learnings']['globalDir']).resolve()
    repo_search_path = Path(config['learnings']['repoSearchPath']).resolve()

    exclude_dirs = {'node_modules', '.git', '.venv', 'venv', '__pycache__', 'build', 'dist'}

    global_files = []
    repo_files_by_name: Dict[str, List[Path]] = {}
    seen_paths: set = set()

    # Global learnings
    if global_dir.exists():
        for f in global_dir.glob('**/*.md'):
            canonical = f.resolve()
            if canonical not in seen_paths and canonical.name != 'MANIFEST.md':
                seen_paths.add(canonical)
                global_files.append(canonical)

    # Repo learnings
    search_paths = {repo_search_path}
    if global_dir.parts[1:2] and global_dir.parts[1] != repo_search_path.parts[1]:
        alt_search = Path('/' + global_dir.parts[1])
        if alt_search.exists():
            search_paths.add(alt_search)

    for search_path in search_paths:
        if not search_path.exists():
            continue
        for learnings_dir in _rglob_follow_symlinks(search_path, '.projects/learnings'):
            learnings_dir = learnings_dir.resolve()
            if str(learnings_dir).startswith(str(global_dir)):
                continue
            if any(excluded in learnings_dir.parts for excluded in exclude_dirs):
                continue

            repo_name = learnings_dir.parent.parent.name
            for f in learnings_dir.glob('**/*.md'):
                canonical = f.resolve()
                if canonical not in seen_paths and canonical.name != 'MANIFEST.md':
                    seen_paths.add(canonical)
                    if repo_name not in repo_files_by_name:
                        repo_files_by_name[repo_name] = []
                    repo_files_by_name[repo_name].append(canonical)

    return global_files, repo_files_by_name


def extract_metadata_from_path(file_path: Path, config: Dict[str, Any]) -> Dict[str, str]:
    """Extract scope metadata from file path."""
    canonical_path = file_path.resolve()
    path_str = str(canonical_path)
    global_dir = str(Path(config['learnings']['globalDir']).resolve())

    if path_str.startswith(global_dir):
        return {"scope": "global", "repo": "", "file_path": path_str}

    try:
        repo_name = canonical_path.parent.parent.parent.name
        return {"scope": "repo", "repo": repo_name, "file_path": path_str}
    except Exception:
        return {"scope": "repo", "repo": "unknown", "file_path": path_str}


def extract_field(content: str, field: str) -> str | None:
    """Extract a **Field:** value from learning content."""
    match = re.search(rf'\*\*{field}:\*\*\s*(.+?)(?:\n|$)', content, re.IGNORECASE)
    return match.group(1).strip() if match else None


def extract_topic(content: str) -> str:
    """Extract topic from **Topic:** field, fallback to tag-based inference."""
    topic = extract_field(content, 'Topic')
    if topic:
        return topic.lower().replace(' ', '-')
    return infer_topic_from_tags(extract_tags(content))


def extract_tags(content: str) -> List[str]:
    """Extract tags from **Tags:** field."""
    tags_str = extract_field(content, 'Tags')
    if tags_str:
        return [t.strip().lower() for t in tags_str.split(',') if t.strip()][:8]
    return []


def extract_type(content: str) -> str | None:
    """Extract type from **Type:** field."""
    return extract_field(content, 'Type')


def generate_manifest(manifest_data: Dict[str, List[Dict[str, Any]]], config: Dict[str, Any]) -> None:
    """Generate single manifest file at ~/.projects/learnings/MANIFEST.md"""
    global_dir = Path(config['learnings']['globalDir'])
    global_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = global_dir / 'MANIFEST.md'

    lines = [
        "# Learnings Manifest",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        ""
    ]

    # Global section
    global_learnings = manifest_data.get('global', [])
    if global_learnings:
        lines.extend(_format_section("Global Learnings", global_learnings))

    # Repo sections
    for scope in sorted(manifest_data.keys()):
        if scope == 'global':
            continue
        lines.extend(_format_section(f"Repo: {scope}", manifest_data[scope]))

    with open(manifest_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"\n[OK] Generated manifest: {manifest_path}")


def _format_section(title: str, learnings: List[Dict[str, Any]]) -> List[str]:
    """Format a manifest section."""
    total = len(learnings)
    gotchas = sum(1 for l in learnings if l.get('is_gotcha'))

    lines = []
    if gotchas > 0:
        lines.append(f"## {title} ({total} total, {gotchas} gotchas)")
    else:
        lines.append(f"## {title} ({total} total)")
    lines.append("")

    # Aggregate by topic
    topics: Dict[str, Dict] = defaultdict(lambda: {'count': 0, 'keywords': defaultdict(int), 'gotchas': 0})

    for l in learnings:
        topic = l['topic']
        topics[topic]['count'] += 1
        if l.get('is_gotcha'):
            topics[topic]['gotchas'] += 1
        for kw in l.get('keywords', []):
            topics[topic]['keywords'][kw] += 1

    # Table sorted by count
    lines.append("| Topic | Count | Keywords |")
    lines.append("|-------|-------|----------|")

    for topic, data in sorted(topics.items(), key=lambda x: x[1]['count'], reverse=True):
        count = data['count']
        top_kw = sorted(data['keywords'].items(), key=lambda x: x[1], reverse=True)[:6]
        kw_str = ', '.join(k for k, _ in top_kw)
        count_str = f"{count} ({data['gotchas']}⚠️)" if data['gotchas'] else str(count)
        lines.append(f"| {topic} | {count_str} | {kw_str} |")

    lines.append("")
    return lines


def index_single_file(file_path: Path, config: Dict[str, Any]) -> bool:
    """Index a single learning file into SQLite. Returns True on success."""
    try:
        conn = db.get_connection(config)
    except Exception as e:
        print(f"[WARN] Failed to open database, skipping index: {e}")
        return False

    try:
        content = file_path.read_text(encoding='utf-8')
        metadata = extract_metadata_from_path(file_path, config)

        topic = extract_topic(content)
        keywords = extract_tags(content)

        metadata['topic'] = topic
        metadata['keywords'] = ','.join(keywords)

        doc_id = hashlib.md5(str(file_path.resolve()).encode()).hexdigest()
        db.upsert_document(conn, doc_id, content, metadata)
        print(f"[OK] Indexed: {file_path.name}")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to index {file_path.name}: {e}")
        return False
    finally:
        conn.close()


def index_learning_files():
    """Main indexing function."""
    print("Loading configuration...")
    config = db.load_config()

    print("Opening database...")
    try:
        conn = db.get_connection(config)
        print(f"[OK] Database ready")
    except Exception as e:
        print(f"[ERROR] Failed to open database: {e}")
        sys.exit(1)

    print("\nDiscovering learning files...")
    global_files, repo_files_by_name = find_all_learning_files(config)
    repo_files = [f for files in repo_files_by_name.values() for f in files]
    all_files = global_files + repo_files

    print(f"Found {len(all_files)} files (Global: {len(global_files)}, Repos: {len(repo_files)})")

    if not all_files:
        print("\nNo learning files found. Run /compound to create some!")
        conn.close()
        return

    print("\nIndexing...")
    manifest_data: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    indexed = 0

    for file_path in all_files:
        try:
            content = file_path.read_text(encoding='utf-8')
            metadata = extract_metadata_from_path(file_path, config)

            topic = extract_topic(content)
            keywords = extract_tags(content)
            learning_type = extract_type(content)

            metadata['topic'] = topic
            metadata['keywords'] = ','.join(keywords)

            doc_id = hashlib.md5(str(file_path.resolve()).encode()).hexdigest()
            db.upsert_document(conn, doc_id, content, metadata)

            scope_key = metadata['repo'] if metadata['scope'] == 'repo' else 'global'
            manifest_data[scope_key].append({
                'topic': topic,
                'keywords': keywords,
                'is_gotcha': learning_type and 'gotcha' in learning_type.lower(),
            })

            indexed += 1
            if indexed % 10 == 0:
                print(f"  {indexed}/{len(all_files)}...")

        except Exception as e:
            print(f"  [ERROR] {file_path.name}: {e}")

    # Prune orphaned entries whose files no longer exist on disk
    all_docs = db.get_all_documents(conn, include_content=False)
    pruned = 0
    for doc_id, metadata in zip(all_docs['ids'], all_docs['metadatas']):
        file_path_str = metadata.get('file_path', '')
        if file_path_str and not os.path.exists(file_path_str):
            db.delete_document(conn, doc_id)
            pruned += 1
    if pruned:
        print(f"[OK] Pruned {pruned} orphaned entries")
    else:
        print("[OK] No orphaned entries found")

    conn.close()
    print(f"\n[OK] Indexed {indexed} files")

    if manifest_data:
        generate_manifest(manifest_data, config)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Index learning files into SQLite')
    parser.add_argument('--file', metavar='PATH', help='Index a single file instead of all learnings')
    args = parser.parse_args()

    if args.file:
        config = db.load_config()
        file_path = Path(args.file).expanduser().resolve()
        if not file_path.exists():
            print(f"[ERROR] File not found: {file_path}")
            sys.exit(1)
        success = index_single_file(file_path, config)
        sys.exit(0 if success else 1)
    else:
        index_learning_files()
