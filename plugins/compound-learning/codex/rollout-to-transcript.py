#!/usr/bin/env python3
"""Convert a Codex rollout JSONL into Claude-transcript JSONL.

The compound-learning plugin's parsers (extract-transcript-context.py and
extract-transcript-messages.py) expect Claude's transcript schema:
  - top-level {"type": "user", "message": {"content": "<string>"}}
  - top-level {"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}

Codex rollouts use a different shape:
  {"type": "response_item", "payload": {"type": "message", "role": "user"|"assistant"|"developer",
                                        "content": [{"type": "input_text"|"output_text", "text": "..."}]}}

This adapter emits the Claude shape so the unmodified plugin scripts work on Codex
sessions. Keeping the conversion here (not in the plugin) means the plugin stays a
black box: one source of truth for both engines, no cross-repo edits.

Read-only on the rollout; writes converted JSONL to stdout.
"""
import json
import sys

# User messages Codex injects at turn start that are not real user prompts.
# These mirror Claude's <command-name>/<system-reminder> noise that the plugin
# parsers already strip; we drop them here so keyword extraction stays clean.
# Matched as a PREFIX only: Codex sends the AGENTS.md preamble as its own user
# message (separate from the real prompt), so prefix-anchoring drops the
# preamble without ever discarding a genuine prompt that merely mentions these
# tokens. (The <permissions instructions> block arrives in the developer role,
# which convert() already skips entirely.)
_NOISE_PREFIXES = (
    "# AGENTS.md instructions",
    "<INSTRUCTIONS>",
    "<user-instructions>",
)


def _text_from_content(content) -> str:
    """Flatten a Codex message content array into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            # input_text (user/developer), output_text (assistant), and plain text
            if block.get("type") in ("input_text", "output_text", "text"):
                t = block.get("text", "")
                if t:
                    parts.append(t)
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _is_noise_user(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(p) for p in _NOISE_PREFIXES)


def convert(rollout_path: str) -> list:
    """Return a list of Claude-transcript JSONL strings from a Codex rollout."""
    out = []
    try:
        f = open(rollout_path, "r")
    except (FileNotFoundError, PermissionError):
        return out

    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Only the durable conversation items carry message text; event_msg
            # entries duplicate them, so we ignore those to avoid double-counting.
            if entry.get("type") != "response_item":
                continue
            payload = entry.get("payload", {})
            if payload.get("type") != "message":
                continue

            role = payload.get("role")
            text = _text_from_content(payload.get("content", "")).strip()
            if not text:
                continue

            if role == "user":
                if _is_noise_user(text):
                    continue
                # String content => is_real_user_prompt() in the context parser
                # recognizes it as a genuine prompt boundary.
                out.append(json.dumps({"type": "user", "message": {"content": text}}))
            elif role == "assistant":
                out.append(
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {"content": [{"type": "text", "text": text}]},
                        }
                    )
                )
            # 'developer' (system/permissions prompt) is intentionally dropped.

    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(0)
    for converted_line in convert(sys.argv[1]):
        print(converted_line)
