#!/usr/bin/env python3
"""Analyze changed SQL migrations for schema evolution risk."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
from typing import Any, Iterable, List, Sequence

_PLUGIN_ROOT = Path(
    os.environ.get(
        "CLAUDE_PLUGIN_ROOT",
        Path(__file__).resolve().parent.parent,
    )
)
sys.path.insert(0, str(_PLUGIN_ROOT))

from lib import schema_analysis


DEFAULT_CONFIG: dict[str, Any] = {
    "analysis": {
        "migrationGlobs": [
            "migrations/**/*.sql",
            "db/migrations/**/*.sql",
            "**/migrations/**/*.sql",
            "**/*migration*.sql",
        ],
        "minimumSeverity": "low",
        "failOnSeverity": "high",
        "defaultOutputFormat": "text",
        "maxFindings": 200,
    }
}

SEVERITY_CHOICES = ("low", "medium", "high")
FAIL_ON_CHOICES = ("none", "low", "medium", "high")


def _run_git(args: Sequence[str], repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _plugin_root() -> Path:
    return _PLUGIN_ROOT


def _load_config(plugin_root: Path) -> dict[str, Any]:
    config_path = plugin_root / ".claude-plugin" / "config.json"
    if not config_path.exists():
        return DEFAULT_CONFIG

    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return DEFAULT_CONFIG

    merged: dict[str, Any] = {
        "analysis": dict(DEFAULT_CONFIG["analysis"]),
    }
    merged["analysis"].update(loaded.get("analysis", {}))
    return merged


def _resolve_repo_root(cwd: Path) -> Path:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("Not inside a git repository. Provide explicit SQL file paths.")

    return Path(proc.stdout.strip())


def _parse_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def _candidate_default_ranges(repo_root: Path) -> list[str]:
    ranges: list[str] = []

    upstream = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], repo_root)
    if upstream.returncode == 0:
        upstream_name = upstream.stdout.strip()
        if upstream_name:
            ranges.append(f"{upstream_name}...HEAD")

    ranges.extend(
        [
            "origin/main...HEAD",
            "main...HEAD",
            "origin/master...HEAD",
            "master...HEAD",
            "HEAD~1..HEAD",
        ]
    )

    return ranges


def _first_valid_diff_range(repo_root: Path) -> str | None:
    for diff_range in _candidate_default_ranges(repo_root):
        proc = _run_git(["diff", "--name-only", "--diff-filter=ACMR", diff_range], repo_root)
        if proc.returncode == 0:
            return diff_range
    return None


def _collect_git_changed_files(
    repo_root: Path,
    *,
    diff_range: str | None,
    base_ref: str | None,
) -> list[str]:
    paths: set[str] = set()

    selected_range = diff_range
    if not selected_range and base_ref:
        selected_range = f"{base_ref}...HEAD"

    if not selected_range:
        selected_range = _first_valid_diff_range(repo_root)

    if selected_range:
        proc = _run_git(
            ["diff", "--name-only", "--diff-filter=ACMR", selected_range],
            repo_root,
        )
        if proc.returncode == 0:
            paths.update(_parse_lines(proc.stdout))

    for args in (
        ["diff", "--name-only", "--diff-filter=ACMR"],
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        proc = _run_git(args, repo_root)
        if proc.returncode == 0:
            paths.update(_parse_lines(proc.stdout))

    return sorted(paths)


def _normalize_input_paths(paths: Iterable[str], repo_root: Path) -> list[str]:
    normalized: list[str] = []
    for raw_path in paths:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            try:
                relative = candidate.resolve().relative_to(repo_root.resolve())
                normalized.append(relative.as_posix())
            except ValueError:
                normalized.append(candidate.as_posix())
        else:
            normalized.append(PurePosixPath(raw_path).as_posix())
    return sorted(set(normalized))


def _path_matches_globs(path: str, globs: Sequence[str]) -> bool:
    posix_path = PurePosixPath(path)
    for pattern in globs:
        clean_pattern = pattern[2:] if pattern.startswith("./") else pattern
        if posix_path.match(clean_pattern):
            return True
    return False


def _select_targets(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    migration_globs: Sequence[str],
) -> list[str]:
    if args.files:
        return _normalize_input_paths(args.files, repo_root)

    changed = _collect_git_changed_files(
        repo_root,
        diff_range=args.diff_range,
        base_ref=args.base_ref,
    )

    return [
        path
        for path in changed
        if path.lower().endswith(".sql") and _path_matches_globs(path, migration_globs)
    ]


def _read_targets(repo_root: Path, target_paths: Sequence[str]) -> tuple[list[tuple[str, str]], list[str]]:
    loaded: list[tuple[str, str]] = []
    missing: list[str] = []

    for target_path in target_paths:
        path_obj = Path(target_path)
        full_path = path_obj if path_obj.is_absolute() else (repo_root / path_obj)
        if not full_path.exists():
            missing.append(target_path)
            continue

        try:
            content = full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = full_path.read_text(encoding="utf-8", errors="replace")

        if path_obj.is_absolute():
            display_path = target_path
        else:
            display_path = full_path.resolve().relative_to(repo_root.resolve()).as_posix()

        loaded.append((display_path, content))

    return loaded, missing


def _severity_value(level: str) -> int:
    if level == "none":
        return 0
    return schema_analysis.SEVERITY_ORDER.get(level, 0)


def _has_failing_findings(report: dict[str, Any], fail_on: str) -> bool:
    threshold = _severity_value(fail_on)
    if threshold <= 0:
        return False

    for finding in report.get("findings", []):
        if _severity_value(str(finding.get("severity", ""))) >= threshold:
            return True
    return False


def _format_text_report(report: dict[str, Any], analyzed_targets: Sequence[str], skipped_missing: Sequence[str]) -> str:
    summary = report.get("summary", {})
    by_severity = summary.get("by_severity", {})

    lines: list[str] = [
        "Schema Evolution Report",
        report.get("assumption", ""),
        "",
        "Summary",
        f"- Status: {report.get('status', 'unknown')}",
        f"- Files analyzed: {summary.get('files_scanned', 0)}",
        f"- Statements analyzed: {summary.get('statements_scanned', 0)}",
        (
            "- Findings: "
            f"{summary.get('reported_findings', 0)} "
            f"(high={by_severity.get('high', 0)}, "
            f"medium={by_severity.get('medium', 0)}, "
            f"low={by_severity.get('low', 0)})"
        ),
        f"- Minimum severity: {summary.get('minimum_severity', 'low')}",
    ]

    if analyzed_targets:
        lines.append(f"- Targets: {', '.join(analyzed_targets)}")

    if skipped_missing:
        lines.append(f"- Skipped missing files: {', '.join(skipped_missing)}")

    findings = report.get("findings", [])
    if not findings:
        lines.extend(
            [
                "",
                "Findings",
                "- No risky schema evolution patterns detected at the selected severity threshold.",
            ]
        )
        return "\n".join(lines)

    lines.extend(["", "Findings (risk-ranked)"])
    for index, finding in enumerate(findings, start=1):
        lines.append(
            (
                f"{index}. [{str(finding.get('severity', 'unknown')).upper()}] "
                f"{finding.get('file')}:{finding.get('line')} "
                f"{finding.get('rule_id')}"
            )
        )
        lines.append(f"   - Message: {finding.get('message')}")
        if finding.get("object"):
            lines.append(f"   - Object: {finding.get('object')}")
        lines.append(f"   - Rationale: {finding.get('rationale')}")
        lines.append(f"   - Mitigation: {finding.get('mitigation')}")
        lines.append(f"   - Statement: {finding.get('statement_excerpt')}")

    return "\n".join(lines)


def build_parser(config: dict[str, Any]) -> argparse.ArgumentParser:
    analysis_config = config.get("analysis", {})

    parser = argparse.ArgumentParser(
        description="Analyze SQL schema migration changes for risky evolution patterns.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Explicit SQL file paths to analyze. If omitted, changed migration files are auto-detected from git.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default=str(analysis_config.get("defaultOutputFormat", "text")),
        help="Output format.",
    )
    parser.add_argument(
        "--min-severity",
        choices=SEVERITY_CHOICES,
        default=str(analysis_config.get("minimumSeverity", "low")),
        help="Minimum severity to include in report.",
    )
    parser.add_argument(
        "--fail-on",
        choices=FAIL_ON_CHOICES,
        default=str(analysis_config.get("failOnSeverity", "high")),
        help="Exit with status 1 if any finding meets or exceeds this severity.",
    )
    parser.add_argument(
        "--max-findings",
        type=int,
        default=int(analysis_config.get("maxFindings", 200)),
        help="Maximum findings to include in output.",
    )
    parser.add_argument(
        "--migration-glob",
        action="append",
        default=None,
        help="Override migration glob patterns (repeatable).",
    )
    parser.add_argument(
        "--diff-range",
        default=None,
        help="Git diff range used when auto-detecting changed files (example: origin/main...HEAD).",
    )
    parser.add_argument(
        "--base-ref",
        default=None,
        help="Base ref for range diff (<base-ref>...HEAD) when auto-detecting changed files.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    plugin_root = _plugin_root()
    config = _load_config(plugin_root)
    parser = build_parser(config)

    args = parser.parse_args(argv)

    try:
        repo_root = _resolve_repo_root(Path.cwd())
    except RuntimeError as exc:
        if args.files:
            repo_root = Path.cwd()
        else:
            print(str(exc), file=sys.stderr)
            return 2

    migration_globs = args.migration_glob or config.get("analysis", {}).get("migrationGlobs", [])

    targets = _select_targets(
        args=args,
        repo_root=repo_root,
        migration_globs=migration_globs,
    )

    loaded_files, missing_files = _read_targets(repo_root, targets)

    report = schema_analysis.analyze_schema_changes(
        loaded_files,
        min_severity=args.min_severity,
        max_findings=args.max_findings,
    )

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_text_report(report, [path for path, _ in loaded_files], missing_files))

    return 1 if _has_failing_findings(report, args.fail_on) else 0


if __name__ == "__main__":
    raise SystemExit(main())
