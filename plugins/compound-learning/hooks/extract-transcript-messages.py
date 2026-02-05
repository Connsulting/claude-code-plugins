#!/usr/bin/env python3
"""Extract user and assistant messages from Claude transcript JSONL.

Filters out tool calls, file snapshots, and metadata to produce clean conversation text.
"""
import json
import sys

def extract_messages(jsonl_path: str, max_bytes: int = 100000) -> str:
    """Extract user and assistant message content from transcript."""
    messages = []

    with open(jsonl_path, 'r') as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            # Skip non-message entries
            if entry.get('type') not in ('user', 'assistant'):
                continue

            # Skip meta messages
            if entry.get('isMeta'):
                continue

            msg = entry.get('message', {})
            content = msg.get('content', '')

            # Skip empty content
            if not content:
                continue

            # For assistant messages, content can be a list of blocks
            if isinstance(content, list):
                # Extract just text blocks, skip tool_use blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        text_parts.append(block.get('text', ''))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = '\n'.join(text_parts)

            # Skip command-related content
            if '<command-name>' in content or '<local-command' in content:
                continue

            # Skip tool results
            if '<system-reminder>' in content and 'Called the' in content:
                continue

            role = entry.get('type', 'unknown')
            messages.append(f"[{role.upper()}]: {content[:2000]}")  # Limit per message

    # Join and truncate to max bytes
    full_text = '\n\n'.join(messages)
    if len(full_text.encode('utf-8')) > max_bytes:
        # Take from end (most recent)
        full_text = full_text[-max_bytes:]
        # Find first complete message
        first_bracket = full_text.find('[')
        if first_bracket > 0:
            full_text = full_text[first_bracket:]

    return full_text

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: extract-transcript-messages.py <jsonl_path> [max_bytes]", file=sys.stderr)
        sys.exit(1)

    jsonl_path = sys.argv[1]
    max_bytes = int(sys.argv[2]) if len(sys.argv) > 2 else 100000

    print(extract_messages(jsonl_path, max_bytes))
