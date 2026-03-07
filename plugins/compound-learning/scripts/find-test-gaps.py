#!/usr/bin/env python3
"""
Find test-coverage gaps for the compound-learning plugin.

This analyzer combines coverage.py JSON output with static test-reference
heuristics so we can identify source files that are untested, low coverage,
or only indirectly exercised.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

DEFAULT_THRESHOLD = 60.0
DEFAULT_TESTS_DIR = "tests"
SCOPE_GLOBS = (
    "hooks/*.py",
    "hooks/*.sh",
    "lib/*.py",
    "lib/*.sh",
    "scripts/*.py",
    "skills/*/*.py",
)
CATEGORY_PRIORITY = {
    "untested": 0,
    "low_coverage": 1,
    "indirectly_tested": 2,
}
INVOCATION_CALL_SUFFIXES = (
    ".run",
    ".Popen",
    ".check_call",
    ".check_output",
    ".call",
    ".system",
    ".popen",
    ".spec_from_file_location",
)
INVOCATION_CALL_NAMES = {
    "run",
    "Popen",
    "check_call",
    "check_output",
    "call",
    "system",
    "popen",
    "spec_from_file_location",
}


@dataclass(frozen=True)
class SourceFile:
    abs_path: Path
    rel_path: str
    language: str
    module_candidates: tuple[str, ...]


@dataclass(frozen=True)
class ParsedTestFile:
    rel_path: str
    imported_modules: frozenset[str]
    literals: tuple[str, ...]
    invocation_literals: tuple[str, ...]
    text: str


def _normalize_text_path(value: str) -> str:
    return value.replace("\\", "/")


def _module_candidates(rel_path: str) -> tuple[str, ...]:
    module_path = Path(rel_path)
    if module_path.suffix != ".py":
        return tuple()

    parts = [part.replace("-", "_") for part in module_path.with_suffix("").parts]
    if not parts:
        return tuple()
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return tuple()

    dotted = ".".join(parts)
    stem = parts[-1]

    ordered: List[str] = []
    for candidate in (dotted, stem):
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return tuple(ordered)


def discover_source_files(plugin_root: Path, patterns: Sequence[str] = SCOPE_GLOBS) -> List[SourceFile]:
    discovered: Dict[str, SourceFile] = {}

    for pattern in patterns:
        for path in sorted(plugin_root.glob(pattern)):
            if not path.is_file():
                continue
            rel_path = path.relative_to(plugin_root).as_posix()
            if rel_path in discovered:
                continue
            language = "python" if path.suffix == ".py" else "shell"
            discovered[rel_path] = SourceFile(
                abs_path=path,
                rel_path=rel_path,
                language=language,
                module_candidates=_module_candidates(rel_path),
            )

    return [discovered[key] for key in sorted(discovered)]


def discover_test_files(plugin_root: Path, tests_dir: str = DEFAULT_TESTS_DIR) -> List[Path]:
    tests_root = (plugin_root / tests_dir).resolve()
    if not tests_root.exists() or not tests_root.is_dir():
        return []

    files = [
        path
        for path in tests_root.rglob("*.py")
        if path.is_file() and (path.name.startswith("test_") or path.name.endswith("_test.py"))
    ]
    return sorted(files, key=lambda item: item.as_posix())


def _normalize_coverage_path(raw_path: str, plugin_root: Path) -> str | None:
    normalized = _normalize_text_path(raw_path).lstrip("./")
    if not normalized:
        return None

    path = Path(normalized)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(plugin_root.resolve()).as_posix()
        except ValueError:
            return None

    scoped_prefixes = (
        "plugins/compound-learning/",
        f"{plugin_root.name}/",
    )
    for prefix in scoped_prefixes:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break

    candidate = (plugin_root / normalized).resolve()
    if candidate.exists():
        try:
            return candidate.relative_to(plugin_root.resolve()).as_posix()
        except ValueError:
            return None
    return normalized


def _coverage_percent(file_payload: Mapping[str, Any]) -> float | None:
    summary = file_payload.get("summary")
    if isinstance(summary, Mapping):
        percent = summary.get("percent_covered")
        if percent is not None:
            try:
                return float(percent)
            except (TypeError, ValueError):
                pass

        covered = summary.get("covered_lines")
        statements = summary.get("num_statements")
        if covered is not None and statements:
            try:
                return (float(covered) / float(statements)) * 100.0
            except (TypeError, ValueError, ZeroDivisionError):
                pass

    executed_lines = file_payload.get("executed_lines")
    missing_lines = file_payload.get("missing_lines")
    if isinstance(executed_lines, list) and isinstance(missing_lines, list):
        total = len(executed_lines) + len(missing_lines)
        if total == 0:
            return 0.0
        return (len(executed_lines) / total) * 100.0

    return None


def load_coverage_snapshot(coverage_json: Path | None, plugin_root: Path) -> tuple[Dict[str, float], bool]:
    if coverage_json is None or not coverage_json.exists():
        return {}, False

    try:
        payload = json.loads(coverage_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, False

    files = payload.get("files")
    if not isinstance(files, Mapping):
        return {}, False

    coverage_by_file: Dict[str, float] = {}
    for raw_path, file_payload in files.items():
        if not isinstance(raw_path, str) or not isinstance(file_payload, Mapping):
            continue
        rel_path = _normalize_coverage_path(raw_path, plugin_root)
        if not rel_path:
            continue
        percent = _coverage_percent(file_payload)
        if percent is None:
            continue
        current = coverage_by_file.get(rel_path)
        if current is None or percent > current:
            coverage_by_file[rel_path] = percent

    return coverage_by_file, True


def _extract_string_literals(node: ast.AST) -> List[str]:
    literals: List[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Constant(self, const_node: ast.Constant) -> Any:
            if isinstance(const_node.value, str):
                literals.append(_normalize_text_path(const_node.value))
            self.generic_visit(const_node)

    Visitor().visit(node)
    return literals


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parent = _call_name(func.value)
        if parent:
            return f"{parent}.{func.attr}"
        return func.attr
    return ""


def _is_invocation_call(call_name: str) -> bool:
    if not call_name:
        return False
    if call_name in INVOCATION_CALL_NAMES:
        return True
    if any(call_name.endswith(suffix) for suffix in INVOCATION_CALL_SUFFIXES):
        return True
    return False


def parse_test_file(test_path: Path, plugin_root: Path) -> ParsedTestFile:
    text = test_path.read_text(encoding="utf-8")
    imported_modules: set[str] = set()
    literals: List[str] = []
    invocation_literals: List[str] = []

    try:
        tree = ast.parse(text, filename=test_path.as_posix())
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module)
                for alias in node.names:
                    if alias.name == "*" or not node.module:
                        continue
                    imported_modules.add(f"{node.module}.{alias.name}")
            elif isinstance(node, ast.Call):
                call_literals = _extract_string_literals(node)
                literals.extend(call_literals)
                if _is_invocation_call(_call_name(node.func)):
                    invocation_literals.extend(call_literals)

    rel_path = test_path.relative_to(plugin_root).as_posix()
    return ParsedTestFile(
        rel_path=rel_path,
        imported_modules=frozenset(imported_modules),
        literals=tuple(literals),
        invocation_literals=tuple(invocation_literals),
        text=text,
    )


def _path_reference(source: SourceFile, values: Iterable[str]) -> bool:
    normalized_values = [_normalize_text_path(value) for value in values if value]
    if not normalized_values:
        return False

    if any(source.rel_path in value for value in normalized_values):
        return True

    source_dir = source.rel_path.split("/", maxsplit=1)[0]
    basename = Path(source.rel_path).name
    combined = " ".join(normalized_values)

    if f"{source_dir}/{basename}" in combined:
        return True
    if source_dir in normalized_values and basename in normalized_values:
        return True

    return False


def _module_reference(source: SourceFile, test_file: ParsedTestFile) -> bool:
    if source.language != "python":
        return False

    imported_modules = test_file.imported_modules
    for candidate in source.module_candidates:
        if candidate in imported_modules:
            return True
        if any(module.startswith(f"{candidate}.") for module in imported_modules):
            return True
        if re.search(rf"\b{re.escape(candidate)}\b", test_file.text):
            return True
    return False


def _indirect_module_reference(source: SourceFile, invocation_literals: Iterable[str]) -> bool:
    if source.language != "python":
        return False
    for value in invocation_literals:
        for candidate in source.module_candidates:
            if candidate and candidate in value:
                return True
    return False


def collect_test_references(
    plugin_root: Path,
    source_files: Sequence[SourceFile],
    tests_dir: str = DEFAULT_TESTS_DIR,
) -> tuple[Dict[str, List[str]], Dict[str, List[str]], int]:
    parsed_tests = [parse_test_file(path, plugin_root) for path in discover_test_files(plugin_root, tests_dir)]

    direct_refs: Dict[str, set[str]] = {source.rel_path: set() for source in source_files}
    indirect_refs: Dict[str, set[str]] = {source.rel_path: set() for source in source_files}

    for source in source_files:
        for test_file in parsed_tests:
            direct_match = _module_reference(source, test_file) or _path_reference(source, test_file.literals)
            indirect_match = _path_reference(source, test_file.invocation_literals) or _indirect_module_reference(
                source,
                test_file.invocation_literals,
            )

            if direct_match:
                direct_refs[source.rel_path].add(test_file.rel_path)
            if indirect_match:
                indirect_refs[source.rel_path].add(test_file.rel_path)

    normalized_direct = {path: sorted(refs) for path, refs in direct_refs.items()}
    normalized_indirect = {path: sorted(refs) for path, refs in indirect_refs.items()}
    return normalized_direct, normalized_indirect, len(parsed_tests)


def _classify_gap(
    coverage_pct: float | None,
    has_test_reference: bool,
    has_indirect_reference: bool,
    threshold: float,
) -> tuple[str | None, str | None]:
    if has_indirect_reference and (coverage_pct is None or coverage_pct == 0.0):
        return (
            "indirectly_tested",
            "Referenced via subprocess/path invocation, but no measured line coverage.",
        )

    reasons: List[str] = []
    if coverage_pct == 0.0:
        reasons.append("0% coverage")
    if not has_test_reference:
        reasons.append("no test reference")
    if reasons:
        return "untested", " and ".join(reasons).capitalize() + "."

    if coverage_pct is not None and coverage_pct < threshold:
        return (
            "low_coverage",
            f"Coverage {coverage_pct:.1f}% is below the {threshold:.1f}% threshold.",
        )

    return None, None


def _round_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


def find_test_gaps(
    plugin_root: Path,
    threshold: float = DEFAULT_THRESHOLD,
    coverage_json: Path | None = None,
    tests_dir: str = DEFAULT_TESTS_DIR,
) -> Dict[str, Any]:
    source_files = discover_source_files(plugin_root)
    direct_refs, indirect_refs, test_file_count = collect_test_references(
        plugin_root=plugin_root,
        source_files=source_files,
        tests_dir=tests_dir,
    )
    coverage_by_file, coverage_loaded = load_coverage_snapshot(coverage_json, plugin_root)

    gaps: List[Dict[str, Any]] = []
    category_counts = {
        "untested": 0,
        "low_coverage": 0,
        "indirectly_tested": 0,
    }
    files_with_refs = 0
    files_with_indirect_refs = 0

    for source in source_files:
        direct = direct_refs.get(source.rel_path, [])
        indirect = indirect_refs.get(source.rel_path, [])
        has_reference = bool(direct or indirect)
        has_indirect_reference = bool(indirect)
        if has_reference:
            files_with_refs += 1
        if has_indirect_reference:
            files_with_indirect_refs += 1

        coverage_pct: float | None = None
        if source.language == "python":
            if coverage_loaded:
                coverage_pct = coverage_by_file.get(source.rel_path, 0.0)
            else:
                coverage_pct = None

        classification, reason = _classify_gap(
            coverage_pct=coverage_pct,
            has_test_reference=has_reference,
            has_indirect_reference=has_indirect_reference,
            threshold=threshold,
        )
        if not classification:
            continue

        category_counts[classification] += 1
        gaps.append(
            {
                "path": source.rel_path,
                "classification": classification,
                "coverage_pct": _round_pct(coverage_pct),
                "test_references": direct,
                "indirect_test_references": indirect,
                "reason": reason,
            }
        )

    def sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        coverage = item.get("coverage_pct")
        coverage_sort = float(coverage) if coverage is not None else -1.0
        return (
            CATEGORY_PRIORITY[item["classification"]],
            coverage_sort,
            item["path"],
        )

    gaps.sort(key=sort_key)
    for index, gap in enumerate(gaps, start=1):
        gap["priority_rank"] = index

    coverage_source = str(coverage_json.resolve()) if coverage_json else None
    report: Dict[str, Any] = {
        "scope": {
            "plugin_root": str(plugin_root.resolve()),
            "patterns": list(SCOPE_GLOBS),
            "tests_dir": tests_dir,
        },
        "threshold": round(float(threshold), 2),
        "coverage": {
            "path": coverage_source,
            "loaded": coverage_loaded,
        },
        "summary": {
            "source_files": len(source_files),
            "test_files": test_file_count,
            "files_with_test_references": files_with_refs,
            "files_with_indirect_references": files_with_indirect_refs,
            "gaps_found": len(gaps),
            "by_category": category_counts,
        },
        "gaps": gaps,
    }
    return report


def render_json_report(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def _coverage_display(coverage_pct: float | None) -> str:
    if coverage_pct is None:
        return "n/a"
    return f"{coverage_pct:.1f}%"


def render_text_report(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    by_category = summary["by_category"]
    lines = [
        "Test Gap Finder",
        f"Plugin Root: {report['scope']['plugin_root']}",
        f"Coverage JSON: {report['coverage']['path'] or 'none'} (loaded={report['coverage']['loaded']})",
        f"Threshold: {report['threshold']:.1f}%",
        "",
        "Summary:",
        f"- Source files: {summary['source_files']}",
        f"- Test files: {summary['test_files']}",
        f"- Gaps found: {summary['gaps_found']}",
        f"- Untested: {by_category['untested']}",
        f"- Low coverage: {by_category['low_coverage']}",
        f"- Indirectly tested: {by_category['indirectly_tested']}",
    ]

    gaps = report["gaps"]
    if gaps:
        lines.append("")
        lines.append("Prioritized gaps:")
        for gap in gaps:
            lines.append(
                (
                    f"{gap['priority_rank']:>2}. {gap['classification']:<17} "
                    f"{gap['path']} | coverage={_coverage_display(gap['coverage_pct'])} "
                    f"| refs={len(gap['test_references'])} | indirect_refs={len(gap['indirect_test_references'])}"
                )
            )
            lines.append(f"    reason: {gap['reason']}")
    else:
        lines.append("")
        lines.append("No gaps matched the configured rules.")

    return "\n".join(lines)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Identify test gaps in compound-learning source files.")
    parser.add_argument(
        "--plugin-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Plugin root directory (default: script parent/..).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="Low-coverage threshold percentage (default: 60).",
    )
    parser.add_argument(
        "--coverage-json",
        default=None,
        help="coverage.py JSON path (default: <plugin-root>/coverage.json when present).",
    )
    parser.add_argument(
        "--tests-dir",
        default=DEFAULT_TESTS_DIR,
        help="Tests directory relative to plugin root (default: tests).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output file path. Rendered report still prints to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    plugin_root = Path(args.plugin_root).expanduser().resolve()
    if not plugin_root.exists() or not plugin_root.is_dir():
        print(f"error: plugin root not found: {plugin_root}", file=sys.stderr)
        return 2

    if args.threshold < 0:
        print("error: --threshold must be >= 0", file=sys.stderr)
        return 2

    if args.coverage_json:
        coverage_json = Path(args.coverage_json).expanduser().resolve()
    else:
        default_coverage = plugin_root / "coverage.json"
        coverage_json = default_coverage if default_coverage.exists() else None

    report = find_test_gaps(
        plugin_root=plugin_root,
        threshold=args.threshold,
        coverage_json=coverage_json,
        tests_dir=args.tests_dir,
    )

    if args.format == "json":
        output = render_json_report(report)
    else:
        output = render_text_report(report)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
