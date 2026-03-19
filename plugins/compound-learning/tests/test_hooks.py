"""
Hook behavior tests for the extract-transcript-context hook.

These are fast tests (no indexed corpus needed, no slow marker).
They verify transcript parsing, truncation, and edge case handling.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

# Import extract-transcript-context.py via spec (hyphens in filename)
_hook_spec = importlib.util.spec_from_file_location(
    "extract_transcript_context",
    PLUGIN_ROOT / "hooks" / "extract-transcript-context.py",
)
_hook_mod = importlib.util.module_from_spec(_hook_spec)
_hook_spec.loader.exec_module(_hook_mod)

extract_context = _hook_mod.extract_context
is_real_user_prompt = _hook_mod.is_real_user_prompt


def _write_transcript(path: Path, entries: list) -> Path:
    """Write a list of dicts as JSONL to the given path. Returns the path."""
    transcript = path / "transcript.jsonl"
    with open(transcript, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return transcript


# ---------------------------------------------------------------------------
# Test 1: Basic context extraction
# ---------------------------------------------------------------------------

def test_extract_transcript_context_basic(tmp_path):
    """Extracts assistant text between the last two user prompts."""
    entries = [
        {"type": "user", "message": {"content": "First user message"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "First assistant response."}
                ]
            },
        },
        {"type": "user", "message": {"content": "Second user message"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Second assistant response."}
                ]
            },
        },
    ]

    transcript = _write_transcript(tmp_path, entries)
    context = extract_context(str(transcript))

    assert isinstance(context, str)
    assert len(context) > 0, "Expected non-empty context from valid transcript"
    # Should contain the assistant response between the last two user prompts
    assert "First assistant response." in context
    assert "Second assistant response." not in context


# ---------------------------------------------------------------------------
# Test 2: Truncation respects max_chars
# ---------------------------------------------------------------------------

def test_extract_transcript_context_truncation(tmp_path):
    """Output must respect the max_chars limit."""
    long_text = "A" * 5000

    entries = [
        {"type": "user", "message": {"content": "First prompt"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": long_text}
                ]
            },
        },
        {"type": "user", "message": {"content": "Second prompt"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": long_text}
                ]
            },
        },
    ]

    transcript = _write_transcript(tmp_path, entries)
    max_chars = 500
    context = extract_context(str(transcript), max_chars=max_chars)

    # Allow small overhead for truncation indicator ("...")
    assert len(context) <= max_chars + 50, (
        f"Context length {len(context)} exceeds max_chars {max_chars} by too much"
    )


# ---------------------------------------------------------------------------
# Test 3: Empty file returns empty string
# ---------------------------------------------------------------------------

def test_extract_transcript_context_empty_file(tmp_path):
    """An empty transcript file should return an empty string."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    context = extract_context(str(transcript))
    assert context == "", f"Expected empty string from empty file, got: {context!r}"


# ---------------------------------------------------------------------------
# Test 4: No user prompts (only system/tool messages)
# ---------------------------------------------------------------------------

def test_extract_transcript_context_no_user_prompts(tmp_path):
    """Transcript with only system/tool messages should return empty or handle gracefully."""
    entries = [
        {"type": "system", "message": {"content": "System initialization"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Tool output processing."}
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "abc123", "content": "result data"}
                ]
            },
        },
    ]

    transcript = _write_transcript(tmp_path, entries)
    context = extract_context(str(transcript))

    # No real user prompts found, so either empty or gracefully handled
    assert isinstance(context, str), "Should return a string even with no user prompts"


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

def test_is_real_user_prompt_string_content():
    """Real user prompts have string content."""
    entry = {"type": "user", "message": {"content": "Hello"}}
    assert is_real_user_prompt(entry) is True


def test_is_real_user_prompt_tool_result():
    """Tool results have array content and should not be treated as user prompts."""
    entry = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "data"}
            ]
        },
    }
    assert is_real_user_prompt(entry) is False


def test_is_real_user_prompt_assistant_type():
    """Assistant entries are not user prompts."""
    entry = {"type": "assistant", "message": {"content": "response"}}
    assert is_real_user_prompt(entry) is False
