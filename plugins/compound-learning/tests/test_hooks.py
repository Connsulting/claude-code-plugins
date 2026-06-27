"""
Hook behavior tests for the extract-transcript-context hook.

These are fast tests (no indexed corpus needed, no slow marker).
They verify transcript parsing, truncation, and edge case handling.
"""

import importlib.util
import json
import os
import subprocess
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


def test_auto_peek_codex_keyword_engine_uses_codex_exec(tmp_path):
    """Codex bridge uses codex exec and passes raw keyword JSON to search."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    codex_argv = tmp_path / "codex.argv"
    claude_argv = tmp_path / "claude.argv"
    search_keywords = tmp_path / "search-keywords.json"

    python_stub = bin_dir / "python3"
    python_stub.write_text(
        """#!/bin/sh
case "$1" in
  */rollout-to-transcript.py)
    exec "$REAL_PYTHON" "$@"
    ;;
  */extract-transcript-context.py)
    exit 0
    ;;
  */search-learnings.py)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--keywords-json" ]; then
        shift
        printf '%s\\n' "$1" > "$SEARCH_KEYWORDS_FILE"
        break
      fi
      shift
    done
    printf '%s\\n' '{"status":"none","count":0,"learnings":[]}'
    exit 0
    ;;
esac
printf 'unexpected python3 call: %s\\n' "$*" >&2
exit 2
""",
        encoding="utf-8",
    )
    python_stub.chmod(0o755)

    codex_stub = bin_dir / "codex"
    codex_stub.write_text(
        """#!/bin/sh
printf '%s\\n' "$@" > "$CODEX_ARGV_FILE"
printf '%s\\n' '{"keywords":["codex hooks"]}'
""",
        encoding="utf-8",
    )
    codex_stub.chmod(0o755)

    claude_stub = bin_dir / "claude"
    claude_stub.write_text(
        """#!/bin/sh
printf '%s\\n' "$@" > "$CLAUDE_ARGV_FILE"
printf '%s\\n' '{"result":"{\\"keywords\\":[\\"claude haiku\\"]}"}'
""",
        encoding="utf-8",
    )
    claude_stub.chmod(0o755)

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "How does Codex auto peek work?",
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "CODEX_ARGV_FILE": str(codex_argv),
        "CLAUDE_ARGV_FILE": str(claude_argv),
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "REAL_PYTHON": sys.executable,
        "SEARCH_KEYWORDS_FILE": str(search_keywords),
    }
    hook_input = {
        "prompt": "Find the Codex auto peek keyword extraction behavior",
        "transcript_path": str(rollout),
        "session_id": "codex-test-session",
        "cwd": str(tmp_path),
    }

    result = subprocess.run(
        ["bash", str(PLUGIN_ROOT / "codex" / "peek.sh")],
        input=json.dumps(hook_input),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert codex_argv.exists(), "expected codex keyword engine to invoke codex"
    assert not claude_argv.exists(), "codex keyword engine must not invoke claude"
    assert search_keywords.read_text(encoding="utf-8").strip() == '["codex hooks"]'

    argv = codex_argv.read_text(encoding="utf-8").splitlines()
    assert argv[0] == "exec"
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--ephemeral" in argv
    assert "--skip-git-repo-check" in argv

    model_index = argv.index("--model") if "--model" in argv else argv.index("-m")
    assert argv[model_index + 1] == "gpt-5.4-mini"
    assert any("reasoning" in arg and "low" in arg for arg in argv)
    assert (
        ("--disable" in argv and "hooks" in argv)
        or any("hooks" in arg and "false" in arg for arg in argv)
    )
