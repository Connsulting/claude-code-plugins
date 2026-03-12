import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = PLUGIN_ROOT / "hooks" / "extract-transcript-context.py"

_spec = importlib.util.spec_from_file_location("extract_transcript_context", MODULE_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules["extract_transcript_context"] = mod
_spec.loader.exec_module(mod)


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.write_text(
        "".join(f"{json.dumps(entry)}\n" for entry in entries),
        encoding="utf-8",
    )


def test_extract_context_returns_recent_assistant_context_without_stderr(tmp_path, capsys):
    transcript_path = tmp_path / "session.jsonl"
    _write_transcript(
        transcript_path,
        [
            {
                "type": "user",
                "message": {"content": "first prompt"},
            },
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "first answer"}],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "follow-up detail"}],
                },
            },
            {
                "type": "user",
                "message": {"content": "latest prompt"},
            },
        ],
    )

    context = mod.extract_context(str(transcript_path))
    captured = capsys.readouterr()

    assert context == "first answer\nfollow-up detail"
    assert captured.err == ""


def test_extract_context_logs_transcript_read_failures_to_stderr(tmp_path, capsys):
    missing_path = tmp_path / "missing.jsonl"

    context = mod.extract_context(str(missing_path))
    captured = capsys.readouterr()

    assert context == ""
    assert "[extract-transcript-context] failed to read transcript" in captured.err
    assert str(missing_path) in captured.err

