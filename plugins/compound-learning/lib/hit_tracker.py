#!/usr/bin/env python3
"""Track hit counts for accessed learnings in both SQLite and markdown files."""

import os
import re
import sys
from datetime import date

_PLUGIN_ROOT = os.environ.get('CLAUDE_PLUGIN_ROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _PLUGIN_ROOT)

import lib.db as db


def record_hits(config: dict, results: list[dict], today: str | None = None) -> None:
    """Record hit counts for search results in SQLite and learning files.

    Args:
        config: Plugin configuration dict (passed to db.get_connection).
        results: List of search result dicts with 'id' and 'metadata.file_path'.
        today: Date string YYYY-MM-DD; defaults to current date.
    """
    if today is None:
        today = date.today().isoformat()

    conn = db.get_connection(config)
    try:
        for result in results:
            file_path = result.get('metadata', {}).get('file_path', '')
            if not file_path:
                continue

            doc_id = result['id']

            try:
                db.increment_hit_count(conn, doc_id, today)
            except Exception as e:
                print(f"[WARN] DB hit increment failed for {doc_id}: {e}", file=sys.stderr)

            try:
                _update_file_hits(file_path, today)
            except Exception as e:
                print(f"[WARN] File hit update failed for {file_path}: {e}", file=sys.stderr)
    finally:
        conn.close()


def _update_file_hits(file_path: str, today: str) -> None:
    """Update **Hits:** and **Last Accessed:** fields in a learning markdown file."""
    if not os.path.isfile(file_path):
        return

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    content = _update_or_insert_hits(content)
    content = _update_or_insert_last_accessed(content, today)

    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)


def _update_or_insert_hits(content: str) -> str:
    """Increment **Hits:** N by 1, or insert **Hits:** 1 after the last **Field:** line."""
    hits_pattern = re.compile(r'^(\*\*Hits:\*\*\s*)(\d+)', re.MULTILINE)
    match = hits_pattern.search(content)
    if match:
        new_count = int(match.group(2)) + 1
        return hits_pattern.sub(rf'\g<1>{new_count}', content, count=1)

    return _insert_field_after_last_metadata(content, '**Hits:** 1')


def _update_or_insert_last_accessed(content: str, today: str) -> str:
    """Update **Last Accessed:** date, or insert it after the last **Field:** line."""
    la_pattern = re.compile(r'^(\*\*Last Accessed:\*\*\s*).*$', re.MULTILINE)
    match = la_pattern.search(content)
    if match:
        return la_pattern.sub(rf'\g<1>{today}', content, count=1)

    return _insert_field_after_last_metadata(content, f'**Last Accessed:** {today}')


def _insert_field_after_last_metadata(content: str, field_line: str) -> str:
    """Insert a field line after the last **Key:** value line in the frontmatter block."""
    field_pattern = re.compile(r'^\*\*\w[\w\s]*:\*\*.*$', re.MULTILINE)
    last_match = None
    for m in field_pattern.finditer(content):
        last_match = m

    if last_match:
        insert_pos = last_match.end()
        return content[:insert_pos] + '\n' + field_line + content[insert_pos:]

    return content
