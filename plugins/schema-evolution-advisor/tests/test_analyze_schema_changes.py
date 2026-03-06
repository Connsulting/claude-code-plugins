import importlib.util
import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.schema_analysis as schema_analysis


CLI_SPEC = importlib.util.spec_from_file_location(
    "schema_evolution_cli",
    PLUGIN_ROOT / "scripts" / "analyze-schema-changes.py",
)
CLI = importlib.util.module_from_spec(CLI_SPEC)
sys.modules[CLI_SPEC.name] = CLI
CLI_SPEC.loader.exec_module(CLI)


def test_split_sql_statements_handles_comments_and_dollar_quotes():
    sql_text = """
-- this semicolon should be ignored ;
CREATE TABLE users (id int primary key);
/* block comment ; should be ignored */
INSERT INTO users VALUES ($$semi;inside$$);
ALTER TABLE users ADD COLUMN email text;
"""

    statements = schema_analysis.split_sql_statements(sql_text)

    assert len(statements) == 3
    assert statements[0].text.startswith("CREATE TABLE users")
    assert statements[1].text.startswith("INSERT INTO users")
    assert statements[2].text.startswith("ALTER TABLE users")


def test_risk_classification_returns_high_medium_low_findings():
    sql_text = """
ALTER TABLE users DROP COLUMN legacy_name;
ALTER TABLE users ADD COLUMN slug text NOT NULL;
ALTER TABLE users RENAME COLUMN full_name TO display_name;
"""

    report = schema_analysis.analyze_schema_changes(
        [("migrations/001_users.sql", sql_text)],
        min_severity="low",
    )

    findings = report["findings"]
    severities = {finding["rule_id"]: finding["severity"] for finding in findings}

    assert severities["drop-column"] == "high"
    assert severities["add-not-null-no-default"] == "medium"
    assert severities["rename-column"] == "low"


def test_empty_input_returns_no_changes_status():
    report = schema_analysis.analyze_schema_changes([], min_severity="low")

    assert report["status"] == "no_changes"
    assert report["summary"]["files_scanned"] == 0
    assert report["findings"] == []


def test_cli_json_output_contract(tmp_path, monkeypatch, capsys):
    migration_file = tmp_path / "001_drop_table.sql"
    migration_file.write_text("DROP TABLE accounts;\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    exit_code = CLI.main(
        [
            "--format",
            "json",
            "--fail-on",
            "none",
            str(migration_file),
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == "findings"
    assert set(payload.keys()) >= {"status", "assumption", "summary", "files", "findings"}
    assert payload["findings"][0]["severity"] == "high"


def test_cli_text_output_contract(tmp_path, monkeypatch, capsys):
    migration_file = tmp_path / "002_rename_column.sql"
    migration_file.write_text(
        "ALTER TABLE users RENAME COLUMN full_name TO display_name;\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    exit_code = CLI.main(
        [
            "--format",
            "text",
            "--fail-on",
            "none",
            "--min-severity",
            "low",
            str(migration_file),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Schema Evolution Report" in captured.out
    assert "[LOW]" in captured.out
    assert "rename-column" in captured.out
