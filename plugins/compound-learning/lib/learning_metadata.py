"""Shared parsing helpers for learning markdown metadata fields."""

from __future__ import annotations

import re


def extract_field(content: str, field: str) -> str | None:
    """Extract a markdown metadata field value from `**Field:** value`."""
    pattern = rf"\*\*{re.escape(field)}:\*\*\s*(.+?)(?:\n|$)"
    match = re.search(pattern, content, re.IGNORECASE)
    return match.group(1).strip() if match else None


def extract_tags(
    content: str,
    *,
    strip_brackets: bool = False,
    max_tags: int = 8,
) -> list[str]:
    """Extract normalised tags from `**Tags:**` metadata."""
    tags_str = extract_field(content, "Tags")
    if not tags_str:
        return []

    if strip_brackets:
        tags_str = tags_str.strip("[]")

    tags = [tag.strip().lower() for tag in tags_str.split(",") if tag.strip()]
    return tags[:max_tags]
