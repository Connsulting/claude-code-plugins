# ruff: noqa: E402
import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.observability as observability

_search_spec = importlib.util.spec_from_file_location(
    "search_learnings",
    PLUGIN_ROOT / "scripts" / "search-learnings.py",
)
search_module = importlib.util.module_from_spec(_search_spec)
_search_spec.loader.exec_module(search_module)


def test_structured_event_shape(tmp_path):
    log_path = tmp_path / "observability.jsonl"
    config = {
        "observability": {
            "enabled": True,
            "level": "debug",
            "logPath": str(log_path),
            "context": {
                "correlation_id": "corr-123",
                "session_id": "sess-123",
            },
        }
    }
    logger = observability.get_logger("search", config, operation_name="unit-test")
    logger.emit(
        "fanout",
        "success",
        level="info",
        duration_ms=12,
        counts={"keywords": 2},
        message="fanout completed",
    )

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    event = json.loads(lines[0])
    assert event["timestamp"]
    assert event["level"] == "info"
    assert event["component"] == "search"
    assert event["operation"] == "fanout"
    assert event["status"] == "success"
    assert event["duration_ms"] == 12
    assert event["counts"]["keywords"] == 2
    assert event["correlation_id"] == "corr-123"
    assert event["session_id"] == "sess-123"
    assert event["operation_name"] == "unit-test"


def test_level_filtering(tmp_path):
    log_path = tmp_path / "observability.jsonl"
    config = {
        "observability": {
            "enabled": True,
            "level": "warn",
            "logPath": str(log_path),
        }
    }
    logger = observability.get_logger("db", config)
    logger.emit("vector_search", "success", level="info")
    logger.emit("vector_search", "error", level="error")

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["level"] == "error"


def test_attach_runtime_context_prefers_hook_env(monkeypatch):
    config = {
        "observability": {
            "enabled": True,
            "context": {
                "correlation_id": "stale-correlation",
                "session_id": "stale-session",
            },
        }
    }
    monkeypatch.setenv("LEARNINGS_OBS_CORRELATION_ID", "corr-hook-123")
    monkeypatch.setenv("LEARNINGS_OBS_SESSION_ID", "sess-hook-123")

    context = observability.attach_runtime_context(config, operation_name="search_learnings")

    assert context["correlation_id"] == "corr-hook-123"
    assert context["session_id"] == "sess-hook-123"
    assert context["operation_name"] == "search_learnings"


def test_search_stdout_contract_with_observability(monkeypatch, capsys, tmp_path):
    log_path = tmp_path / "search-observability.jsonl"
    config = {
        "learnings": {
            "highConfidenceThreshold": 0.40,
            "possiblyRelevantThreshold": 0.55,
            "keywordBoostWeight": 0.65,
        },
        "observability": {
            "enabled": True,
            "level": "debug",
            "logPath": str(log_path),
        },
    }
    monkeypatch.setenv("LEARNINGS_OBS_CORRELATION_ID", "corr-hook-abc")
    monkeypatch.setenv("LEARNINGS_OBS_SESSION_ID", "sess-hook-abc")

    class FakeConn:
        def close(self):
            return None

    def fake_query_single_keyword(_config, keyword, _scope_repos, _query_size, logger=None):
        if logger:
            logger.emit(
                "keyword_query",
                "success",
                level="debug",
                counts={"result_count": 1},
                keyword=keyword,
            )
        return [
            {
                "id": "learning-1",
                "document": "JWT authentication pattern",
                "metadata": {"scope": "global", "repo": "", "file_path": "/tmp/a.md", "topic": "auth", "keywords": "jwt"},
                "distance": 0.20,
            }
        ]

    monkeypatch.setattr(search_module.db, "load_config", lambda: config)
    monkeypatch.setattr(search_module, "detect_learning_hierarchy", lambda _cwd, _home: [])
    monkeypatch.setattr(search_module, "query_single_keyword", fake_query_single_keyword)
    monkeypatch.setattr(search_module.db, "get_connection", lambda _config: FakeConn())
    monkeypatch.setattr(search_module, "fts5_search", lambda _conn, _query, limit=50, logger=None: set())

    search_module.search_learnings(
        "jwt auth",
        working_dir=str(tmp_path),
        max_results=2,
        keywords_json='["jwt auth"]',
    )
    output = capsys.readouterr().out

    payload = json.loads(output)
    assert payload["status"] == "success"
    assert "high_confidence" in payload
    assert log_path.exists()
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").strip().splitlines() if line.strip()]
    assert lines
    assert all(event.get("correlation_id") == "corr-hook-abc" for event in lines)
    assert all(event.get("session_id") == "sess-hook-abc" for event in lines)
