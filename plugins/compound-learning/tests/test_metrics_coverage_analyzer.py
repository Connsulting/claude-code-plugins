import importlib.util
import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "metrics-coverage-analyzer.py"

_spec = importlib.util.spec_from_file_location("metrics_coverage_analyzer", SCRIPT_PATH)
analyzer = importlib.util.module_from_spec(_spec)
assert _spec is not None and _spec.loader is not None
sys.modules[_spec.name] = analyzer
_spec.loader.exec_module(analyzer)


def test_collect_python_events_extracts_literal_emit_calls(tmp_path):
    scan_root = tmp_path / "scan"
    scan_root.mkdir()
    sample = scan_root / "sample.py"
    sample.write_text(
        "\n".join(
            [
                "def run(logger, status):",
                "    logger.emit('pipeline', 'start')",
                "    logger.emit('pipeline', 'completed')",
                "    logger.emit('partial', status)",
                "    logger.emit(operation, 'success')",
                "    other.emit('pipeline', 'success')",
            ]
        ),
        encoding="utf-8",
    )

    events, error = analyzer.collect_python_events(sample, scan_root)

    assert error is None
    assert [(event.operation, event.status) for event in events] == [
        ("pipeline", "start"),
        ("pipeline", "success"),
    ]


def test_collect_shell_events_handles_multiline_and_dynamic_status(tmp_path):
    scan_root = tmp_path / "scan"
    scan_root.mkdir()
    sample = scan_root / "hook.sh"
    sample.write_text(
        "\n".join(
            [
                "hook_obs_event \"info\" \"hook_start\" \"start\"",
                "hook_obs_event \"warn\" \"hook_end\" \"$HOOK_OUTCOME\" \\",
                "  --message \"hook finished\"",
                "hook_obs_event \"info\" \"dependency_check\" \"missing\"",
                "hook_obs_event \"$LEVEL\" \"$OP\" \"success\"",
            ]
        ),
        encoding="utf-8",
    )

    events, error = analyzer.collect_shell_events(sample, scan_root)

    assert error is None
    assert [(event.operation, event.status, event.dynamic_status) for event in events] == [
        ("hook_start", "start", False),
        ("hook_end", "<dynamic>", True),
        ("dependency_check", "missing", False),
    ]


def test_analyze_scan_root_aggregates_hotspots(tmp_path):
    scan_root = tmp_path / "scan"
    scan_root.mkdir()
    (scan_root / "events.py").write_text(
        "\n".join(
            [
                "def run(logger):",
                "    logger.emit('pipeline', 'start')",
                "    logger.emit('pipeline', 'success')",
                "    logger.emit('partial', 'start')",
            ]
        ),
        encoding="utf-8",
    )
    (scan_root / "hook.sh").write_text(
        "\n".join(
            [
                "hook_obs_event \"info\" \"probe\" \"start\"",
                "hook_obs_event \"warn\" \"probe\" \"failure\"",
            ]
        ),
        encoding="utf-8",
    )

    report = analyzer.analyze_scan_root(scan_root)

    overall = report["overall"]
    assert overall["instrumented_operations"] == 3
    assert overall["operations_with_terminal_status"] == 2
    assert overall["coverage_percent"] == 66.67
    assert overall["missing_terminal_operations"] == ["partial"]

    hotspots = report["hotspots"]
    assert hotspots
    assert hotspots[0]["path"] == "events.py"
    assert hotspots[0]["missing_terminal_operations"] == ["partial"]


def test_main_returns_non_zero_when_coverage_below_threshold(tmp_path, capsys):
    scan_root = tmp_path / "scan"
    scan_root.mkdir()
    (scan_root / "events.py").write_text(
        "\n".join(
            [
                "def run(logger):",
                "    logger.emit('only_start', 'start')",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = analyzer.main(
        [
            "--scan-root",
            str(scan_root),
            "--output",
            "json",
            "--fail-under",
            "90",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 2
    assert payload["threshold"]["passed"] is False
    assert payload["overall"]["coverage_percent"] == 0.0
