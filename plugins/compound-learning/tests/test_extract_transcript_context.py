import importlib.util
import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent

_context_spec = importlib.util.spec_from_file_location(
    "extract_transcript_context",
    PLUGIN_ROOT / "hooks" / "extract-transcript-context.py",
)
context_module = importlib.util.module_from_spec(_context_spec)
_context_spec.loader.exec_module(context_module)


def _write_jsonl(path: Path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            if isinstance(record, str):
                handle.write(record + "\n")
            else:
                handle.write(json.dumps(record) + "\n")


def test_extract_context_handles_mixed_string_and_dict_blocks(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"content": "older prompt"}},
            {
                "type": "assistant",
                "message": {"content": ["string block", {"type": "text", "text": "dict block"}]},
            },
            {"type": "user", "message": {"content": "latest prompt"}},
        ],
    )

    context = context_module.extract_context(str(transcript))

    assert "string block" in context
    assert "dict block" in context


def test_extract_context_skips_malformed_jsonl_lines(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"content": "older prompt"}},
            "this is not valid json",
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "safe text"}]}},
            '{"type":"assistant","message":',
            {"type": "user", "message": {"content": "latest prompt"}},
        ],
    )

    context = context_module.extract_context(str(transcript))

    assert context == "safe text"


def test_extract_context_ignores_unexpected_content_item_types(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"content": "older prompt"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        7,
                        None,
                        {"type": "tool_use", "name": "bash"},
                        {"type": "text", "text": 123},
                        {"type": "text", "text": "valid text"},
                        True,
                    ]
                },
            },
            {"type": "user", "message": {"content": "latest prompt"}},
        ],
    )

    context = context_module.extract_context(str(transcript))

    assert context == "valid text"
