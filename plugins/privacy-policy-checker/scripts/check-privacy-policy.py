#!/usr/bin/env python3
"""Deterministic static checker for tagged privacy-policy claims."""

from __future__ import annotations

import argparse
import copy
import fnmatch
import ipaddress
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PLUGIN_ROOT / ".claude-plugin" / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "policy": {
        "defaultPath": "PRIVACY_POLICY.md",
    },
    "source": {
        "include": [
            "*.py",
            "**/*.py",
            "*.js",
            "**/*.js",
            "*.ts",
            "**/*.ts",
            "*.tsx",
            "**/*.tsx",
            "*.jsx",
            "**/*.jsx",
            "*.go",
            "**/*.go",
            "*.java",
            "**/*.java",
            "*.rb",
            "**/*.rb",
            "*.php",
            "**/*.php",
            "*.cs",
            "**/*.cs",
            "*.swift",
            "**/*.swift",
            "*.kt",
            "**/*.kt",
            "*.rs",
            "**/*.rs",
            "*.sh",
            "**/*.sh",
            "*.yaml",
            "**/*.yaml",
            "*.yml",
            "**/*.yml",
            "*.json",
            "**/*.json",
            "*.toml",
            "**/*.toml",
        ],
        "exclude": [
            ".git/**",
            "**/.git/**",
            ".claude/**",
            "**/.claude/**",
            ".claude-plugin/**",
            "**/.claude-plugin/**",
            "tests/**",
            "**/tests/**",
            "test/**",
            "**/test/**",
            "vendor/**",
            "**/vendor/**",
            "node_modules/**",
            "**/node_modules/**",
            "dist/**",
            "**/dist/**",
            "build/**",
            "**/build/**",
            ".next/**",
            "**/.next/**",
            "coverage/**",
            "**/coverage/**",
            "__pycache__/**",
            "**/__pycache__/**",
            ".venv/**",
            "**/.venv/**",
            "venv/**",
            "**/venv/**",
            "*.min.js",
            "**/*.min.js",
            "*.map",
            "**/*.map",
        ],
    },
    "network": {
        "allowlistDomains": [],
    },
    "scanner": {
        "maxFileSizeBytes": 1_000_000,
    },
}

CLAIM_TAG_PATTERN = re.compile(r"\[privacy-claim:([a-z0-9_]+)\]")
URL_PATTERN = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")

ANALYTICS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google_analytics", re.compile(r"\bgtag\s*\(", re.IGNORECASE)),
    ("google_analytics", re.compile(r"\bga\s*\(", re.IGNORECASE)),
    (
        "analytics_domain",
        re.compile(
            # Match tracker domains in both normal strings ("google-analytics.com")
            # and escaped source literals ("google-analytics\\.com").
            r"google-analytics\\?\.com|googletagmanager\\?\.com|api\\?\.segment\\?\.io|cdn\\?\.segment\\?\.com",
            re.IGNORECASE,
        ),
    ),
    (
        "analytics_sdk",
        re.compile(r"\bmixpanel\b|\bamplitude\b|\bposthog\b|\bhotjar\b|\bfullstory\b", re.IGNORECASE),
    ),
)

PII_LOGGING_PATTERN = re.compile(
    r"((logger|log)\.(debug|info|warning|warn|error|critical)|"
    r"console\.log|print)\s*\(.*"
    r"\b(email|e-mail|ssn|social security|phone|address|dob|date[_ -]?of[_ -]?birth|"
    r"passport|credit[_ -]?card|cvv|account[_ -]?number|password|token)\b",
    re.IGNORECASE,
)

SUPPORTED_CLAIM_IDS: tuple[str, ...] = (
    "no_analytics_trackers",
    "no_third_party_exfiltration",
    "no_pii_logging",
)


@dataclass(frozen=True)
class Violation:
    claim_id: str
    file: str
    line: int
    evidence: str
    rule_id: str


def deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(config_path: Path | None) -> tuple[dict[str, Any], Path]:
    resolved_path = (config_path or DEFAULT_CONFIG_PATH).resolve()
    config = copy.deepcopy(DEFAULT_CONFIG)

    if resolved_path.exists():
        raw = resolved_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, Mapping):
            raise ValueError("Configuration file must contain a JSON object")
        deep_merge(config, parsed)
    elif config_path is not None:
        raise FileNotFoundError(f"Config file not found: {resolved_path}")

    return config, resolved_path


def extract_claim_ids(policy_text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in CLAIM_TAG_PATTERN.finditer(policy_text):
        claim_id = match.group(1)
        if claim_id not in seen:
            found.append(claim_id)
            seen.add(claim_id)
    return found


def classify_claims(claim_ids: Sequence[str]) -> tuple[list[str], list[str]]:
    supported: list[str] = []
    unsupported: list[str] = []
    for claim_id in claim_ids:
        if claim_id in SUPPORTED_CLAIM_IDS:
            supported.append(claim_id)
        else:
            unsupported.append(claim_id)
    return supported, unsupported


def normalize_globs(values: Sequence[str]) -> list[str]:
    return [value.replace("\\", "/").lstrip("./") for value in values if value]


def path_matches(path: str, patterns: Sequence[str]) -> bool:
    pure_path = PurePosixPath(path)
    for pattern in patterns:
        normalized = pattern.replace("\\", "/").lstrip("./")
        if pure_path.match(normalized) or fnmatch.fnmatch(path, normalized):
            return True
    return False


def is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(2048)
    except OSError:
        return True
    return b"\x00" in chunk


def collect_candidate_files(
    repo_root: Path,
    include_globs: Sequence[str],
    exclude_globs: Sequence[str],
    max_file_size_bytes: int,
) -> list[Path]:
    include = normalize_globs(include_globs)
    exclude = normalize_globs(exclude_globs)

    candidates: list[Path] = []
    for path in sorted(repo_root.rglob("*"), key=lambda value: value.as_posix()):
        if not path.is_file():
            continue

        rel_path = path.relative_to(repo_root).as_posix()
        if include and not path_matches(rel_path, include):
            continue
        if exclude and path_matches(rel_path, exclude):
            continue

        try:
            if path.stat().st_size > max_file_size_bytes:
                continue
        except OSError:
            continue

        if is_binary(path):
            continue

        candidates.append(path)
    return candidates


def truncate_evidence(line: str, limit: int = 180) -> str:
    compact = " ".join(line.strip().split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def check_no_analytics_trackers(rel_path: str, lines: Sequence[str]) -> list[Violation]:
    violations: list[Violation] = []
    for idx, line in enumerate(lines, start=1):
        for rule_id, pattern in ANALYTICS_PATTERNS:
            if pattern.search(line):
                violations.append(
                    Violation(
                        claim_id="no_analytics_trackers",
                        file=rel_path,
                        line=idx,
                        evidence=truncate_evidence(line),
                        rule_id=rule_id,
                    )
                )
                break
    return violations


def domain_is_local_or_private(domain: str) -> bool:
    normalized = domain.lower().strip(".")
    if normalized in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return True
    if normalized.endswith(".local"):
        return True

    try:
        ip_addr = ipaddress.ip_address(normalized)
    except ValueError:
        return False

    return bool(ip_addr.is_private or ip_addr.is_loopback or ip_addr.is_link_local)


def domain_is_allowlisted(domain: str, allowlist: Sequence[str]) -> bool:
    normalized = domain.lower().strip(".")
    for allowed in allowlist:
        entry = allowed.lower().strip().strip(".")
        if not entry:
            continue
        if normalized == entry or normalized.endswith(f".{entry}"):
            return True
    return False


def check_no_third_party_exfiltration(
    rel_path: str,
    lines: Sequence[str],
    allowlist_domains: Sequence[str],
) -> list[Violation]:
    violations: list[Violation] = []
    for idx, line in enumerate(lines, start=1):
        for match in URL_PATTERN.finditer(line):
            url = match.group(0)
            parsed = urlparse(url)
            domain = parsed.hostname
            if not domain:
                continue
            if domain_is_local_or_private(domain):
                continue
            if domain_is_allowlisted(domain, allowlist_domains):
                continue

            violations.append(
                Violation(
                    claim_id="no_third_party_exfiltration",
                    file=rel_path,
                    line=idx,
                    evidence=truncate_evidence(line),
                    rule_id="external_domain_literal",
                )
            )
    return violations


def check_no_pii_logging(rel_path: str, lines: Sequence[str]) -> list[Violation]:
    violations: list[Violation] = []
    for idx, line in enumerate(lines, start=1):
        if PII_LOGGING_PATTERN.search(line):
            violations.append(
                Violation(
                    claim_id="no_pii_logging",
                    file=rel_path,
                    line=idx,
                    evidence=truncate_evidence(line),
                    rule_id="pii_logging_call",
                )
            )
    return violations


def run_claim_checks(
    claim_ids: Sequence[str],
    repo_root: Path,
    config: Mapping[str, Any],
) -> tuple[list[Violation], int]:
    source = config.get("source", {})
    scanner = config.get("scanner", {})
    network = config.get("network", {})

    include_globs = source.get("include", [])
    exclude_globs = source.get("exclude", [])
    max_file_size = int(scanner.get("maxFileSizeBytes", 1_000_000))
    allowlist = network.get("allowlistDomains", [])

    files = collect_candidate_files(repo_root, include_globs, exclude_globs, max_file_size)
    violations: list[Violation] = []

    for path in files:
        rel_path = path.relative_to(repo_root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for claim_id in claim_ids:
            if claim_id == "no_analytics_trackers":
                violations.extend(check_no_analytics_trackers(rel_path, lines))
            elif claim_id == "no_third_party_exfiltration":
                violations.extend(check_no_third_party_exfiltration(rel_path, lines, allowlist))
            elif claim_id == "no_pii_logging":
                violations.extend(check_no_pii_logging(rel_path, lines))

    violations = sorted(
        violations,
        key=lambda item: (item.file, item.line, item.claim_id, item.rule_id, item.evidence),
    )
    return violations, len(files)


def render_text_report(report: Mapping[str, Any]) -> str:
    lines = [
        "Privacy Policy Consistency Check",
        f"Policy: {report['policy_path']}",
        f"Repo Root: {report['repo_root']}",
        f"Claims In Policy: {', '.join(report['claims_in_policy']) or '(none)'}",
        f"Claims Checked: {', '.join(report['claims_checked']) or '(none)'}",
        f"Scanned Files: {report['scanned_files']}",
    ]

    if report["unsupported_claims"]:
        lines.append(f"Unsupported Claims: {', '.join(report['unsupported_claims'])}")

    if report["warnings"]:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in report["warnings"])

    if report["errors"]:
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in report["errors"])

    violations = report["violations"]
    if violations:
        lines.append("Violations:")
        for violation in violations:
            lines.append(
                f"- [{violation['claim_id']}] {violation['file']}:{violation['line']} :: {violation['evidence']}"
            )
    else:
        lines.append("Violations: none")

    lines.append(f"Result: {report['status'].upper()}")
    return "\n".join(lines)


def render_json_report(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def resolve_policy_path(repo_root: Path, config: Mapping[str, Any], explicit_path: str | None) -> Path:
    candidate = explicit_path or config.get("policy", {}).get("defaultPath", "PRIVACY_POLICY.md")
    path = Path(str(candidate))
    if not path.is_absolute():
        path = (repo_root / path).resolve()
    return path


def build_report(
    repo_root: Path,
    policy_path: Path,
    config: Mapping[str, Any],
    ignore_unsupported_claims: bool,
) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []

    if not repo_root.exists() or not repo_root.is_dir():
        errors.append(f"Repository root does not exist or is not a directory: {repo_root}")

    claim_ids: list[str] = []
    if not errors:
        if not policy_path.exists() or not policy_path.is_file():
            errors.append(f"Policy file not found: {policy_path}")
        else:
            try:
                policy_text = policy_path.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"Unable to read policy file: {exc}")
            else:
                claim_ids = extract_claim_ids(policy_text)

    supported_claims, unsupported_claims = classify_claims(claim_ids)

    if unsupported_claims:
        message = "Unsupported claim IDs in policy: " + ", ".join(unsupported_claims)
        if ignore_unsupported_claims:
            warnings.append(message)
        else:
            errors.append(message)

    violations: list[Violation] = []
    scanned_files = 0
    if not errors and supported_claims:
        violations, scanned_files = run_claim_checks(supported_claims, repo_root, config)
    elif not errors and not supported_claims:
        warnings.append("No supported privacy claims found in policy. Nothing to enforce.")

    status = "pass"
    if errors:
        status = "error"
    elif violations:
        status = "fail"

    return {
        "repo_root": repo_root.as_posix(),
        "policy_path": policy_path.as_posix(),
        "claims_in_policy": claim_ids,
        "claims_checked": supported_claims,
        "unsupported_claims": unsupported_claims,
        "scanned_files": scanned_files,
        "violations": [
            {
                "claim_id": violation.claim_id,
                "file": violation.file,
                "line": violation.line,
                "evidence": violation.evidence,
                "rule_id": violation.rule_id,
            }
            for violation in violations
        ],
        "violation_count": len(violations),
        "warnings": warnings,
        "errors": errors,
        "status": status,
    }


def exit_code_for_report(report: Mapping[str, Any]) -> int:
    if report["status"] == "error":
        return 2
    if report["status"] == "fail":
        return 1
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="Repository root to scan (default: current directory)")
    parser.add_argument("--policy-path", help="Path to policy markdown file (default from config)")
    parser.add_argument("--config", help="Path to checker config JSON")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Report format")
    parser.add_argument("--output", help="Optional file path to write report output")
    parser.add_argument(
        "--ignore-unsupported-claims",
        action="store_true",
        help="Treat unsupported claim IDs as warnings instead of errors",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    repo_root = Path(args.repo_root).resolve()

    try:
        config, _ = load_config(Path(args.config).resolve() if args.config else None)
    except (OSError, ValueError, json.JSONDecodeError, FileNotFoundError) as exc:
        report = {
            "repo_root": repo_root.as_posix(),
            "policy_path": "",
            "claims_in_policy": [],
            "claims_checked": [],
            "unsupported_claims": [],
            "scanned_files": 0,
            "violations": [],
            "violation_count": 0,
            "warnings": [],
            "errors": [f"Failed to load config: {exc}"],
            "status": "error",
        }
        rendered = render_json_report(report) if args.format == "json" else render_text_report(report)
        print(rendered)
        return 2

    policy_path = resolve_policy_path(repo_root, config, args.policy_path)
    report = build_report(
        repo_root=repo_root,
        policy_path=policy_path,
        config=config,
        ignore_unsupported_claims=args.ignore_unsupported_claims,
    )

    rendered = render_json_report(report) if args.format == "json" else render_text_report(report)

    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = (Path.cwd() / output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")

    print(rendered)
    return exit_code_for_report(report)


if __name__ == "__main__":
    raise SystemExit(main())
