#!/usr/bin/env python3
"""Extract user and assistant messages from Claude, Grok, or Codex JSONL.

Filters out tool calls, file snapshots, and metadata to produce clean conversation text.
"""
from __future__ import annotations

import json
import re
import sys

_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)

# Prefixes that are injected context, not a real user prompt.
_NOISE_PREFIXES = (
    "<user_info>",
    "<git_status>",
    "<system-reminder>",
    "<open_and_recently_viewed_files>",
    "# AGENTS.md",
    "<INSTRUCTIONS>",
    "<user-instructions>",
    "<command-name>",
    "<local-command",
)


def _flatten_content(content) -> str:
    """Turn a string or list of text blocks into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") in ("text", "input_text", "output_text"):
                parts.append(block.get("text") or "")
            elif isinstance(block.get("text"), str) and block.get("type") not in (
                "tool_use",
                "tool_result",
                "function_call",
            ):
                parts.append(block["text"])
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _role_and_content(entry: dict) -> tuple[str | None, str]:
    """Return (role, text) for a Claude, Grok, or Codex JSONL entry."""
    entry_type = entry.get("type")

    if entry.get("isMeta"):
        return None, ""

    # Claude: {"type": "user"|"assistant", "message": {"content": ...}}
    if entry_type in ("user", "assistant") and "message" in entry:
        return entry_type, _flatten_content(entry.get("message", {}).get("content", ""))

    # Grok chat_history: {"type": "user"|"assistant", "content": ...}
    if entry_type in ("user", "assistant") and "content" in entry:
        return entry_type, _flatten_content(entry.get("content", ""))

    # Codex rollout: {"type": "response_item", "payload": {"type": "message", "role": ..., "content": ...}}
    if entry_type == "response_item":
        payload = entry.get("payload") or {}
        if payload.get("type") == "message":
            role = payload.get("role")
            if role in ("user", "assistant"):
                return role, _flatten_content(payload.get("content", ""))

    return None, ""


def _clean_user_text(text: str) -> str:
    """Keep the real prompt; drop injected preambles."""
    match = _USER_QUERY_RE.search(text)
    if match:
        return match.group(1).strip()

    stripped = text.lstrip()
    if any(stripped.startswith(prefix) for prefix in _NOISE_PREFIXES):
        return ""
    if "<command-name>" in text or "<local-command" in text:
        return ""
    if "<system-reminder>" in text and "Called the" in text:
        return ""
    return text


def _clean_assistant_text(text: str) -> str:
    if "<command-name>" in text or "<local-command" in text:
        return ""
    if "<system-reminder>" in text and "Called the" in text:
        return ""
    return text


def extract_messages(jsonl_path: str, max_bytes: int = 100000) -> str:
    """Extract user and assistant message content from transcript."""
    messages = []

    try:
        handle = open(jsonl_path, "r", encoding="utf-8")
    except OSError:
        return ""

    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue

            role, content = _role_and_content(entry)
            if not role or not content:
                continue

            if role == "user":
                content = _clean_user_text(content)
            else:
                content = _clean_assistant_text(content)
            if not content.strip():
                continue

            messages.append(f"[{role.upper()}]: {content[:2000]}")

    full_text = "\n\n".join(messages)
    encoded = full_text.encode("utf-8")
    if len(encoded) > max_bytes:
        full_text = full_text[-max_bytes:]
        first_bracket = full_text.find("[")
        if first_bracket > 0:
            full_text = full_text[first_bracket:]

    return full_text


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: extract-transcript-messages.py <jsonl_path> [max_bytes]",
            file=sys.stderr,
        )
        sys.exit(1)

    jsonl_path = sys.argv[1]
    max_bytes = int(sys.argv[2]) if len(sys.argv) > 2 else 100000
    print(extract_messages(jsonl_path, max_bytes))
