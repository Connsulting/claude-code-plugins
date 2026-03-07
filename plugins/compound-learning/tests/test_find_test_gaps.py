import importlib.util
import json
import sys
import textwrap
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "find-test-gaps.py"

_spec = importlib.util.spec_from_file_location("find_test_gaps", SCRIPT_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules["find_test_gaps"] = mod
_spec.loader.exec_module(mod)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def _write_coverage(plugin_root: Path, file_percents: dict[str, float]) -> Path:
    files = {}
    for rel_path, percent in file_percents.items():
        files[rel_path] = {
            "summary": {
                "covered_lines": int(percent),
                "num_statements": 100,
                "percent_covered": percent,
            },
            "executed_lines": [],
            "missing_lines": [],
        }

    payload = {
        "meta": {"format": 2},
        "files": files,
        "totals": {},
    }
    coverage_path = plugin_root / "coverage.json"
    coverage_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return coverage_path


def test_classifies_expected_gap_types(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(plugin_root / "lib" / "untested.py", "def run_untested():\n    return 1\n")
    _write(plugin_root / "lib" / "low.py", "def run_low():\n    return 2\n")
    _write(plugin_root / "hooks" / "run.sh", "#!/usr/bin/env bash\necho run\n")

    _write(plugin_root / "tests" / "test_low.py", "import lib.low\n")
    _write(
        plugin_root / "tests" / "test_hooks.py",
        """
        import subprocess

        def test_hook_script_reference():
            subprocess.run(["bash", "hooks/run.sh"], check=False)
        """,
    )

    coverage_path = _write_coverage(
        plugin_root,
        {
            "lib/untested.py": 0.0,
            "lib/low.py": 42.0,
        },
    )

    report = mod.find_test_gaps(
        plugin_root=plugin_root,
        threshold=60.0,
        coverage_json=coverage_path,
    )
    by_path = {gap["path"]: gap for gap in report["gaps"]}

    assert by_path["lib/untested.py"]["classification"] == "untested"
    assert by_path["lib/low.py"]["classification"] == "low_coverage"
    assert by_path["hooks/run.sh"]["classification"] == "indirectly_tested"
    assert by_path["hooks/run.sh"]["indirect_test_references"] == ["tests/test_hooks.py"]


def test_prioritization_is_deterministic(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(plugin_root / "lib" / "alpha_untested.py", "def alpha():\n    return 1\n")
    _write(plugin_root / "lib" / "beta_low.py", "def beta():\n    return 2\n")
    _write(plugin_root / "hooks" / "runner.sh", "#!/usr/bin/env bash\necho hi\n")

    _write(plugin_root / "tests" / "test_beta.py", "import lib.beta_low\n")
    _write(
        plugin_root / "tests" / "test_runner.py",
        """
        import subprocess

        def test_runner_script_reference():
            subprocess.run(["hooks/runner.sh"], check=False)
        """,
    )

    coverage_path = _write_coverage(
        plugin_root,
        {
            "lib/alpha_untested.py": 0.0,
            "lib/beta_low.py": 20.0,
        },
    )

    report = mod.find_test_gaps(
        plugin_root=plugin_root,
        threshold=60.0,
        coverage_json=coverage_path,
    )
    gap_paths = [gap["path"] for gap in report["gaps"]]

    assert gap_paths == [
        "lib/alpha_untested.py",
        "lib/beta_low.py",
        "hooks/runner.sh",
    ]
    assert [gap["priority_rank"] for gap in report["gaps"]] == [1, 2, 3]


def test_high_coverage_without_detected_reference_is_not_untested(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(plugin_root / "lib" / "well_covered.py", "def run_it():\n    return 7\n")
    _write(plugin_root / "tests" / "test_placeholder.py", "def test_placeholder():\n    assert True\n")

    coverage_path = _write_coverage(
        plugin_root,
        {
            "lib/well_covered.py": 93.0,
        },
    )

    report = mod.find_test_gaps(
        plugin_root=plugin_root,
        threshold=60.0,
        coverage_json=coverage_path,
    )
    by_path = {gap["path"]: gap for gap in report["gaps"]}

    assert "lib/well_covered.py" not in by_path


def test_renderers_and_cli_json_output(tmp_path, capsys):
    plugin_root = tmp_path / "compound-learning"
    _write(plugin_root / "lib" / "only.py", "def only():\n    return 1\n")
    _write_coverage(plugin_root, {"lib/only.py": 0.0})

    report = mod.find_test_gaps(
        plugin_root=plugin_root,
        threshold=60.0,
        coverage_json=plugin_root / "coverage.json",
    )

    text_output = mod.render_text_report(report)
    assert "Test Gap Finder" in text_output
    assert "lib/only.py" in text_output
    assert "untested" in text_output

    json_output = mod.render_json_report(report)
    payload = json.loads(json_output)
    assert payload["summary"]["gaps_found"] == 1

    exit_code = mod.main(
        [
            "--plugin-root",
            str(plugin_root),
            "--format",
            "json",
            "--threshold",
            "75",
        ]
    )
    assert exit_code == 0

    captured = capsys.readouterr()
    cli_payload = json.loads(captured.out)
    assert cli_payload["threshold"] == 75.0
    assert cli_payload["gaps"][0]["classification"] == "untested"
