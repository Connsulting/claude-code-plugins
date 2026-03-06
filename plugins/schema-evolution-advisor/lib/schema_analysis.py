"""Static schema migration analysis for risky SQL evolution patterns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Dict, Iterable, List, Sequence

ASSUMPTION_TEXT = (
    "Assumption: analysis is static and SQL-only; runtime data distribution, lock impact, and "
    "application compatibility are not inspected."
)

ANALYSIS_VERSION = "1"

SEVERITY_ORDER = {
    "low": 1,
    "medium": 2,
    "high": 3,
}


@dataclass(frozen=True)
class SqlStatement:
    """A parsed SQL statement with its starting line number."""

    text: str
    start_line: int


@dataclass(frozen=True)
class Finding:
    """Represents one risky schema evolution pattern."""

    rule_id: str
    severity: str
    file_path: str
    line: int
    object_name: str | None
    message: str
    rationale: str
    mitigation: str
    statement_excerpt: str


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _severity_value(severity: str) -> int:
    return SEVERITY_ORDER.get(severity.lower(), 0)


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _excerpt(statement: str, limit: int = 200) -> str:
    collapsed = _collapse_whitespace(statement)
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3]}..."


def _normalize_identifier(name: str | None) -> str | None:
    if not name:
        return None

    cleaned = name.strip().strip(",").strip(";")
    if cleaned.startswith('"') and cleaned.endswith('"') and len(cleaned) > 1:
        cleaned = cleaned[1:-1]

    return cleaned or None


def _extract_table_name(statement: str) -> str | None:
    match = re.search(
        r"\bALTER\s+TABLE(?:\s+ONLY)?(?:\s+IF\s+EXISTS)?\s+([^\s,;]+)",
        statement,
        re.IGNORECASE,
    )
    return _normalize_identifier(match.group(1) if match else None)


def _extract_drop_table_name(statement: str) -> str | None:
    match = re.search(
        r"\bDROP\s+TABLE(?:\s+IF\s+EXISTS)?\s+([^\s,;]+)",
        statement,
        re.IGNORECASE,
    )
    return _normalize_identifier(match.group(1) if match else None)


def _extract_drop_type_name(statement: str) -> str | None:
    match = re.search(
        r"\bDROP\s+TYPE(?:\s+IF\s+EXISTS)?\s+([^\s,;]+)",
        statement,
        re.IGNORECASE,
    )
    return _normalize_identifier(match.group(1) if match else None)


def _extract_column_after_keyword(statement: str, keyword: str) -> str | None:
    pattern = rf"\b{keyword}\b(?:\s+IF\s+EXISTS)?\s+([^\s,;]+)"
    match = re.search(pattern, statement, re.IGNORECASE)
    return _normalize_identifier(match.group(1) if match else None)


def _extract_renamed_column(statement: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"\bRENAME\s+COLUMN\s+([^\s,;]+)\s+TO\s+([^\s,;]+)",
        statement,
        re.IGNORECASE,
    )
    if not match:
        return None, None

    old_name = _normalize_identifier(match.group(1))
    new_name = _normalize_identifier(match.group(2))
    return old_name, new_name


def _extract_renamed_table(statement: str) -> str | None:
    match = re.search(r"\bRENAME\s+TO\s+([^\s,;]+)", statement, re.IGNORECASE)
    return _normalize_identifier(match.group(1) if match else None)


def _extract_alter_column_type_target(statement: str) -> str | None:
    match = re.search(
        r"\bALTER\s+COLUMN\s+([^\s,;]+)\s+(?:SET\s+DATA\s+)?TYPE\b",
        statement,
        re.IGNORECASE,
    )
    return _normalize_identifier(match.group(1) if match else None)


def _extract_set_not_null_target(statement: str) -> str | None:
    match = re.search(
        r"\bALTER\s+COLUMN\s+([^\s,;]+)\s+SET\s+NOT\s+NULL\b",
        statement,
        re.IGNORECASE,
    )
    return _normalize_identifier(match.group(1) if match else None)


def _extract_added_column(statement: str) -> str | None:
    return _extract_column_after_keyword(statement, "ADD\\s+COLUMN")


def _has_prior_backfill(
    prior_statements: Sequence[SqlStatement],
    table_name: str | None,
    column_name: str | None,
) -> bool:
    if not table_name or not column_name:
        return False

    table_token = _collapse_whitespace(table_name).upper()
    column_token = _collapse_whitespace(column_name).upper()

    for prior in prior_statements:
        normalized = _collapse_whitespace(prior.text).upper()
        if f"UPDATE {table_token}" not in normalized:
            continue
        if " SET " not in normalized:
            continue
        if column_token in normalized:
            return True

    return False


def _build_finding(
    *,
    rule_id: str,
    severity: str,
    statement: SqlStatement,
    file_path: str,
    object_name: str | None,
    message: str,
    rationale: str,
    mitigation: str,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        file_path=file_path,
        line=statement.start_line,
        object_name=object_name,
        message=message,
        rationale=rationale,
        mitigation=mitigation,
        statement_excerpt=_excerpt(statement.text),
    )


def _classify_statement(
    *,
    statement: SqlStatement,
    file_path: str,
    prior_statements: Sequence[SqlStatement],
) -> List[Finding]:
    findings: List[Finding] = []
    normalized = _collapse_whitespace(statement.text)
    upper = normalized.upper()

    if re.search(r"\bDROP\s+TABLE\b", upper):
        table_name = _extract_drop_table_name(normalized)
        findings.append(
            _build_finding(
                rule_id="drop-table",
                severity="high",
                statement=statement,
                file_path=file_path,
                object_name=table_name,
                message="Dropping a table can irreversibly remove data and break dependent queries.",
                rationale="DDL drop operations are destructive and often incompatible with running code paths.",
                mitigation=(
                    "Prefer a phased deprecation (stop writes, backfill/archive, then drop in a later release) "
                    "or guard with an explicit rollback plan."
                ),
            )
        )

    if re.search(r"\bALTER\s+TABLE\b", upper) and re.search(r"\bDROP\s+COLUMN\b", upper):
        table_name = _extract_table_name(normalized)
        column_name = _extract_column_after_keyword(normalized, "DROP\\s+COLUMN")
        object_name = f"{table_name}.{column_name}" if table_name and column_name else table_name or column_name
        findings.append(
            _build_finding(
                rule_id="drop-column",
                severity="high",
                statement=statement,
                file_path=file_path,
                object_name=object_name,
                message="Dropping a column can remove data and break reads/writes still referencing it.",
                rationale="Column removal is a backward-incompatible contract change for application and analytics code.",
                mitigation=(
                    "Use additive migration first (new column + dual-write/read transition), then remove the old "
                    "column after consumers are migrated."
                ),
            )
        )

    if re.search(r"\bDROP\s+TYPE\b", upper):
        type_name = _extract_drop_type_name(normalized)
        findings.append(
            _build_finding(
                rule_id="drop-type",
                severity="high",
                statement=statement,
                file_path=file_path,
                object_name=type_name,
                message="Dropping a type can invalidate dependent columns, defaults, and functions.",
                rationale="Type deletion is usually a destructive dependency break in PostgreSQL-style schemas.",
                mitigation="Migrate dependents to a replacement type first, then drop after dependency checks pass.",
            )
        )

    if (
        re.search(r"\bALTER\s+TABLE\b", upper)
        and re.search(r"\bALTER\s+COLUMN\b", upper)
        and re.search(r"\b(?:SET\s+DATA\s+)?TYPE\b", upper)
    ):
        table_name = _extract_table_name(normalized)
        column_name = _extract_alter_column_type_target(normalized)
        object_name = f"{table_name}.{column_name}" if table_name and column_name else table_name or column_name
        findings.append(
            _build_finding(
                rule_id="alter-column-type",
                severity="high",
                statement=statement,
                file_path=file_path,
                object_name=object_name,
                message="Altering column type can fail casts or silently change semantics.",
                rationale="Type changes may rewrite data and break application/runtime assumptions.",
                mitigation=(
                    "Validate cast compatibility with representative data, stage via shadow column when possible, "
                    "and include explicit rollback handling."
                ),
            )
        )

    if (
        re.search(r"\bALTER\s+TABLE\b", upper)
        and re.search(r"\bALTER\s+COLUMN\b", upper)
        and re.search(r"\bSET\s+NOT\s+NULL\b", upper)
    ):
        table_name = _extract_table_name(normalized)
        column_name = _extract_set_not_null_target(normalized)
        has_backfill = _has_prior_backfill(prior_statements, table_name, column_name)
        object_name = f"{table_name}.{column_name}" if table_name and column_name else table_name or column_name
        findings.append(
            _build_finding(
                rule_id="set-not-null",
                severity="low" if has_backfill else "medium",
                statement=statement,
                file_path=file_path,
                object_name=object_name,
                message="Setting NOT NULL can fail if existing rows still contain null values.",
                rationale="Constraint tightening requires full data compliance before migration applies safely.",
                mitigation=(
                    "Backfill null rows first and verify with a validation query before applying SET NOT NULL."
                    if not has_backfill
                    else "Backfill detected earlier in file; still validate zero-null rows before deploy."
                ),
            )
        )

    if (
        re.search(r"\bALTER\s+TABLE\b", upper)
        and re.search(r"\bADD\s+COLUMN\b", upper)
        and re.search(r"\bNOT\s+NULL\b", upper)
        and not re.search(r"\bDEFAULT\b", upper)
    ):
        table_name = _extract_table_name(normalized)
        column_name = _extract_added_column(normalized)
        object_name = f"{table_name}.{column_name}" if table_name and column_name else table_name or column_name
        findings.append(
            _build_finding(
                rule_id="add-not-null-no-default",
                severity="medium",
                statement=statement,
                file_path=file_path,
                object_name=object_name,
                message="Adding a NOT NULL column without DEFAULT can fail on existing rows.",
                rationale="Existing data cannot satisfy a new required column unless values are backfilled.",
                mitigation="Add with a safe DEFAULT or nullable phase, backfill, then enforce NOT NULL in a follow-up migration.",
            )
        )

    if (
        re.search(r"\bALTER\s+TABLE\b", upper)
        and re.search(r"\bADD\b", upper)
        and re.search(r"\b(?:UNIQUE|PRIMARY\s+KEY|CHECK|FOREIGN\s+KEY)\b", upper)
    ):
        table_name = _extract_table_name(normalized)
        findings.append(
            _build_finding(
                rule_id="constraint-tightening",
                severity="medium",
                statement=statement,
                file_path=file_path,
                object_name=table_name,
                message="Adding constraints can reject existing data or new writes.",
                rationale="Constraint tightening is backward-incompatible when legacy records violate new rules.",
                mitigation="Validate existing data against the new constraint before rollout and sequence app changes accordingly.",
            )
        )

    if re.search(r"\bALTER\s+TABLE\b", upper) and re.search(r"\bRENAME\s+COLUMN\b", upper):
        table_name = _extract_table_name(normalized)
        old_name, new_name = _extract_renamed_column(normalized)
        object_name = table_name
        if table_name and old_name:
            object_name = f"{table_name}.{old_name}"
        findings.append(
            _build_finding(
                rule_id="rename-column",
                severity="low",
                statement=statement,
                file_path=file_path,
                object_name=object_name,
                message="Renaming a column can break callers using the old identifier.",
                rationale="Column renames are source-compatible only after all consumers are updated.",
                mitigation=(
                    f"Update all query callers and ORM mappings from '{old_name}' to '{new_name}' before deploy "
                    "or keep a compatibility layer during transition."
                    if old_name and new_name
                    else "Coordinate application updates with this migration and verify all consumers are updated."
                ),
            )
        )

    if re.search(r"\bALTER\s+TABLE\b", upper) and re.search(r"\bRENAME\s+TO\b", upper):
        table_name = _extract_table_name(normalized)
        new_name = _extract_renamed_table(normalized)
        findings.append(
            _build_finding(
                rule_id="rename-table",
                severity="low",
                statement=statement,
                file_path=file_path,
                object_name=table_name,
                message="Renaming a table can break queries, jobs, and permissions using the old name.",
                rationale="Table renames require coordinated updates across app code and operational tooling.",
                mitigation=(
                    f"Audit references and switch consumers from '{table_name}' to '{new_name}' in the same rollout window."
                    if table_name and new_name
                    else "Audit downstream dependencies and coordinate rename rollout with application updates."
                ),
            )
        )

    if re.search(r"\bALTER\s+TYPE\b", upper) and re.search(r"\bRENAME\s+VALUE\b", upper):
        findings.append(
            _build_finding(
                rule_id="rename-enum-value",
                severity="medium",
                statement=statement,
                file_path=file_path,
                object_name=None,
                message="Renaming enum values can break application constants and serialized payloads.",
                rationale="Enum literals are often hard-coded across services and data pipelines.",
                mitigation="Roll out compatible code changes first and validate all enum literal consumers before migration.",
            )
        )

    return findings


def split_sql_statements(sql_text: str) -> List[SqlStatement]:
    """Split SQL text into statements while handling comments and quoted strings."""

    statements: List[SqlStatement] = []
    buffer: List[str] = []

    line_number = 1
    statement_start_line = 1

    in_single_quote = False
    in_double_quote = False
    dollar_tag: str | None = None
    in_line_comment = False
    block_comment_depth = 0

    index = 0
    length = len(sql_text)

    while index < length:
        char = sql_text[index]
        next_char = sql_text[index + 1] if index + 1 < length else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                line_number += 1
                buffer.append(char)
            index += 1
            continue

        if block_comment_depth > 0:
            if char == "\n":
                line_number += 1
            if char == "/" and next_char == "*":
                block_comment_depth += 1
                index += 2
                continue
            if char == "*" and next_char == "/":
                block_comment_depth -= 1
                index += 2
                continue
            index += 1
            continue

        if dollar_tag and sql_text.startswith(dollar_tag, index):
            buffer.append(dollar_tag)
            index += len(dollar_tag)
            dollar_tag = None
            continue

        if not (in_single_quote or in_double_quote or dollar_tag):
            if char == "-" and next_char == "-":
                in_line_comment = True
                index += 2
                continue
            if char == "/" and next_char == "*":
                block_comment_depth = 1
                index += 2
                continue

        if char == "\n":
            line_number += 1

        if dollar_tag:
            buffer.append(char)
            index += 1
            continue

        if not (in_single_quote or in_double_quote):
            dollar_match = re.match(r"\$[A-Za-z_0-9]*\$", sql_text[index:])
            if dollar_match:
                dollar_tag = dollar_match.group(0)
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                continue

        if not in_double_quote and char == "'":
            if in_single_quote and next_char == "'":
                buffer.append("''")
                index += 2
                continue
            in_single_quote = not in_single_quote
            buffer.append(char)
            index += 1
            continue

        if not in_single_quote and char == '"':
            if in_double_quote and next_char == '"':
                buffer.append('""')
                index += 2
                continue
            in_double_quote = not in_double_quote
            buffer.append(char)
            index += 1
            continue

        if char == ";" and not (in_single_quote or in_double_quote or dollar_tag):
            raw = "".join(buffer)
            leading_whitespace = re.match(r"^\s*", raw).group(0)
            leading_line_offset = leading_whitespace.count("\n")
            text = raw.strip()

            if text:
                statements.append(
                    SqlStatement(text=text, start_line=statement_start_line + leading_line_offset)
                )

            buffer = []
            statement_start_line = line_number
            index += 1
            continue

        buffer.append(char)
        index += 1

    trailing = "".join(buffer)
    if trailing.strip():
        leading_whitespace = re.match(r"^\s*", trailing).group(0)
        leading_line_offset = leading_whitespace.count("\n")
        statements.append(
            SqlStatement(text=trailing.strip(), start_line=statement_start_line + leading_line_offset)
        )

    return statements


def analyze_sql_file(
    *,
    file_path: str,
    sql_text: str,
    min_severity: str = "low",
) -> Dict[str, Any]:
    """Analyze a SQL file and return findings filtered by minimum severity."""

    statements = split_sql_statements(sql_text)
    min_value = _severity_value(min_severity)

    findings: List[Finding] = []
    prior_statements: List[SqlStatement] = []

    for statement in statements:
        statement_findings = _classify_statement(
            statement=statement,
            file_path=file_path,
            prior_statements=prior_statements,
        )
        findings.extend(
            finding
            for finding in statement_findings
            if _severity_value(finding.severity) >= min_value
        )
        prior_statements.append(statement)

    findings.sort(
        key=lambda finding: (
            -_severity_value(finding.severity),
            finding.line,
            finding.rule_id,
        )
    )

    severity_counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        severity_counts[finding.severity] += 1

    return {
        "path": file_path,
        "statement_count": len(statements),
        "finding_count": len(findings),
        "by_severity": severity_counts,
        "findings": [
            {
                "rule_id": finding.rule_id,
                "severity": finding.severity,
                "file": finding.file_path,
                "line": finding.line,
                "object": finding.object_name,
                "message": finding.message,
                "rationale": finding.rationale,
                "mitigation": finding.mitigation,
                "statement_excerpt": finding.statement_excerpt,
            }
            for finding in findings
        ],
    }


def _sort_findings(findings: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        findings,
        key=lambda finding: (
            -_severity_value(str(finding.get("severity", ""))),
            str(finding.get("file", "")),
            int(finding.get("line", 0)),
            str(finding.get("rule_id", "")),
        ),
    )


def analyze_schema_changes(
    files: Sequence[tuple[str, str]],
    *,
    min_severity: str = "low",
    max_findings: int = 200,
) -> Dict[str, Any]:
    """Analyze multiple SQL files and return a deterministic report."""

    if max_findings < 1:
        raise ValueError("max_findings must be >= 1")

    files_report: List[Dict[str, Any]] = []
    all_findings: List[Dict[str, Any]] = []
    statements_scanned = 0

    for file_path, sql_text in files:
        file_report = analyze_sql_file(
            file_path=file_path,
            sql_text=sql_text,
            min_severity=min_severity,
        )
        statements_scanned += int(file_report["statement_count"])
        files_report.append(file_report)
        all_findings.extend(file_report["findings"])

    ordered_findings = _sort_findings(all_findings)
    reported_findings = ordered_findings[:max_findings]
    truncated = len(reported_findings) < len(ordered_findings)

    reported_keys = {
        (finding["file"], int(finding["line"]), finding["rule_id"], finding["message"])
        for finding in reported_findings
    }

    visible_files: List[Dict[str, Any]] = []
    for file_report in files_report:
        visible = [
            finding
            for finding in file_report["findings"]
            if (
                finding["file"],
                int(finding["line"]),
                finding["rule_id"],
                finding["message"],
            )
            in reported_keys
        ]
        visible_files.append(
            {
                "path": file_report["path"],
                "statement_count": file_report["statement_count"],
                "finding_count": len(visible),
                "findings": _sort_findings(visible),
            }
        )

    by_severity = {"high": 0, "medium": 0, "low": 0}
    for finding in ordered_findings:
        severity = str(finding.get("severity", "")).lower()
        if severity in by_severity:
            by_severity[severity] += 1

    if not files:
        status = "no_changes"
    elif ordered_findings:
        status = "findings"
    else:
        status = "ok"

    return {
        "analysis_version": ANALYSIS_VERSION,
        "generated_at": _now_utc_iso(),
        "assumption": ASSUMPTION_TEXT,
        "status": status,
        "summary": {
            "files_scanned": len(files),
            "files_with_findings": sum(1 for file_report in files_report if file_report["finding_count"] > 0),
            "statements_scanned": statements_scanned,
            "total_findings": len(ordered_findings),
            "reported_findings": len(reported_findings),
            "truncated": truncated,
            "minimum_severity": min_severity,
            "by_severity": by_severity,
        },
        "files": visible_files,
        "findings": reported_findings,
    }
