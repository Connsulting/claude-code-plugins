#!/usr/bin/env python3
"""
Find and rank testing gaps for the compound-learning plugin.

The report is deterministic for the same input tree and optional coverage.xml.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

DEFAULT_SOURCE_DIRS = ("lib", "scripts", "hooks", "skills")
DEFAULT_TESTS_DIR = "tests"
COVERAGE_PREFIX = "plugins/compound-learning/"


@dataclass(frozen=True)
class SourceFunction:
    module_rel_path: str
    name: str
    qualified_name: str
    lineno: int
    end_lineno: int
    is_public: bool


@dataclass(frozen=True)
class SourceModule:
    abs_path: Path
    rel_path: str
    import_candidates: tuple[str, ...]
    public_symbols: tuple[str, ...]
    functions: tuple[SourceFunction, ...]


@dataclass(frozen=True)
class TestReference:
    abs_path: Path
    rel_path: str
    imported_modules: frozenset[str]
    imported_names: frozenset[str]
    symbol_refs: frozenset[str]
    module_path_refs: frozenset[str]
    text: str


@dataclass(frozen=True)
class CoverageData:
    by_module_line_hits: Mapping[str, Mapping[int, int]]
    loaded: bool
    source: str | None


def _round_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


def discover_python_files(base_dir: Path, relative_dirs: Sequence[str]) -> List[Path]:
    """Return sorted Python files found under the provided directories."""
    files: List[Path] = []
    for rel_dir in relative_dirs:
        root = (base_dir / rel_dir).resolve()
        if not root.exists() or not root.is_dir():
            continue
        files.extend(path for path in root.rglob("*.py") if path.is_file())
    return sorted(set(files), key=lambda p: p.as_posix())


def discover_test_files(plugin_root: Path, tests_dir: str = DEFAULT_TESTS_DIR) -> List[Path]:
    """Discover pytest-style test files."""
    tests_root = (plugin_root / tests_dir).resolve()
    if not tests_root.exists() or not tests_root.is_dir():
        return []

    files = [
        path
        for path in tests_root.rglob("*.py")
        if path.is_file() and (path.name.startswith("test_") or path.name.endswith("_test.py"))
    ]
    return sorted(files, key=lambda p: p.as_posix())


def module_import_candidates(relative_path: str) -> tuple[str, ...]:
    """Return likely import identifiers for a module path."""
    rel = Path(relative_path)
    parts = list(rel.with_suffix("").parts)
    if not parts:
        return tuple()

    if parts[-1] == "__init__":
        parts = parts[:-1]

    if not parts:
        return tuple()

    normalized = [part.replace("-", "_") for part in parts]
    dotted = ".".join(normalized)
    stem = normalized[-1]

    candidates = [dotted, stem]
    unique: List[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def _extract_module_symbols(module_path: Path, module_rel_path: str) -> tuple[tuple[str, ...], tuple[SourceFunction, ...]]:
    """Extract public symbols and callable ranges from a module AST."""
    try:
        source = module_path.read_text(encoding="utf-8")
    except OSError:
        return tuple(), tuple()

    try:
        tree = ast.parse(source, filename=module_path.as_posix())
    except SyntaxError:
        return tuple(), tuple()

    public_symbols: List[str] = []
    functions: List[SourceFunction] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_public = not node.name.startswith("_")
            if is_public:
                public_symbols.append(node.name)
            functions.append(
                SourceFunction(
                    module_rel_path=module_rel_path,
                    name=node.name,
                    qualified_name=node.name,
                    lineno=int(node.lineno),
                    end_lineno=int(getattr(node, "end_lineno", node.lineno)),
                    is_public=is_public,
                )
            )
            continue

        if isinstance(node, ast.ClassDef):
            class_is_public = not node.name.startswith("_")
            if class_is_public:
                public_symbols.append(node.name)

            for class_node in node.body:
                if isinstance(class_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_is_public = not class_node.name.startswith("_")
                    functions.append(
                        SourceFunction(
                            module_rel_path=module_rel_path,
                            name=class_node.name,
                            qualified_name=f"{node.name}.{class_node.name}",
                            lineno=int(class_node.lineno),
                            end_lineno=int(getattr(class_node, "end_lineno", class_node.lineno)),
                            is_public=method_is_public,
                        )
                    )

    unique_public_symbols = tuple(sorted(dict.fromkeys(public_symbols)))
    ordered_functions = tuple(
        sorted(functions, key=lambda fn: (fn.lineno, fn.end_lineno, fn.qualified_name))
    )
    return unique_public_symbols, ordered_functions


def discover_source_modules(
    plugin_root: Path,
    source_dirs: Sequence[str] = DEFAULT_SOURCE_DIRS,
) -> List[SourceModule]:
    """Discover plugin source modules and extracted symbol inventories."""
    modules: List[SourceModule] = []
    for source_file in discover_python_files(plugin_root, source_dirs):
        rel_path = source_file.relative_to(plugin_root).as_posix()
        if Path(rel_path).name == "__init__.py":
            continue

        public_symbols, functions = _extract_module_symbols(source_file, rel_path)
        modules.append(
            SourceModule(
                abs_path=source_file,
                rel_path=rel_path,
                import_candidates=module_import_candidates(rel_path),
                public_symbols=public_symbols,
                functions=functions,
            )
        )

    return sorted(modules, key=lambda module: module.rel_path)


def _collect_name_refs(tree: ast.AST) -> frozenset[str]:
    names: set[str] = set()

    class Collector(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> Any:
            names.add(node.id)
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> Any:
            names.add(node.attr)
            self.generic_visit(node)

    Collector().visit(tree)
    return frozenset(names)


def _extract_dynamic_import_module(call: ast.Call) -> str | None:
    if call.args:
        first_arg = call.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            if isinstance(call.func, ast.Attribute) and call.func.attr == "import_module":
                return first_arg.value
            if isinstance(call.func, ast.Name) and call.func.id == "__import__":
                return first_arg.value
    return None


def _extract_string_literals(node: ast.AST) -> List[str]:
    literals: List[str] = []

    class Collector(ast.NodeVisitor):
        def visit_Constant(self, const_node: ast.Constant) -> Any:
            if isinstance(const_node.value, str):
                literals.append(const_node.value)
            self.generic_visit(const_node)

    Collector().visit(node)
    return literals


def _extract_module_path_from_expr(
    expr: ast.AST,
    named_path_refs: Mapping[str, str] | None = None,
) -> str | None:
    if named_path_refs and isinstance(expr, ast.Name):
        return named_path_refs.get(expr.id)

    raw_literals: List[str]
    if isinstance(expr, ast.Constant):
        if not isinstance(expr.value, str):
            return None
        raw_literals = [expr.value]
    else:
        raw_literals = _extract_string_literals(expr)

    literals = [value.replace("\\", "/").strip("/") for value in raw_literals if value.strip("/")]
    if not literals:
        return None

    for index, value in enumerate(literals):
        if value in DEFAULT_SOURCE_DIRS:
            candidate_parts = [part for part in literals[index:] if part]
            if candidate_parts and candidate_parts[-1].endswith(".py"):
                return "/".join(candidate_parts)
    return None


def _collect_named_path_refs(tree: ast.AST) -> Dict[str, str]:
    named_refs: Dict[str, str] = {}

    if not isinstance(tree, ast.Module):
        return named_refs

    for node in tree.body:
        value_node: ast.AST | None = None
        targets: List[ast.AST] = []

        if isinstance(node, ast.Assign):
            value_node = node.value
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            value_node = node.value
            targets = [node.target]

        if value_node is None:
            continue

        resolved_path = _extract_module_path_from_expr(value_node)
        if not resolved_path:
            continue

        for target in targets:
            if isinstance(target, ast.Name):
                named_refs[target.id] = resolved_path

    return named_refs


def _extract_module_path_ref(call: ast.Call, named_path_refs: Mapping[str, str]) -> str | None:
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "spec_from_file_location"):
        return None
    if len(call.args) < 2:
        return None
    return _extract_module_path_from_expr(call.args[1], named_path_refs=named_path_refs)


def parse_test_reference(test_path: Path, plugin_root: Path) -> TestReference:
    """Parse import and symbol hints from a test file."""
    text = test_path.read_text(encoding="utf-8")

    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    symbol_refs: frozenset[str] = frozenset()
    module_path_refs: set[str] = set()

    try:
        tree = ast.parse(text, filename=test_path.as_posix())
        symbol_refs = _collect_name_refs(tree)
        named_path_refs = _collect_named_path_refs(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
                    imported_names.add(alias.name.split(".")[-1])
                    if alias.asname:
                        imported_names.add(alias.asname)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    if node.module:
                        imported_modules.add(f"{node.module}.{alias.name}")
                    imported_names.add(alias.name.split(".")[-1])
                    if alias.asname:
                        imported_names.add(alias.asname)
            elif isinstance(node, ast.Call):
                dynamic_module = _extract_dynamic_import_module(node)
                if dynamic_module:
                    imported_modules.add(dynamic_module)
                module_path_ref = _extract_module_path_ref(node, named_path_refs)
                if module_path_ref:
                    module_path_refs.add(module_path_ref)
    except SyntaxError:
        # Keep fallback text matching when AST parsing fails.
        pass

    return TestReference(
        abs_path=test_path,
        rel_path=test_path.relative_to(plugin_root).as_posix(),
        imported_modules=frozenset(imported_modules),
        imported_names=frozenset(imported_names),
        symbol_refs=symbol_refs,
        module_path_refs=frozenset(module_path_refs),
        text=text,
    )


def collect_test_references(plugin_root: Path, tests_dir: str = DEFAULT_TESTS_DIR) -> List[TestReference]:
    """Discover and parse all test references under tests_dir."""
    return [parse_test_reference(path, plugin_root) for path in discover_test_files(plugin_root, tests_dir)]


def module_test_matches(module: SourceModule, test_ref: TestReference) -> bool:
    """Heuristic: determine whether a test directly targets a source module."""
    imported_modules = test_ref.imported_modules

    for candidate in module.import_candidates:
        if candidate in imported_modules:
            return True
        if any(item.startswith(f"{candidate}.") for item in imported_modules):
            return True

    if module.rel_path in test_ref.module_path_refs:
        return True

    return False


def symbol_test_matches(symbol: str, test_ref: TestReference) -> bool:
    """Determine whether a symbol is referenced by a test file."""
    direct = symbol
    suffix = symbol.split(".")[-1]

    if direct in test_ref.symbol_refs or suffix in test_ref.symbol_refs:
        return True

    for token in (direct, suffix):
        if not token:
            continue
        if re.search(rf"\b{re.escape(token)}\b", test_ref.text):
            return True

    return False


def normalize_coverage_filename(raw_filename: str, plugin_root: Path) -> str | None:
    """Normalize coverage.xml class filename to plugin-relative path."""
    raw = raw_filename.replace("\\", "/")

    if raw.startswith(COVERAGE_PREFIX):
        raw = raw[len(COVERAGE_PREFIX) :]

    path = Path(raw)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(plugin_root.resolve()).as_posix()
        except ValueError:
            return None

    stripped = raw.lstrip("./")
    candidate = (plugin_root / stripped).resolve()
    if candidate.exists():
        try:
            return candidate.relative_to(plugin_root.resolve()).as_posix()
        except ValueError:
            return None

    # coverage.py can emit basenames only when source paths are configured as directories
    # (for example "db.py" instead of "lib/db.py"). Resolve unique matches in source dirs.
    if stripped and "/" not in stripped:
        matches = [
            path
            for path in plugin_root.rglob(stripped)
            if path.is_file()
            and path.suffix == ".py"
            and path.relative_to(plugin_root).parts
            and path.relative_to(plugin_root).parts[0] in DEFAULT_SOURCE_DIRS
        ]
        unique = sorted({path.relative_to(plugin_root).as_posix() for path in matches})
        if len(unique) == 1:
            return unique[0]

    return stripped if stripped else None


def parse_coverage_xml(coverage_xml: Path | None, plugin_root: Path) -> CoverageData:
    """Parse coverage line hit counts keyed by plugin-relative module path."""
    if coverage_xml is None or not coverage_xml.exists():
        return CoverageData(by_module_line_hits={}, loaded=False, source=None)

    try:
        root = ET.parse(coverage_xml).getroot()
    except ET.ParseError:
        return CoverageData(by_module_line_hits={}, loaded=False, source=str(coverage_xml))

    by_module: Dict[str, Dict[int, int]] = {}

    for class_node in root.findall(".//class"):
        filename = class_node.attrib.get("filename", "")
        rel = normalize_coverage_filename(filename, plugin_root)
        if not rel:
            continue

        line_hits = by_module.setdefault(rel, {})
        for line_node in class_node.findall("./lines/line"):
            try:
                lineno = int(line_node.attrib["number"])
            except (KeyError, ValueError):
                continue
            try:
                hits = int(line_node.attrib.get("hits", "0"))
            except ValueError:
                hits = 0
            current = line_hits.get(lineno)
            if current is None or hits > current:
                line_hits[lineno] = hits

    normalized: Dict[str, Mapping[int, int]] = {
        key: dict(sorted(value.items())) for key, value in sorted(by_module.items())
    }
    return CoverageData(
        by_module_line_hits=normalized,
        loaded=True,
        source=str(coverage_xml),
    )


def coverage_pct_from_hits(line_hits: Mapping[int, int] | None) -> float | None:
    if line_hits is None:
        return None
    if not line_hits:
        return 0.0

    total = len(line_hits)
    covered = sum(1 for hits in line_hits.values() if hits > 0)
    return (covered / total) * 100.0 if total else 0.0


def function_coverage_pct(
    function: SourceFunction,
    module_line_hits: Mapping[int, int] | None,
    coverage_loaded: bool,
) -> float | None:
    if not coverage_loaded:
        return None

    if module_line_hits is None:
        return 0.0

    relevant_hits = {
        lineno: hits
        for lineno, hits in module_line_hits.items()
        if function.lineno <= lineno <= function.end_lineno
    }
    return coverage_pct_from_hits(relevant_hits)


def coverage_penalty(coverage_pct: float | None) -> int:
    if coverage_pct is None:
        return 20
    if coverage_pct < 10:
        return 40
    if coverage_pct < 30:
        return 30
    if coverage_pct < 60:
        return 20
    if coverage_pct < 80:
        return 10
    return 0


def classify_priority(score: int) -> str:
    if score >= 75:
        return "high"
    if score >= 45:
        return "medium"
    return "low"


def _suggest_module_tests(module: SourceModule, missing_symbols: Sequence[str], tested_by: Sequence[str]) -> List[str]:
    module_name = module.rel_path
    suggestions: List[str] = []

    if not tested_by:
        if missing_symbols:
            targets = ", ".join(missing_symbols[:3])
            suggestions.append(f"Create a direct test module for {module_name} covering: {targets}.")
        else:
            suggestions.append(f"Create a direct test module for {module_name} with at least one behavior-level test.")
    elif missing_symbols:
        targets = ", ".join(missing_symbols[:3])
        suggestions.append(f"Add focused tests for uncovered public symbols in {module_name}: {targets}.")

    if not suggestions:
        suggestions.append(f"Increase assertion depth in existing tests for {module_name}.")
    return suggestions


def _suggest_function_tests(module_rel_path: str, function_name: str) -> List[str]:
    return [f"Add/extend a test that calls {function_name} in {module_rel_path} and asserts expected side effects."]


def analyze_test_gaps(
    plugin_root: Path,
    source_dirs: Sequence[str] = DEFAULT_SOURCE_DIRS,
    tests_dir: str = DEFAULT_TESTS_DIR,
    coverage_xml: Path | None = None,
    top_modules: int = 20,
    top_functions: int = 40,
) -> Dict[str, Any]:
    """Compute and rank module/function test gaps."""
    modules = discover_source_modules(plugin_root, source_dirs=source_dirs)
    test_refs = collect_test_references(plugin_root, tests_dir=tests_dir)
    coverage = parse_coverage_xml(coverage_xml, plugin_root)

    module_gaps: List[Dict[str, Any]] = []
    function_gaps: List[Dict[str, Any]] = []

    for module in modules:
        module_tests = sorted(
            [test.rel_path for test in test_refs if module_test_matches(module, test)]
        )
        module_test_refs = [test for test in test_refs if test.rel_path in module_tests]

        missing_symbols = sorted(
            [
                symbol
                for symbol in module.public_symbols
                if not any(symbol_test_matches(symbol, test) for test in module_test_refs)
            ]
        )

        module_line_hits = coverage.by_module_line_hits.get(module.rel_path)
        module_coverage_pct = (
            coverage_pct_from_hits(module_line_hits or {}) if coverage.loaded else None
        )

        untested_function_count = 0
        for function in module.functions:
            function_tests = sorted(
                [
                    test.rel_path
                    for test in module_test_refs
                    if symbol_test_matches(function.qualified_name, test)
                    or symbol_test_matches(function.name, test)
                ]
            )
            if not function_tests:
                untested_function_count += 1

            function_pct = function_coverage_pct(function, module_line_hits, coverage.loaded)

            function_score = 0
            function_score += 40 if not function_tests else 5
            function_score += coverage_penalty(function_pct)
            if function.is_public and not function_tests:
                function_score += 15
            elif not function.is_public and not function_tests:
                function_score += 5
            function_score = min(100, function_score)

            function_gaps.append(
                {
                    "module": module.rel_path,
                    "function": function.qualified_name,
                    "score": function_score,
                    "priority": classify_priority(function_score),
                    "evidence": {
                        "coverage_pct": _round_pct(function_pct),
                        "tested_by": function_tests,
                        "missing_symbols": [function.qualified_name] if not function_tests else [],
                        "line_range": [function.lineno, function.end_lineno],
                        "is_public": function.is_public,
                    },
                    "suggested_next_tests": _suggest_function_tests(module.rel_path, function.qualified_name),
                }
            )

        public_symbol_count = len(module.public_symbols)
        missing_symbol_ratio = (len(missing_symbols) / public_symbol_count) if public_symbol_count else 0.0

        module_score = 0
        module_score += 45 if not module_tests else 5
        module_score += coverage_penalty(module_coverage_pct)
        module_score += int(round(30 * missing_symbol_ratio))
        module_score += min(20, untested_function_count * 4)
        module_score = min(100, module_score)

        module_gaps.append(
            {
                "module": module.rel_path,
                "score": module_score,
                "priority": classify_priority(module_score),
                "evidence": {
                    "coverage_pct": _round_pct(module_coverage_pct),
                    "tested_by": module_tests,
                    "missing_symbols": missing_symbols,
                    "public_symbol_count": public_symbol_count,
                    "untested_public_symbol_count": len(missing_symbols),
                    "untested_function_count": untested_function_count,
                },
                "suggested_next_tests": _suggest_module_tests(module, missing_symbols, module_tests),
            }
        )

    module_gaps.sort(key=lambda item: (-item["score"], item["module"]))
    function_gaps.sort(key=lambda item: (-item["score"], item["module"], item["function"]))

    total_modules = len(modules)
    modules_with_tests = sum(1 for gap in module_gaps if gap["evidence"]["tested_by"])
    modules_without_tests = total_modules - modules_with_tests

    coverage_values = [
        gap["evidence"]["coverage_pct"]
        for gap in module_gaps
        if gap["evidence"]["coverage_pct"] is not None
    ]
    overall_coverage = sum(coverage_values) / len(coverage_values) if coverage_values else None

    top_module_gaps = module_gaps[: max(0, top_modules)]
    top_function_gaps = function_gaps[: max(0, top_functions)]

    report: Dict[str, Any] = {
        "report_version": 1,
        "scope": {
            "plugin_root": plugin_root.resolve().as_posix(),
            "source_dirs": list(source_dirs),
            "tests_dir": tests_dir,
            "coverage_xml": coverage.source,
        },
        "summary": {
            "source_modules": total_modules,
            "test_files": len(test_refs),
            "modules_with_direct_tests": modules_with_tests,
            "modules_without_direct_tests": modules_without_tests,
            "coverage_loaded": coverage.loaded,
            "overall_module_coverage_pct": _round_pct(overall_coverage),
            "high_priority_module_gaps": sum(1 for gap in module_gaps if gap["priority"] == "high"),
            "high_priority_function_gaps": sum(1 for gap in function_gaps if gap["priority"] == "high"),
        },
        "prioritized_gaps": {
            "modules": top_module_gaps,
            "functions": top_function_gaps,
        },
        "assumptions": [
            "Gap scoring targets plugin Python runtime code only (lib/scripts/hooks/skills).",
            "Test linkage uses static import/path/symbol heuristics and may under-report dynamic test coverage.",
            "Function-level coverage is approximated from coverage.xml line hits inside AST function ranges.",
        ],
        "limitations": [
            "No branch coverage scoring is applied.",
            "References inferred by symbol names can miss indirect execution paths.",
            "Shell/markdown assets are intentionally excluded from scoring.",
        ],
    }
    return report


def serialize_report(report: Mapping[str, Any]) -> str:
    """Serialize report with stable formatting."""
    return json.dumps(report, indent=2, sort_keys=True)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Identify and rank under-tested modules/functions in compound-learning.",
    )
    parser.add_argument(
        "--plugin-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Plugin root directory (default: script parent/..)",
    )
    parser.add_argument(
        "--coverage-xml",
        default=None,
        help="Optional coverage.xml path. Defaults to <plugin-root>/coverage.xml if present.",
    )
    parser.add_argument(
        "--tests-dir",
        default=DEFAULT_TESTS_DIR,
        help="Tests directory relative to plugin root (default: tests)",
    )
    parser.add_argument(
        "--top-modules",
        type=int,
        default=20,
        help="Max module gaps to include (default: 20)",
    )
    parser.add_argument(
        "--top-functions",
        type=int,
        default=40,
        help="Max function gaps to include (default: 40)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output file path. JSON is always printed to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    plugin_root = Path(args.plugin_root).expanduser().resolve()
    if not plugin_root.exists() or not plugin_root.is_dir():
        print(
            json.dumps(
                {
                    "status": "error",
                    "message": f"plugin root not found: {plugin_root}",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    coverage_path: Path | None
    if args.coverage_xml:
        coverage_path = Path(args.coverage_xml).expanduser().resolve()
    else:
        default_coverage = plugin_root / "coverage.xml"
        coverage_path = default_coverage if default_coverage.exists() else None

    report = analyze_test_gaps(
        plugin_root=plugin_root,
        source_dirs=DEFAULT_SOURCE_DIRS,
        tests_dir=args.tests_dir,
        coverage_xml=coverage_path,
        top_modules=args.top_modules,
        top_functions=args.top_functions,
    )
    payload = serialize_report(report)

    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")

    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
