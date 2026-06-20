#!/usr/bin/env python3
"""
Extract recent conversation context from Claude transcript JSONL.

Reads backward from the end and extracts assistant text content
between the last two real user prompts (not tool results).

Output is truncated to a reasonable size for fast Haiku processing.
"""

import json
import os
import sys

# Only the last exchange is ever needed (the extractor stops after walking past
# two user prompts), and the collected text is capped at max_chars anyway. So we
# never need more than the tail of the transcript. Reading a bounded tail instead
# of the whole file keeps this hook O(1) in conversation length — large sessions
# were loading tens of MB into memory on every prompt submission.
MAX_TAIL_BYTES = 1_048_576  # 1 MiB — comfortably covers many recent exchanges


def _read_tail_lines(path: str, max_bytes: int = MAX_TAIL_BYTES) -> list:
    """Return the last lines of a file, reading at most max_bytes from the end.

    If the file is larger than max_bytes we seek to the tail; the first line of
    that window is almost certainly a partial JSONL record, so we drop it.
    """
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            partial = size > max_bytes
            if partial:
                f.seek(size - max_bytes)
            data = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        return []

    lines = data.decode('utf-8', errors='ignore').splitlines()
    if partial and lines:
        # Drop the leading partial record left by seeking into the middle of a line.
        lines = lines[1:]
    return lines


def is_real_user_prompt(entry: dict) -> bool:
    """Check if entry is a real user prompt (not a tool_result)."""
    if entry.get('type') != 'user':
        return False
    content = entry.get('message', {}).get('content')
    # Real user prompts have string content, tool results have array content
    return isinstance(content, str)


def extract_context(transcript_path: str, max_chars: int = 3000) -> str:
    """Extract text content from transcript since last user message."""
    lines = _read_tail_lines(transcript_path)

    # Parse lines in reverse to find content between last two user prompts
    context_parts = []
    total_chars = 0
    user_prompts_found = 0

    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
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
        content = message.get('content', [])

        if isinstance(content, list):
            for item in content:
                if item.get('type') == 'text':
                    text = item.get('text', '')
                    if text and total_chars + len(text) <= max_chars:
                        context_parts.append(text)
                        total_chars += len(text)
                    elif text:
                        # Truncate to fit
                        remaining = max_chars - total_chars
                        if remaining > 100:
                            context_parts.append(text[:remaining] + "...")
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
