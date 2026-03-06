#!/usr/bin/env python3
"""Analyze static observability instrumentation coverage."""

from __future__ import annotations

import argparse
import ast
import json
import shlex
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


DEFAULT_SCAN_ROOT = Path(__file__).resolve().parents[1]
DYNAMIC_STATUS = "<dynamic>"
NON_TERMINAL_STATUSES = {"start"}

_STATUS_ALIASES = {
    "begin": "start",
    "began": "start",
    "in_progress": "start",
    "inprogress": "start",
    "pending": "start",
    "running": "start",
    "started": "start",
    "complete": "success",
    "completed": "success",
    "done": "success",
    "ok": "success",
    "succeeded": "success",
    "errored": "failure",
    "failed": "failure",
    "fail": "failure",
    "ignore": "skipped",
    "ignored": "skipped",
    "no_op": "skipped",
    "noop": "skipped",
    "skip": "skipped",
}


@dataclass(frozen=True)
class InstrumentationEvent:
    file_path: str
    source: str
    line: int
    operation: str
    status: str
    raw_status: str
    dynamic_status: bool = False


def normalize_status(raw_status: str) -> str:
    normalized = raw_status.strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return "unknown"
    return _STATUS_ALIASES.get(normalized, normalized)


def is_terminal_status(status: str) -> bool:
    return status not in NON_TERMINAL_STATUSES and status != DYNAMIC_STATUS


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _string_literal(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _PythonLoggerEmitVisitor(ast.NodeVisitor):
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.events: List[InstrumentationEvent] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr == "emit":
            owner = function.value
            if isinstance(owner, ast.Name) and owner.id == "logger":
                if len(node.args) >= 2:
                    operation = _string_literal(node.args[0])
                    raw_status = _string_literal(node.args[1])
                    if operation and raw_status:
                        self.events.append(
                            InstrumentationEvent(
                                file_path=self.file_path,
                                source="python",
                                line=node.lineno,
                                operation=operation.strip(),
                                status=normalize_status(raw_status),
                                raw_status=raw_status.strip(),
                            )
                        )
        self.generic_visit(node)


def collect_python_events(path: Path, scan_root: Path) -> Tuple[List[InstrumentationEvent], Optional[str]]:
    file_path = _relative_path(path, scan_root)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"{file_path}: {exc}"

    try:
        tree = ast.parse(content, filename=str(path))
    except SyntaxError as exc:
        line = exc.lineno or 1
        return [], f"{file_path}:{line}: syntax error: {exc.msg}"

    visitor = _PythonLoggerEmitVisitor(file_path)
    visitor.visit(tree)
    return visitor.events, None


def _iter_shell_logical_lines(content: str) -> Iterator[Tuple[int, str]]:
    start_line = 1
    parts: List[str] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.rstrip()
        if not parts:
            start_line = line_number

        if stripped.endswith("\\"):
            parts.append(stripped[:-1])
            continue

        parts.append(stripped)
        joined = " ".join(piece.strip() for piece in parts if piece.strip())
        if joined:
            yield start_line, joined
        parts = []

    if parts:
        joined = " ".join(piece.strip() for piece in parts if piece.strip())
        if joined:
            yield start_line, joined


def _is_dynamic_shell_token(token: str) -> bool:
    return "$" in token


def collect_shell_events(path: Path, scan_root: Path) -> Tuple[List[InstrumentationEvent], Optional[str]]:
    file_path = _relative_path(path, scan_root)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"{file_path}: {exc}"

    events: List[InstrumentationEvent] = []
    for line_number, logical_line in _iter_shell_logical_lines(content):
        try:
            tokens = shlex.split(logical_line, comments=True, posix=True)
        except ValueError:
            continue

        if len(tokens) < 4 or tokens[0] != "hook_obs_event":
            continue

        operation = tokens[2].strip()
        if not operation or _is_dynamic_shell_token(operation):
            continue

        raw_status = tokens[3].strip()
        dynamic_status = _is_dynamic_shell_token(raw_status)
        normalized_status = DYNAMIC_STATUS if dynamic_status else normalize_status(raw_status)

        events.append(
            InstrumentationEvent(
                file_path=file_path,
                source="shell",
                line=line_number,
                operation=operation,
                status=normalized_status,
                raw_status=raw_status,
                dynamic_status=dynamic_status,
            )
        )

    return events, None


def _percent(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((part / total) * 100.0, 2)


def _summarize_operation_map(
    operation_statuses: Dict[str, Iterable[str]],
) -> Dict[str, object]:
    operation_rows = []
    missing_terminal_operations = []
    terminal_count = 0

    for operation in sorted(operation_statuses):
        statuses = sorted(set(operation_statuses[operation]))
        has_terminal = any(is_terminal_status(status) for status in statuses)
        if has_terminal:
            terminal_count += 1
        else:
            missing_terminal_operations.append(operation)
        operation_rows.append(
            {
                "operation": operation,
                "statuses": statuses,
                "has_terminal_status": has_terminal,
            }
        )

    operation_count = len(operation_rows)
    return {
        "instrumented_operations": operation_count,
        "operations_with_terminal_status": terminal_count,
        "coverage_percent": _percent(terminal_count, operation_count),
        "missing_terminal_operations": missing_terminal_operations,
        "operations": operation_rows,
    }


def analyze_scan_root(scan_root: Path) -> Dict[str, object]:
    python_files = sorted(scan_root.rglob("*.py"))
    shell_files = sorted(scan_root.rglob("*.sh"))

    all_events: List[InstrumentationEvent] = []
    parse_errors: List[str] = []

    for path in python_files:
        events, error = collect_python_events(path, scan_root)
        all_events.extend(events)
        if error:
            parse_errors.append(error)

    for path in shell_files:
        events, error = collect_shell_events(path, scan_root)
        all_events.extend(events)
        if error:
            parse_errors.append(error)

    by_file: DefaultDict[str, DefaultDict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    overall_operations: DefaultDict[str, set[str]] = defaultdict(set)
    source_counts: DefaultDict[str, int] = defaultdict(int)

    for event in all_events:
        by_file[event.file_path][event.operation].add(event.status)
        overall_operations[event.operation].add(event.status)
        source_counts[event.source] += 1

    file_summaries = []
    for file_path in sorted(by_file):
        operation_map = by_file[file_path]
        operation_summary = _summarize_operation_map(operation_map)
        file_summaries.append(
            {
                "path": file_path,
                **operation_summary,
            }
        )

    hotspots = sorted(
        (
            {
                "path": file_summary["path"],
                "instrumented_operations": file_summary["instrumented_operations"],
                "operations_with_terminal_status": file_summary["operations_with_terminal_status"],
                "coverage_percent": file_summary["coverage_percent"],
                "missing_terminal_operations": file_summary["missing_terminal_operations"],
            }
            for file_summary in file_summaries
            if file_summary["missing_terminal_operations"]
        ),
        key=lambda row: (
            -len(row["missing_terminal_operations"]),
            row["coverage_percent"],
            row["path"],
        ),
    )

    report: Dict[str, object] = {
        "scan_root": str(scan_root.resolve()),
        "events_analyzed": len(all_events),
        "source_counts": {
            "python": source_counts.get("python", 0),
            "shell": source_counts.get("shell", 0),
        },
        "overall": _summarize_operation_map(overall_operations),
        "files": file_summaries,
        "hotspots": hotspots,
        "parse_errors": sorted(parse_errors),
    }
    return report


def _status_index(operation_rows: List[Dict[str, object]]) -> Dict[str, List[str]]:
    indexed: Dict[str, List[str]] = {}
    for row in operation_rows:
        operation = str(row["operation"])
        statuses = [str(status) for status in row.get("statuses", [])]
        indexed[operation] = statuses
    return indexed


def render_text_report(report: Dict[str, object]) -> str:
    overall = report["overall"]  # type: ignore[assignment]
    source_counts = report["source_counts"]  # type: ignore[assignment]
    hotspots = report["hotspots"]  # type: ignore[assignment]
    parse_errors = report["parse_errors"]  # type: ignore[assignment]

    if not isinstance(overall, dict):
        raise TypeError("report.overall must be a dictionary")
    if not isinstance(source_counts, dict):
        raise TypeError("report.source_counts must be a dictionary")
    if not isinstance(hotspots, list):
        raise TypeError("report.hotspots must be a list")
    if not isinstance(parse_errors, list):
        raise TypeError("report.parse_errors must be a list")

    operation_rows = overall.get("operations", [])
    if not isinstance(operation_rows, list):
        operation_rows = []
    status_by_operation = _status_index(operation_rows)

    lines = [
        "Metrics Coverage Analyzer",
        f"Scan root: {report['scan_root']}",
        (
            "Events analyzed: "
            f"{report['events_analyzed']} "
            f"(python: {source_counts.get('python', 0)}, shell: {source_counts.get('shell', 0)})"
        ),
        (
            "Overall terminal coverage: "
            f"{overall.get('operations_with_terminal_status', 0)}/"
            f"{overall.get('instrumented_operations', 0)} operations "
            f"({overall.get('coverage_percent', 0.0):.2f}%)"
        ),
    ]

    missing_operations = overall.get("missing_terminal_operations", [])
    if isinstance(missing_operations, list) and missing_operations:
        lines.append("Operations missing terminal status:")
        for operation in missing_operations:
            statuses = status_by_operation.get(str(operation), [])
            status_label = ", ".join(statuses) if statuses else "none"
            lines.append(f"  - {operation} [{status_label}]")
    else:
        lines.append("Operations missing terminal status: none")

    lines.append("Coverage hotspots:")
    if hotspots:
        for hotspot in hotspots:
            if not isinstance(hotspot, dict):
                continue
            missing = hotspot.get("missing_terminal_operations", [])
            missing_label = ", ".join(str(item) for item in missing) if missing else "none"
            lines.append(
                "  - "
                f"{hotspot.get('path')}: "
                f"{hotspot.get('operations_with_terminal_status', 0)}/"
                f"{hotspot.get('instrumented_operations', 0)} "
                f"({hotspot.get('coverage_percent', 0.0):.2f}%), "
                f"missing: {missing_label}"
            )
    else:
        lines.append("  - none")

    if parse_errors:
        lines.append("Parse errors:")
        for error in parse_errors:
            lines.append(f"  - {error}")

    return "\n".join(lines)


def _threshold_percent(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:  # pragma: no cover - argparse handles presentation
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0.0 or parsed > 100.0:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return parsed


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scan-root",
        default=str(DEFAULT_SCAN_ROOT),
        help="Directory to scan recursively (default: plugin root).",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    parser.add_argument(
        "--fail-under",
        type=_threshold_percent,
        default=None,
        help="Fail with a non-zero exit code if overall coverage is below this percent.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    scan_root = Path(args.scan_root).expanduser()
    if not scan_root.is_dir():
        print(f"Scan root does not exist or is not a directory: {scan_root}", file=sys.stderr)
        return 1

    report = analyze_scan_root(scan_root)
    coverage = float(report["overall"]["coverage_percent"])  # type: ignore[index]
    threshold_result = None
    if args.fail_under is not None:
        threshold_result = {
            "fail_under": round(args.fail_under, 2),
            "passed": coverage >= args.fail_under,
        }
        report["threshold"] = threshold_result

    if args.output == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text_report(report))

    if threshold_result and not threshold_result["passed"]:
        if args.output == "text":
            print(
                (
                    f"Threshold check failed: overall coverage {coverage:.2f}% "
                    f"is below {args.fail_under:.2f}%"
                ),
                file=sys.stderr,
            )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
