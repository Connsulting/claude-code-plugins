#!/usr/bin/env python3
"""
Extract recent conversation context from Claude transcript JSONL.

Reads backward from the end and extracts assistant text content
between the last two real user prompts (not tool results).

Output is truncated to a reasonable size for fast Haiku processing.
"""

import json
import sys
from typing import Iterator


def is_real_user_prompt(entry: dict) -> bool:
    """Check if entry is a real user prompt (not a tool_result)."""
    if not isinstance(entry, dict):
        return False
    if entry.get('type') != 'user':
        return False
    message = entry.get('message', {})
    if not isinstance(message, dict):
        return False
    content = message.get('content')
    # Real user prompts have string content, tool results have array content
    return isinstance(content, str)


def iter_assistant_text(content) -> Iterator[str]:
    """Yield assistant text blocks from supported transcript content shapes."""
    if isinstance(content, str):
        yield content
        return

    if not isinstance(content, list):
        return

    for item in content:
        if isinstance(item, str):
            yield item
            continue
        if not isinstance(item, dict):
            continue
        if item.get('type') != 'text':
            continue
        text = item.get('text', '')
        if isinstance(text, str):
            yield text


def extract_context(transcript_path: str, max_chars: int = 3000) -> str:
    """Extract text content from transcript since last user message."""
    try:
        with open(transcript_path, 'r') as f:
            lines = f.readlines()
    except (FileNotFoundError, PermissionError):
        return ""

    # Parse lines in reverse to find content between last two user prompts
    context_parts = []
    total_chars = 0
    user_prompts_found = 0

    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        entry_type = entry.get('type')

        # Check for real user prompt (not tool_result)
        if is_real_user_prompt(entry):
            user_prompts_found += 1
            if user_prompts_found >= 2:
                # We've found the previous user prompt, stop collecting
                break
            continue

        # Skip non-assistant types (progress, file-history-snapshot, tool results, etc)
        if entry_type != 'assistant':
            continue

        # Only collect after we've passed the current user prompt
        if user_prompts_found == 0:
            continue

        # Extract text from assistant messages
        message = entry.get('message', {})
        if not isinstance(message, dict):
            continue
        content = message.get('content', [])

        for text in iter_assistant_text(content):
            if text and total_chars + len(text) <= max_chars:
                context_parts.append(text)
                total_chars += len(text)
            elif text:
                # Truncate to fit
                remaining = max_chars - total_chars
                if remaining > 100:
                    context_parts.append(text[:remaining] + "...")
                total_chars = max_chars
                break

        if total_chars >= max_chars:
            break

    # Reverse to get chronological order
    context_parts.reverse()
    return "\n".join(context_parts)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("", end="")
        sys.exit(0)

    transcript_path = sys.argv[1]
    max_chars = int(sys.argv[2]) if len(sys.argv) > 2 else 3000

    context = extract_context(transcript_path, max_chars)
    print(context, end="")
