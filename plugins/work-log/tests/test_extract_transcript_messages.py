"""Extract user/assistant text from Claude, Grok, and Codex transcripts."""

import importlib.util
import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "extract_transcript_messages",
    PLUGIN_ROOT / "hooks" / "extract-transcript-messages.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
extract_messages = _mod.extract_messages


def _write(path: Path, entries: list) -> Path:
    transcript = path / "transcript.jsonl"
    transcript.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )
    return transcript


def test_claude_user_and_assistant(tmp_path):
    transcript = _write(
        tmp_path,
        [
            {"type": "user", "message": {"content": "Ship the work log hook"}},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "I will wire all three engines."}]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "bash", "input": {}}]
                },
            },
        ],
    )
    text = extract_messages(str(transcript))
    assert "[USER]: Ship the work log hook" in text
    assert "[ASSISTANT]: I will wire all three engines." in text
    assert "tool_use" not in text


def test_claude_skips_command_and_meta(tmp_path):
    transcript = _write(
        tmp_path,
        [
            {"type": "user", "isMeta": True, "message": {"content": "hidden"}},
            {
                "type": "user",
                "message": {"content": "<command-name>compact</command-name>"},
            },
            {"type": "user", "message": {"content": "real question"}},
        ],
    )
    text = extract_messages(str(transcript))
    assert "hidden" not in text
    assert "compact" not in text
    assert "[USER]: real question" in text


def test_grok_user_query_and_string_assistant(tmp_path):
    transcript = _write(
        tmp_path,
        [
            {"type": "system", "content": "You are Grok"},
            {
                "type": "user",
                "content": [{"type": "text", "text": "<user_info>\nOS Version: linux\n"}],
            },
            {
                "type": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "<system-reminder>\nMCP server connected\n</system-reminder>",
                    }
                ],
            },
            {
                "type": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "<user_query>Are Grok sessions in Notion?</user_query>",
                    }
                ],
            },
            {"type": "reasoning", "content": "thinking"},
            {
                "type": "assistant",
                "content": "Grok is not logging to Notion yet.",
                "tool_calls": [],
            },
            {"type": "tool_result", "content": "ok"},
        ],
    )
    text = extract_messages(str(transcript))
    assert "[USER]: Are Grok sessions in Notion?" in text
    assert "[ASSISTANT]: Grok is not logging to Notion yet." in text
    assert "You are Grok" not in text
    assert "OS Version" not in text
    assert "thinking" not in text


def test_codex_response_item_messages(tmp_path):
    transcript = _write(
        tmp_path,
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Log this Codex session"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "permissions"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Logged."}],
                },
            },
            {
                "type": "event_msg",
                "payload": {"role": "assistant", "content": "duplicate"},
            },
        ],
    )
    text = extract_messages(str(transcript))
    assert "[USER]: Log this Codex session" in text
    assert "[ASSISTANT]: Logged." in text
    assert "permissions" not in text
    assert "duplicate" not in text


def test_codex_drops_agents_md_preamble(tmp_path):
    transcript = _write(
        tmp_path,
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "# AGENTS.md instructions\nDo not"}
                    ],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "real Codex prompt"}],
                },
            },
        ],
    )
    text = extract_messages(str(transcript))
    assert "AGENTS.md" not in text
    assert "[USER]: real Codex prompt" in text


def test_missing_file_returns_empty(tmp_path):
    assert extract_messages(str(tmp_path / "missing.jsonl")) == ""
