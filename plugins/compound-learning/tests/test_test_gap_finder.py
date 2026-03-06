import importlib.util
import json
import sys
import textwrap
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "test-gap-finder.py"

_spec = importlib.util.spec_from_file_location("test_gap_finder", SCRIPT_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules["test_gap_finder"] = mod
_spec.loader.exec_module(mod)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def test_discovery_respects_python_scope_and_test_patterns(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(plugin_root / "lib" / "alpha.py", "def alpha():\n    return 1\n")
    _write(plugin_root / "lib" / "__init__.py", "")
    _write(plugin_root / "scripts" / "runner.py", "def run():\n    return True\n")
    _write(plugin_root / "hooks" / "watch.py", "def watch():\n    return None\n")
    _write(plugin_root / "skills" / "sync" / "sync.py", "def sync():\n    return None\n")
    _write(plugin_root / "skills" / "sync" / "README.md", "not python")
    _write(plugin_root / "tests" / "test_alpha.py", "import lib.alpha\n")
    _write(plugin_root / "tests" / "runner_test.py", "import scripts.runner\n")
    _write(plugin_root / "tests" / "helper.py", "# not a test file\n")

    modules = mod.discover_source_modules(plugin_root)
    rel_paths = [module.rel_path for module in modules]

    assert rel_paths == [
        "hooks/watch.py",
        "lib/alpha.py",
        "scripts/runner.py",
        "skills/sync/sync.py",
    ]

    tests = mod.discover_test_files(plugin_root)
    test_paths = [path.relative_to(plugin_root).as_posix() for path in tests]
    assert test_paths == ["tests/runner_test.py", "tests/test_alpha.py"]


def test_extracts_public_symbols_and_function_inventory(tmp_path):
    plugin_root = tmp_path / "compound-learning"
    _write(
        plugin_root / "lib" / "symbols.py",
        """
        def public_fn():
            return True

        def _private_fn():
            return False

        class Widget:
            def run(self):
                return 1

            def _hidden(self):
                return 0
        """,
    )

    modules = mod.discover_source_modules(plugin_root, source_dirs=("lib",))
    assert len(modules) == 1

    module = modules[0]
    assert module.public_symbols == ("Widget", "public_fn")

    function_names = [fn.qualified_name for fn in module.functions]
    assert function_names == [
        "public_fn",
        "_private_fn",
        "Widget.run",
        "Widget._hidden",
    ]


def test_ranking_prefers_untested_and_uncovered_modules(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(
        plugin_root / "lib" / "covered.py",
        """
        def covered_public():
            return "covered"

        def another_public():
            return "more"
        """,
    )
    _write(
        plugin_root / "lib" / "untested.py",
        """
        def untested_public():
            return "untested"
        """,
    )

    _write(
        plugin_root / "tests" / "test_covered.py",
        """
        import lib.covered as covered

        def test_covered_public():
            assert covered.covered_public() == "covered"
        """,
    )

    _write(
        plugin_root / "coverage.xml",
        """
        <?xml version="1.0" ?>
        <coverage>
          <packages>
            <package name="lib">
              <classes>
                <class name="covered.py" filename="lib/covered.py">
                  <lines>
                    <line number="1" hits="1"/>
                    <line number="2" hits="1"/>
                    <line number="4" hits="0"/>
                    <line number="5" hits="0"/>
                  </lines>
                </class>
                <class name="untested.py" filename="lib/untested.py">
                  <lines>
                    <line number="1" hits="0"/>
                    <line number="2" hits="0"/>
                  </lines>
                </class>
              </classes>
            </package>
          </packages>
        </coverage>
        """,
    )

    report = mod.analyze_test_gaps(
        plugin_root=plugin_root,
        source_dirs=("lib",),
        tests_dir="tests",
        coverage_xml=plugin_root / "coverage.xml",
        top_modules=10,
        top_functions=10,
    )

    assert report["summary"]["coverage_loaded"] is True
    ranked_modules = report["prioritized_gaps"]["modules"]
    assert ranked_modules[0]["module"] == "lib/untested.py"
    assert ranked_modules[0]["score"] > ranked_modules[1]["score"]
    assert ranked_modules[0]["evidence"]["tested_by"] == []
    assert ranked_modules[0]["evidence"]["missing_symbols"] == ["untested_public"]


def test_coverage_basename_normalization_for_source_dirs(tmp_path):
    plugin_root = tmp_path / "compound-learning"
    _write(plugin_root / "lib" / "db.py", "def load_config():\n    return {}\n")

    _write(
        plugin_root / "coverage.xml",
        """
        <?xml version="1.0" ?>
        <coverage>
          <packages>
            <package name="lib">
              <classes>
                <class name="db.py" filename="db.py">
                  <lines>
                    <line number="1" hits="0"/>
                    <line number="2" hits="1"/>
                  </lines>
                </class>
              </classes>
            </package>
          </packages>
        </coverage>
        """,
    )

    report = mod.analyze_test_gaps(
        plugin_root=plugin_root,
        source_dirs=("lib",),
        tests_dir="tests",
        coverage_xml=plugin_root / "coverage.xml",
    )

    first_module = report["prioritized_gaps"]["modules"][0]
    assert first_module["module"] == "lib/db.py"
    assert first_module["evidence"]["coverage_pct"] == 50.0


def test_report_schema_and_json_stability(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(plugin_root / "lib" / "single.py", "def run_once():\n    return 1\n")
    _write(plugin_root / "tests" / "test_single.py", "import lib.single\n")

    report = mod.analyze_test_gaps(
        plugin_root=plugin_root,
        source_dirs=("lib",),
        tests_dir="tests",
        coverage_xml=None,
        top_modules=5,
        top_functions=5,
    )

    json_a = mod.serialize_report(report)
    json_b = mod.serialize_report(report)
    assert json_a == json_b

    payload = json.loads(json_a)
    assert set(payload.keys()) == {
        "assumptions",
        "limitations",
        "prioritized_gaps",
        "report_version",
        "scope",
        "summary",
    }

    first_module = payload["prioritized_gaps"]["modules"][0]
    assert set(first_module.keys()) == {
        "evidence",
        "module",
        "priority",
        "score",
        "suggested_next_tests",
    }
    assert "coverage_pct" in first_module["evidence"]
    assert "tested_by" in first_module["evidence"]
    assert "missing_symbols" in first_module["evidence"]
    assert isinstance(first_module["suggested_next_tests"], list)
