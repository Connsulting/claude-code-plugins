import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "logging-quality-auditor.py"

_spec = importlib.util.spec_from_file_location("logging_quality_auditor", SCRIPT_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules["logging_quality_auditor"] = mod
_spec.loader.exec_module(mod)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def _findings_for_path(report: dict, rel_path: str) -> list[dict]:
    return [finding for finding in report["findings"] if finding["path"] == rel_path]


def test_detects_expected_logging_audit_findings(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(
        plugin_root / "scripts" / "silent.py",
        """
        import subprocess

        def run():
            try:
                subprocess.run(["tool"], check=True)
            except subprocess.CalledProcessError:
                return False
        """,
    )
    _write(
        plugin_root / "scripts" / "generic.py",
        """
        from pathlib import Path
        import sys

        def load():
            try:
                Path("payload.txt").read_text(encoding="utf-8")
            except OSError:
                print("error", file=sys.stderr)
                return None
        """,
    )
    _write(
        plugin_root / "scripts" / "mixed.py",
        """
        import json

        def main():
            print(json.dumps({"status": "ok"}))
            print("done")
        """,
    )
    _write(
        plugin_root / "hooks" / "silent.sh",
        """
        #!/usr/bin/env bash
        if ! timeout 5 claude -p "hello"; then
          exit 0
        fi
        """,
    )
    _write(
        plugin_root / "hooks" / "pure-data.py",
        """
        def emit(value: str) -> None:
            print(value)
        """,
    )

    report = mod.audit_logging_quality(plugin_root)
    findings_by_path = {
        path: findings[0]
        for path in ("scripts/silent.py", "scripts/generic.py", "scripts/mixed.py", "hooks/silent.sh")
        for findings in [_findings_for_path(report, path)]
    }

    assert findings_by_path["scripts/silent.py"]["classification"] == "missing_exception_diagnostics"
    assert findings_by_path["scripts/silent.py"]["severity"] == "high"
    assert findings_by_path["scripts/generic.py"]["classification"] == "low_context_diagnostic"
    assert findings_by_path["scripts/generic.py"]["severity"] == "low"
    assert findings_by_path["scripts/mixed.py"]["classification"] == "mixed_machine_output"
    assert findings_by_path["scripts/mixed.py"]["severity"] == "medium"
    assert findings_by_path["hooks/silent.sh"]["classification"] == "missing_failure_diagnostics"
    assert not _findings_for_path(report, "hooks/pure-data.py")


def test_prioritization_is_deterministic(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(
        plugin_root / "scripts" / "alpha.py",
        """
        import subprocess

        def run():
            try:
                subprocess.run(["tool"], check=True)
            except subprocess.CalledProcessError:
                return False
        """,
    )
    _write(
        plugin_root / "hooks" / "beta.sh",
        """
        #!/usr/bin/env bash
        if ! timeout 5 python3 worker.py; then
          exit 1
        fi
        """,
    )
    _write(
        plugin_root / "scripts" / "gamma.py",
        """
        import json

        def emit():
            print(json.dumps({"status": "ok"}))
            print("human summary")
        """,
    )
    _write(
        plugin_root / "scripts" / "omega.py",
        """
        from pathlib import Path
        import sys

        def load():
            try:
                Path("payload.txt").read_text(encoding="utf-8")
            except OSError:
                print("warning", file=sys.stderr)
                return None
        """,
    )

    report = mod.audit_logging_quality(plugin_root)

    assert [(finding["path"], finding["classification"]) for finding in report["findings"]] == [
        ("scripts/alpha.py", "missing_exception_diagnostics"),
        ("hooks/beta.sh", "missing_failure_diagnostics"),
        ("scripts/gamma.py", "mixed_machine_output"),
        ("scripts/omega.py", "low_context_diagnostic"),
    ]
    assert [finding["priority_rank"] for finding in report["findings"]] == [1, 2, 3, 4]


def test_stdout_and_structured_error_contracts_avoid_false_positives(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(
        plugin_root / "scripts" / "stdout-human.py",
        """
        from pathlib import Path

        def load(target_path: str):
            try:
                Path(target_path).read_text(encoding="utf-8")
            except OSError as e:
                print(f"[ERROR] Failed to load {target_path}: {e}")
                return None
        """,
    )
    _write(
        plugin_root / "skills" / "audit" / "returned-error.py",
        """
        import lib.db as db

        def action_get(config):
            try:
                return db.get_connection(config)
            except Exception as e:
                return {"status": "error", "message": f"Failed to open database: {e}"}
        """,
    )
    _write(
        plugin_root / "skills" / "audit" / "mutated-error.py",
        """
        import lib.db as db

        def action_delete(config):
            results = {"status": "success", "errors": []}

            try:
                return db.get_connection(config)
            except Exception as e:
                results["status"] = "error"
                results["errors"].append(str(e))
                return results
        """,
    )

    report = mod.audit_logging_quality(plugin_root)

    assert not _findings_for_path(report, "scripts/stdout-human.py")
    assert not _findings_for_path(report, "skills/audit/returned-error.py")
    assert not _findings_for_path(report, "skills/audit/mutated-error.py")


def test_dynamic_f_string_context_is_not_low_context(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(
        plugin_root / "scripts" / "dynamic-context.py",
        """
        from pathlib import Path
        import sys

        def save(target_path: str):
            try:
                Path(target_path).write_text("payload", encoding="utf-8")
            except OSError as e:
                print(f"warning {target_path}: {e}", file=sys.stderr)
                return False
        """,
    )

    report = mod.audit_logging_quality(plugin_root)

    assert not _findings_for_path(report, "scripts/dynamic-context.py")


def test_contextual_stderr_diagnostics_clear_exception_handler_findings(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(
        plugin_root / "hooks" / "contextual.py",
        """
        import sys

        def extract_context(transcript_path: str):
            try:
                with open(transcript_path, "r", encoding="utf-8") as handle:
                    return handle.read()
            except (FileNotFoundError, PermissionError) as exc:
                print(
                    f"failed to read transcript {transcript_path}: {exc}",
                    file=sys.stderr,
                )
                return ""
        """,
    )
    _write(
        plugin_root / "lib" / "gitdir.py",
        """
        import sys
        from pathlib import Path

        def read_gitdir(git_file: Path):
            try:
                return git_file.read_text().strip()
            except OSError as exc:
                print(
                    f"failed to read gitdir marker {git_file}: {exc}",
                    file=sys.stderr,
                )
                return None
        """,
    )

    report = mod.audit_logging_quality(plugin_root)

    assert not _findings_for_path(report, "hooks/contextual.py")
    assert not _findings_for_path(report, "lib/gitdir.py")


def test_conditional_reraise_still_flags_silent_branch(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(
        plugin_root / "scripts" / "conditional.py",
        """
        import subprocess

        def run(debug: bool):
            try:
                subprocess.run(["tool"], check=True)
            except subprocess.CalledProcessError:
                if debug:
                    raise
                return False
        """,
    )

    report = mod.audit_logging_quality(plugin_root)
    findings = _findings_for_path(report, "scripts/conditional.py")

    assert len(findings) == 1
    assert findings[0]["classification"] == "missing_exception_diagnostics"
    assert findings[0]["severity"] == "high"


def test_recovery_handler_without_terminal_suppression_is_not_flagged(tmp_path):
    plugin_root = tmp_path / "compound-learning"

    _write(
        plugin_root / "lib" / "fallback.py",
        """
        try:
            import sqlite3
            _test_conn = sqlite3.connect(":memory:")
            if not hasattr(_test_conn, "enable_load_extension"):
                raise AttributeError("no extension loading")
        except AttributeError:
            import pysqlite3 as sqlite3
        """,
    )

    report = mod.audit_logging_quality(plugin_root)

    assert not _findings_for_path(report, "lib/fallback.py")


def test_renderers_and_cli_fail_on(tmp_path, capsys):
    plugin_root = tmp_path / "compound-learning"
    _write(
        plugin_root / "scripts" / "silent.py",
        """
        import subprocess

        def run():
            try:
                subprocess.run(["tool"], check=True)
            except subprocess.CalledProcessError:
                return False
        """,
    )

    report = mod.audit_logging_quality(plugin_root, fail_on="high")
    text_output = mod.render_text_report(report)
    assert "Logging Quality Auditor" in text_output
    assert "scripts/silent.py" in text_output
    assert "missing_exception_diagnostics" in text_output

    json_output = mod.render_json_report(report)
    payload = json.loads(json_output)
    assert payload["gate"]["triggered"] is True
    assert payload["summary"]["findings_found"] == 1

    exit_code = mod.main(
        [
            "--plugin-root",
            str(plugin_root),
            "--format",
            "json",
            "--fail-on",
            "high",
        ]
    )
    assert exit_code == 1

    captured = capsys.readouterr()
    cli_payload = json.loads(captured.out)
    assert cli_payload["gate"]["fail_on"] == "high"
    assert cli_payload["findings"][0]["classification"] == "missing_exception_diagnostics"


def test_prefers_git_tracked_files_when_available(tmp_path):
    plugin_root = tmp_path / "compound-learning"
    _write(
        plugin_root / "scripts" / "tracked.py",
        """
        import subprocess

        def run():
            try:
                subprocess.run(["tool"], check=True)
            except subprocess.CalledProcessError:
                return False
        """,
    )
    _write(
        plugin_root / "scripts" / "untracked.py",
        """
        import subprocess

        def run():
            try:
                subprocess.run(["ignored"], check=True)
            except subprocess.CalledProcessError:
                return False
        """,
    )

    subprocess.run(["git", "init"], cwd=plugin_root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "scripts/tracked.py"], cwd=plugin_root, check=True, capture_output=True, text=True)

    report = mod.audit_logging_quality(plugin_root)

    assert report["summary"]["source_files"] == 1
    assert [finding["path"] for finding in report["findings"]] == ["scripts/tracked.py"]
