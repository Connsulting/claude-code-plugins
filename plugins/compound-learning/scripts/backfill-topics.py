#!/usr/bin/env python3
"""
Backfill missing **Topic:** fields in learning markdown files.

Scans ~/.projects/learnings/*.md for files that lack a **Topic:** line,
infers the topic from existing **Tags:**, then either prints the proposed
changes (--dry-run, the default) or writes them to disk (--apply).
"""

from __future__ import annotations

import argparse
import re
import sys
import os
from pathlib import Path

# Locate plugin root so lib/ is importable regardless of cwd
_PLUGIN_ROOT = os.environ.get(
    "CLAUDE_PLUGIN_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

from lib.topic_mapping import infer_topic_from_tags


def extract_field(content: str, field: str) -> str | None:
    """Extract a **Field:** value from learning content.

    Mirrors the pattern used in index-learnings.py for consistency.
    """
    match = re.search(rf"\*\*{field}:\*\*\s*(.+?)(?:\n|$)", content, re.IGNORECASE)
    return match.group(1).strip() if match else None


def extract_tags(content: str) -> list[str]:
    """Extract and normalise tags from **Tags:** field."""
    tags_str = extract_field(content, "Tags")
    if not tags_str:
        return []
    # Strip surrounding brackets that appear in some files, e.g. [tag1, tag2]
    tags_str = tags_str.strip("[]")
    return [t.strip().lower() for t in tags_str.split(",") if t.strip()][:8]


def insert_topic_line(content: str, topic: str) -> str:
    """Insert **Topic:** <topic> into content.

    Inserts after the **Type:** line when present; otherwise after the
    first heading (title line starting with #).
    """
    topic_line = f"**Topic:** {topic}"

    # Try inserting after **Type:** line
    type_match = re.search(r"(\*\*Type:\*\*[^\n]*\n)", content, re.IGNORECASE)
    if type_match:
        insert_at = type_match.end()
        return content[:insert_at] + topic_line + "\n" + content[insert_at:]

    # Fall back: insert after the first # heading line
    heading_match = re.search(r"(#[^\n]*\n)", content)
    if heading_match:
        insert_at = heading_match.end()
        return content[:insert_at] + topic_line + "\n" + content[insert_at:]

    # Last resort: prepend
    return topic_line + "\n" + content


def process_file(
    file_path: Path, dry_run: bool
) -> tuple[bool, str | None]:
    """Process a single file.

    Returns (changed, topic) where changed=True means a topic was added.
    """
    content = file_path.read_text(encoding="utf-8")

    if extract_field(content, "Topic"):
        return False, None

    tags = extract_tags(content)
    topic = infer_topic_from_tags(tags)
    new_content = insert_topic_line(content, topic)

    if dry_run:
        return True, topic

    file_path.write_text(new_content, encoding="utf-8")
    return True, topic


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill missing **Topic:** fields in learning files."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print proposed changes without writing (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write changes to disk",
    )
    parser.add_argument(
        "--dir",
        metavar="PATH",
        default="~/.projects/learnings",
        help="Directory to scan (default: ~/.projects/learnings)",
    )
    args = parser.parse_args()

    dry_run = not args.apply
    learnings_dir = Path(args.dir).expanduser().resolve()

    if not learnings_dir.exists():
        print(f"[ERROR] Directory not found: {learnings_dir}")
        sys.exit(1)

    files = sorted(
        f for f in learnings_dir.glob("*.md") if f.name != "MANIFEST.md"
    )

    if not files:
        print(f"No .md files found in {learnings_dir}")
        return

    mode_label = "DRY RUN" if dry_run else "APPLY"
    print(f"[{mode_label}] Scanning {len(files)} files in {learnings_dir}\n")

    changed = 0
    skipped = 0

    for file_path in files:
        try:
            was_changed, topic = process_file(file_path, dry_run)
        except Exception as e:
            print(f"  [ERROR] {file_path.name}: {e}")
            continue

        if was_changed:
            changed += 1
            verb = "Would add" if dry_run else "Added"
            print(f"  {verb} **Topic:** {topic}  ->  {file_path.name}")
        else:
            skipped += 1

    print(f"\nSummary: {changed} files {'would be ' if dry_run else ''}updated, {skipped} already have a topic.")
    if dry_run and changed > 0:
        print("Run with --apply to write changes.")


if __name__ == "__main__":
    main()
