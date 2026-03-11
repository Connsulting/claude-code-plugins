#!/usr/bin/env python3
"""
Audit compound-learning source files for missing or weak diagnostics.

This is a static heuristic auditor. It focuses on Python and shell entry points
where failure-prone operations are caught or handled without enough context to
debug the problem quickly.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCOPE_GLOBS = (
    "hooks/*.py",
    "hooks/*.sh",
    "lib/*.py",
    "lib/*.sh",
    "scripts/*.py",
    "skills/*/*.py",
)
SEVERITY_PRIORITY = {
    "high": 0,
    "medium": 1,
    "low": 2,
}
CLASSIFICATION_PRIORITY = {
    "missing_exception_diagnostics": 0,
    "missing_failure_diagnostics": 1,
    "mixed_machine_output": 2,
    "low_context_diagnostic": 3,
}
GENERIC_DIAGNOSTIC_MESSAGES = {
    "an error occurred",
    "error",
    "exception",
    "failed",
    "failure",
    "operation failed",
    "search failed",
    "unexpected error",
    "warning",
}
EXCEPTION_CONTEXT_TOKENS = {
    "e",
    "err",
    "error",
    "exc",
    "exception",
    "repr",
    "str",
}
STRUCTURED_ERROR_KEYS = {
    "error",
    "errors",
    "hint",
    "message",
    "status",
}
STRUCTURED_ERROR_STATUSES = {
    "error",
    "failed",
    "failure",
    "warning",
}
SHELL_RISKY_TOKENS = {
    "claude": "subprocess",
    "find": "filesystem",
    "jq": "subprocess",
    "mkdir": "filesystem",
    "mktemp": "filesystem",
    "pip": "subprocess",
    "python": "subprocess",
    "python3": "subprocess",
    "timeout": "subprocess",
}
PYTHON_STOP_TYPES = (
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.FunctionDef,
    ast.Lambda,
    ast.Try,
)
LOGGER_METHODS = {
    "critical",
    "debug",
    "error",
    "exception",
    "info",
    "warn",
    "warning",
}


@dataclass(frozen=True)
class SourceFile:
    rel_path: str
    language: str


@dataclass(frozen=True)
class RiskyCall:
    kind: str
    line: int
    call_name: str


@dataclass(frozen=True)
class DiagnosticCall:
    line: int
    message_text: str
    dynamic: bool
    exception_only: bool
    context_tokens: tuple[str, ...] = ()


def discover_source_files(plugin_root: Path, patterns: Sequence[str] = SCOPE_GLOBS) -> list[SourceFile]:
    tracked = _discover_tracked_source_files(plugin_root, patterns)
    if tracked is not None:
        return tracked

    discovered: dict[str, SourceFile] = {}

    for pattern in patterns:
        for path in sorted(plugin_root.glob(pattern)):
            if not path.is_file():
                continue
            rel_path = path.relative_to(plugin_root).as_posix()
            if rel_path in discovered:
                continue
            discovered[rel_path] = SourceFile(
                rel_path=rel_path,
                language="python" if path.suffix == ".py" else "shell",
            )

    return [discovered[key] for key in sorted(discovered)]


def _discover_tracked_source_files(plugin_root: Path, patterns: Sequence[str]) -> list[SourceFile] | None:
    result = subprocess.run(
        ["git", "-C", str(plugin_root), "ls-files", "--", *patterns],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    discovered: dict[str, SourceFile] = {}
    for raw_path in result.stdout.splitlines():
        rel_path = raw_path.strip()
        if not rel_path:
            continue
        discovered[rel_path] = SourceFile(
            rel_path=rel_path,
            language="python" if rel_path.endswith(".py") else "shell",
        )

    return [discovered[key] for key in sorted(discovered)]


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parent = _call_name(func.value)
        return f"{parent}.{func.attr}" if parent else func.attr
    return ""


def _is_sys_stderr(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr == "stderr"


def _print_targets_stderr(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg == "file" and _is_sys_stderr(keyword.value):
            return True
    return False


def _expression_context_tokens(node: ast.AST | None) -> tuple[str, ...]:
    if node is None:
        return ()

    tokens: set[str] = set()

    if isinstance(node, ast.Name):
        tokens.add(node.id)
        return tuple(sorted(tokens))

    if isinstance(node, ast.Attribute):
        full_name = _call_name(node)
        if full_name:
            tokens.add(full_name)
        tokens.update(_expression_context_tokens(node.value))
        return tuple(sorted(tokens))

    for child in ast.iter_child_nodes(node):
        tokens.update(_expression_context_tokens(child))

    return tuple(sorted(tokens))


def _has_contextual_tokens(tokens: Iterable[str]) -> bool:
    for token in tokens:
        root = token.lower().split(".", 1)[0]
        if root not in EXCEPTION_CONTEXT_TOKENS:
            return True
    return False


def _message_details(node: ast.AST | None) -> tuple[str, bool, bool, tuple[str, ...]]:
    if node is None:
        return "", False, False, ()

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False, False, ()

    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        dynamic = False
        context_tokens: set[str] = set()
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                dynamic = True
                context_tokens.update(_expression_context_tokens(value.value))
        message_text = "".join(parts)
        exception_only = dynamic and not message_text.strip() and not _has_contextual_tokens(context_tokens)
        return message_text, dynamic, exception_only, tuple(sorted(context_tokens))

    if isinstance(node, ast.Name):
        context_tokens = (node.id,)
        return "", True, not _has_contextual_tokens(context_tokens), context_tokens

    if isinstance(node, ast.Call) and _call_name(node.func) == "str" and node.args:
        context_tokens = _expression_context_tokens(node.args[0])
        return "", True, not _has_contextual_tokens(context_tokens), context_tokens

    context_tokens = _expression_context_tokens(node)
    return "", True, False, context_tokens


def _normalize_message(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", normalized)


def _diagnostic_is_low_context(diagnostic: DiagnosticCall) -> bool:
    if diagnostic.exception_only:
        return True

    normalized = _normalize_message(diagnostic.message_text)
    if not normalized:
        return diagnostic.dynamic and not _has_contextual_tokens(diagnostic.context_tokens)

    return normalized in GENERIC_DIAGNOSTIC_MESSAGES and not _has_contextual_tokens(diagnostic.context_tokens)


def _iter_nodes(nodes: Iterable[ast.stmt], stop_types: tuple[type[ast.AST], ...]) -> Iterable[ast.AST]:
    for node in nodes:
        yield from _walk_node(node, stop_types)


def _walk_node(node: ast.AST, stop_types: tuple[type[ast.AST], ...]) -> Iterable[ast.AST]:
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, stop_types):
            yield child
            continue
        yield from _walk_node(child, stop_types)


def _open_mode(call: ast.Call) -> str | None:
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str):
        return call.args[1].value
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def _python_risky_call_kind(call: ast.Call) -> str | None:
    name = _call_name(call.func)

    if name == "open":
        mode = _open_mode(call)
        if mode and any(token in mode for token in ("w", "a", "x", "+")):
            return "file_write"
        return "file_read"

    if name in {"read_text", "read_bytes"} or name.endswith(".read_text") or name.endswith(".read_bytes"):
        return "file_read"
    if name in {"write_text", "write_bytes"} or name.endswith(".write_text") or name.endswith(".write_bytes"):
        return "file_write"
    if name in {"mkdir"} or name.endswith(".mkdir") or name in {"os.makedirs", "makedirs", "Path.mkdir"}:
        return "filesystem"
    if name in {"replace", "rename"} or name in {"shutil.copy2", "shutil.move"} or name.endswith(".replace") or name.endswith(".rename"):
        return "filesystem"

    if name in {
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.run",
        "os.popen",
        "os.system",
    }:
        return "subprocess"

    if name in {"sqlite3.connect", "db.get_connection", "sqlite_vec.load"}:
        return "database"

    if name == "SentenceTransformer" or name.endswith(".SentenceTransformer"):
        return "model_load"

    return None


def _collect_risky_calls(nodes: Iterable[ast.stmt]) -> list[RiskyCall]:
    risky_calls: list[RiskyCall] = []
    seen: set[tuple[int, str]] = set()

    for node in _iter_nodes(nodes, stop_types=PYTHON_STOP_TYPES):
        if not isinstance(node, ast.Call):
            continue
        kind = _python_risky_call_kind(node)
        if not kind:
            continue
        key = (node.lineno, kind)
        if key in seen:
            continue
        seen.add(key)
        risky_calls.append(RiskyCall(kind=kind, line=node.lineno, call_name=_call_name(node.func)))

    return risky_calls


def _diagnostic_from_call(node: ast.Call) -> DiagnosticCall | None:
    name = _call_name(node.func)

    if name == "print":
        if _structured_stdout_call(node):
            return None
        message_text, dynamic, exception_only, context_tokens = _message_details(node.args[0] if node.args else None)
        if not _print_targets_stderr(node) and not message_text.strip() and not dynamic:
            return None
        return DiagnosticCall(
            line=node.lineno,
            message_text=message_text,
            dynamic=dynamic,
            exception_only=exception_only,
            context_tokens=context_tokens,
        )

    if name == "sys.stderr.write":
        message_text, dynamic, exception_only, context_tokens = _message_details(node.args[0] if node.args else None)
        return DiagnosticCall(
            line=node.lineno,
            message_text=message_text,
            dynamic=dynamic,
            exception_only=exception_only,
            context_tokens=context_tokens,
        )

    if any(name == method or name.endswith(f".{method}") for method in LOGGER_METHODS):
        message_text, dynamic, exception_only, context_tokens = _message_details(node.args[0] if node.args else None)
        return DiagnosticCall(
            line=node.lineno,
            message_text=message_text,
            dynamic=dynamic,
            exception_only=exception_only,
            context_tokens=context_tokens,
        )

    return None


def _structured_stdout_call(node: ast.Call) -> bool:
    if _call_name(node.func) != "print" or _print_targets_stderr(node):
        return False
    if not node.args:
        return False

    first_arg = node.args[0]
    return isinstance(first_arg, ast.Call) and _call_name(first_arg.func) == "json.dumps"


def _human_stdout_call(node: ast.Call) -> bool:
    if _call_name(node.func) != "print" or _print_targets_stderr(node):
        return False
    if not node.args or _structured_stdout_call(node):
        return False

    text, dynamic, _, _ = _message_details(node.args[0])
    return bool(text.strip() or dynamic)


def _collect_handler_diagnostics(handler: ast.ExceptHandler) -> list[DiagnosticCall]:
    diagnostics: list[DiagnosticCall] = []
    for node in _iter_nodes(handler.body, stop_types=PYTHON_STOP_TYPES):
        if not isinstance(node, ast.Call):
            continue
        diagnostic = _diagnostic_from_call(node)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    return diagnostics


def _string_literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _mapping_looks_like_error_payload(node: ast.Dict) -> bool:
    for key_node, value_node in zip(node.keys, node.values):
        key = _string_literal(key_node)
        if key is None:
            continue
        normalized_key = key.lower()
        if normalized_key in STRUCTURED_ERROR_KEYS - {"status"}:
            return True
        if normalized_key == "status":
            status_value = _string_literal(value_node)
            if status_value and _normalize_message(status_value) in STRUCTURED_ERROR_STATUSES:
                return True
    return False


def _structured_error_name(target: ast.AST) -> str | None:
    if isinstance(target, ast.Name):
        return target.id

    if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
        key = _string_literal(target.slice)
        if key and key.lower() in STRUCTURED_ERROR_KEYS:
            return target.value.id

    return None


def _assignment_marks_structured_error(target: ast.AST, value: ast.AST | None, error_names: set[str]) -> str | None:
    if isinstance(target, ast.Name) and _expression_is_structured_error(value, error_names):
        return target.id

    if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
        key = _string_literal(target.slice)
        if not key:
            return None
        normalized_key = key.lower()
        if normalized_key in STRUCTURED_ERROR_KEYS - {"status"}:
            return target.value.id
        if normalized_key == "status":
            status_value = _string_literal(value)
            if status_value and _normalize_message(status_value) in STRUCTURED_ERROR_STATUSES:
                return target.value.id

    return None


def _call_updates_structured_error(node: ast.Call) -> str | None:
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Subscript)
        and isinstance(node.func.value.value, ast.Name)
    ):
        key = _string_literal(node.func.value.slice)
        if key and key.lower() in STRUCTURED_ERROR_KEYS and node.func.attr in {"append", "extend"}:
            return node.func.value.value.id

    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.attr == "update":
        if node.args and isinstance(node.args[0], ast.Dict) and _mapping_looks_like_error_payload(node.args[0]):
            return node.func.value.id

    return None


def _expression_is_structured_error(node: ast.AST | None, error_names: set[str]) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Dict):
        return _mapping_looks_like_error_payload(node)
    if isinstance(node, ast.Name):
        return node.id in error_names
    return False


def _handler_has_structured_error_output(handler: ast.ExceptHandler) -> bool:
    error_names: set[str] = set()

    for node in _iter_nodes(handler.body, stop_types=PYTHON_STOP_TYPES):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = _assignment_marks_structured_error(target, node.value, error_names)
                if name:
                    error_names.add(name)
        elif isinstance(node, ast.AnnAssign):
            name = _assignment_marks_structured_error(node.target, node.value, error_names)
            if name:
                error_names.add(name)
        elif isinstance(node, ast.Call):
            updated_name = _call_updates_structured_error(node)
            if updated_name:
                error_names.add(updated_name)

        if isinstance(node, ast.Call) and _structured_stdout_call(node):
            return True
        if isinstance(node, ast.Return) and _expression_is_structured_error(node.value, error_names):
            return True

    return False


def _statement_guarantees_reraise(node: ast.stmt) -> bool:
    if isinstance(node, ast.Raise):
        return True

    if isinstance(node, ast.If):
        if not node.orelse:
            return False
        return _block_guarantees_reraise(node.body) and _block_guarantees_reraise(node.orelse)

    return False


def _block_guarantees_reraise(statements: Sequence[ast.stmt]) -> bool:
    for node in statements:
        if _statement_guarantees_reraise(node):
            return True
        if isinstance(node, (ast.Break, ast.Continue, ast.Return)):
            return False
    return False


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    return _block_guarantees_reraise(handler.body)


def _handler_suppresses_exception(handler: ast.ExceptHandler) -> bool:
    if not handler.body:
        return True

    for node in _iter_nodes(
        handler.body,
        stop_types=(ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef, ast.Lambda),
    ):
        if isinstance(node, (ast.Break, ast.Continue, ast.Pass, ast.Return)):
            return True
        if isinstance(node, ast.Call) and _call_name(node.func) in {"exit", "sys.exit"}:
            return True

    return False


def _module_snippet(text: str, line: int) -> str:
    lines = text.splitlines()
    if line <= 0 or line > len(lines):
        return ""
    return lines[line - 1].strip()


def _make_finding(
    *,
    source: SourceFile,
    line: int,
    classification: str,
    severity: str,
    evidence: str,
    reason: str,
    operation_types: Iterable[str] = (),
    function_name: str | None = None,
) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "path": source.rel_path,
        "line": line,
        "classification": classification,
        "severity": severity,
        "evidence": evidence,
        "reason": reason,
        "operation_types": sorted(set(operation_types)),
    }
    if function_name:
        finding["function"] = function_name
    return finding


def _analyze_python_try_handlers(source: SourceFile, text: str, tree: ast.AST) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue

        risky_calls = _collect_risky_calls(node.body)
        if not risky_calls:
            continue

        operation_types = sorted({call.kind for call in risky_calls})
        primary_call = sorted(risky_calls, key=lambda item: (item.line, item.call_name))[0]

        for handler in node.handlers:
            diagnostics = _collect_handler_diagnostics(handler)
            if (
                not diagnostics
                and not _handler_has_structured_error_output(handler)
                and not _handler_reraises(handler)
                and _handler_suppresses_exception(handler)
            ):
                evidence = _module_snippet(text, handler.lineno) or _module_snippet(text, primary_call.line)
                findings.append(
                    _make_finding(
                        source=source,
                        line=handler.lineno,
                        classification="missing_exception_diagnostics",
                        severity="high",
                        evidence=evidence,
                        reason=(
                            "Exception handler suppresses a failure-prone operation without "
                            "diagnostic output or a structured error result."
                        ),
                        operation_types=operation_types,
                    )
                )
                continue

            for diagnostic in diagnostics:
                if not _diagnostic_is_low_context(diagnostic):
                    continue
                evidence = _module_snippet(text, diagnostic.line)
                findings.append(
                    _make_finding(
                        source=source,
                        line=diagnostic.line,
                        classification="low_context_diagnostic",
                        severity="low",
                        evidence=evidence,
                        reason="Diagnostic message is too generic to identify the failing operation.",
                        operation_types=operation_types,
                    )
                )
                break

    return findings


def _analyze_python_output_mix(source: SourceFile, text: str, tree: ast.AST) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    scopes: list[tuple[str, list[ast.stmt], int]] = [("<module>", list(getattr(tree, "body", [])), 1)]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append((node.name, list(node.body), node.lineno))

    for scope_name, body, _ in scopes:
        machine_calls: list[ast.Call] = []
        human_calls: list[ast.Call] = []
        for statement in body:
            if isinstance(statement, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef)):
                continue
            for node in _walk_node(statement, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef, ast.Lambda)):
                if not isinstance(node, ast.Call):
                    continue
                if _structured_stdout_call(node):
                    machine_calls.append(node)
                elif _human_stdout_call(node):
                    human_calls.append(node)

        if not machine_calls or not human_calls:
            continue

        first_human = sorted(human_calls, key=lambda item: item.lineno)[0]
        findings.append(
            _make_finding(
                source=source,
                line=first_human.lineno,
                classification="mixed_machine_output",
                severity="medium",
                evidence=_module_snippet(text, first_human.lineno),
                reason=(
                    "Structured stdout output shares a function with human-readable stdout "
                    "messages, which makes machine consumption brittle."
                ),
                function_name=None if scope_name == "<module>" else scope_name,
            )
        )

    return findings


def analyze_python_source(source: SourceFile, text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text, filename=source.rel_path)
    except SyntaxError:
        return []

    findings = _analyze_python_try_handlers(source, text, tree)
    findings.extend(_analyze_python_output_mix(source, text, tree))
    return findings


def _shell_command_kind(command: str) -> str | None:
    tokens = re.findall(r"[A-Za-z0-9_.-]+", command)
    for token in tokens:
        kind = SHELL_RISKY_TOKENS.get(token)
        if kind:
            return kind
    return None


def _shell_failure_block(lines: list[str], start: int) -> tuple[list[str], int]:
    body: list[str] = []
    depth = 1
    index = start + 1

    while index < len(lines):
        stripped = lines[index].strip()
        if re.match(r"if\b", stripped):
            depth += 1
        elif stripped == "fi":
            depth -= 1
            if depth == 0:
                return body, index
        body.append(lines[index])
        index += 1

    return body, len(lines) - 1


def _shell_has_diagnostic(block: Iterable[str]) -> bool:
    for line in block:
        if "log_activity" in line:
            return True
        if re.search(r"\b(?:echo|printf)\b.*>&2", line):
            return True
    return False


def _shell_extract_message(line: str) -> str:
    match = re.search(r"""(?:log_activity|echo|printf)\s+(['"])(.*?)\1""", line)
    if not match:
        return ""
    return match.group(2)


def analyze_shell_source(source: SourceFile, text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        match = re.match(r"\s*if\s+!\s+(.+?)\s*;\s*then\s*$", line)
        if not match:
            index += 1
            continue

        command = match.group(1).strip()
        command_kind = _shell_command_kind(command)
        block, end_index = _shell_failure_block(lines, index)
        stripped_block = [entry.strip() for entry in block if entry.strip() and not entry.strip().startswith("#")]

        if command_kind and stripped_block:
            if not _shell_has_diagnostic(stripped_block):
                if any(token.startswith(("exit", "return", "continue")) for token in stripped_block):
                    findings.append(
                        _make_finding(
                            source=source,
                            line=index + 1,
                            classification="missing_failure_diagnostics",
                            severity="high",
                            evidence=line.strip(),
                            reason=(
                                "Failure branch handles a risky shell command without "
                                "log_activity or stderr output."
                            ),
                            operation_types=[command_kind],
                        )
                    )
            else:
                for branch_line in stripped_block:
                    if "log_activity" not in branch_line and not re.search(r"\b(?:echo|printf)\b.*>&2", branch_line):
                        continue
                    message = _shell_extract_message(branch_line)
                    if not message or not _normalize_message(message) in GENERIC_DIAGNOSTIC_MESSAGES:
                        continue
                    findings.append(
                        _make_finding(
                            source=source,
                            line=index + 1,
                            classification="low_context_diagnostic",
                            severity="low",
                            evidence=branch_line,
                            reason="Diagnostic message is too generic to identify the failing shell command.",
                            operation_types=[command_kind],
                        )
                    )
                    break

        index = end_index + 1

    return findings


def _severity_matches(value: str, minimum: str) -> bool:
    return SEVERITY_PRIORITY[value] <= SEVERITY_PRIORITY[minimum]


def _sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    function_name = item.get("function") or ""
    return (
        SEVERITY_PRIORITY[item["severity"]],
        CLASSIFICATION_PRIORITY[item["classification"]],
        item["path"],
        int(item["line"]),
        function_name,
        item["evidence"],
    )


def audit_logging_quality(plugin_root: Path, min_severity: str = "low", fail_on: str = "none") -> dict[str, Any]:
    source_files = discover_source_files(plugin_root)
    findings: list[dict[str, Any]] = []

    for source in source_files:
        path = plugin_root / source.rel_path
        text = path.read_text(encoding="utf-8")
        if source.language == "python":
            findings.extend(analyze_python_source(source, text))
        else:
            findings.extend(analyze_shell_source(source, text))

    findings.sort(key=_sort_key)
    for index, finding in enumerate(findings, start=1):
        finding["priority_rank"] = index

    filtered_findings = [finding for finding in findings if _severity_matches(finding["severity"], min_severity)]

    by_classification = {name: 0 for name in CLASSIFICATION_PRIORITY}
    by_severity = {name: 0 for name in SEVERITY_PRIORITY}
    for finding in filtered_findings:
        by_classification[finding["classification"]] += 1
        by_severity[finding["severity"]] += 1

    gate_triggered = False
    gate_matches = 0
    if fail_on != "none":
        gate_matches = sum(1 for finding in findings if _severity_matches(finding["severity"], fail_on))
        gate_triggered = gate_matches > 0

    return {
        "scope": {
            "plugin_root": str(plugin_root.resolve()),
            "patterns": list(SCOPE_GLOBS),
        },
        "filters": {
            "min_severity": min_severity,
        },
        "gate": {
            "fail_on": fail_on,
            "triggered": gate_triggered,
            "matching_findings": gate_matches,
        },
        "summary": {
            "source_files": len(source_files),
            "findings_found": len(filtered_findings),
            "by_severity": by_severity,
            "by_classification": by_classification,
        },
        "findings": filtered_findings,
    }


def render_json_report(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def render_text_report(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "Logging Quality Auditor",
        f"Plugin Root: {report['scope']['plugin_root']}",
        f"Severity Filter: {report['filters']['min_severity']}",
        (
            f"Fail On: {report['gate']['fail_on']} "
            f"(triggered={report['gate']['triggered']}, matches={report['gate']['matching_findings']})"
        ),
        "",
        "Summary:",
        f"- Source files: {summary['source_files']}",
        f"- Findings: {summary['findings_found']}",
        f"- High: {summary['by_severity']['high']}",
        f"- Medium: {summary['by_severity']['medium']}",
        f"- Low: {summary['by_severity']['low']}",
    ]

    if report["findings"]:
        lines.append("")
        lines.append("Prioritized findings:")
        for finding in report["findings"]:
            suffix = f" | function={finding['function']}" if "function" in finding else ""
            operations = ",".join(finding["operation_types"]) or "n/a"
            lines.append(
                (
                    f"{finding['priority_rank']:>2}. {finding['severity']:<6} "
                    f"{finding['classification']:<29} {finding['path']}:{finding['line']} "
                    f"| ops={operations}{suffix}"
                )
            )
            lines.append(f"    evidence: {finding['evidence']}")
            lines.append(f"    reason: {finding['reason']}")
    else:
        lines.append("")
        lines.append("No findings matched the configured rules.")

    return "\n".join(lines)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit logging quality for the compound-learning plugin.")
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
        "--min-severity",
        choices=("low", "medium", "high"),
        default="low",
        help="Minimum severity to include in the report (default: low).",
    )
    parser.add_argument(
        "--fail-on",
        choices=("none", "low", "medium", "high"),
        default="none",
        help="Return exit code 1 when findings meet or exceed this severity.",
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

    report = audit_logging_quality(
        plugin_root=plugin_root,
        min_severity=args.min_severity,
        fail_on=args.fail_on,
    )

    output = render_json_report(report) if args.format == "json" else render_text_report(report)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")

    print(output)
    return 1 if report["gate"]["triggered"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
