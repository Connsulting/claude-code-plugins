#!/usr/bin/env python3
"""Validate machine-readable privacy claims against repository files."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple


FLAG_MAP: Dict[str, re.RegexFlag] = {
    "ASCII": re.ASCII,
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
}


def parse_args() -> argparse.Namespace:
    script_path = Path(__file__).resolve()
    plugin_root = script_path.parent.parent
    parser = argparse.ArgumentParser(
        description="Check compound-learning code/config consistency with privacy policy claims."
    )
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=plugin_root,
        help="Path to compound-learning plugin root (default: script-derived).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=plugin_root / "privacy-policy-claims.json",
        help="Path to privacy claims manifest JSON.",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=plugin_root / "PRIVACY_POLICY.md",
        help="Path to privacy policy markdown document.",
    )
    return parser.parse_args()


def _format_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _read_text(path: Path, text_cache: Dict[Path, str]) -> str:
    if path not in text_cache:
        text_cache[path] = path.read_text(encoding="utf-8")
    return text_cache[path]


def _read_json(path: Path, json_cache: Dict[Path, Any]) -> Any:
    if path not in json_cache:
        json_cache[path] = json.loads(path.read_text(encoding="utf-8"))
    return json_cache[path]


def _json_lookup(payload: Any, dotted_path: str) -> Tuple[bool, Any]:
    current = payload
    for segment in dotted_path.split("."):
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
            continue
        if isinstance(current, list):
            try:
                idx = int(segment)
            except ValueError:
                return False, None
            if idx < 0 or idx >= len(current):
                return False, None
            current = current[idx]
            continue
        return False, None
    return True, current


def _resolve_target(plugin_root: Path, relative_file: str) -> Path:
    target = (plugin_root / relative_file).resolve()
    try:
        target.relative_to(plugin_root.resolve())
    except ValueError:
        raise ValueError(f"target file escapes plugin root: {relative_file}")
    return target


def _normalize_claim_text(value: str) -> str:
    normalized = " ".join(value.replace("`", "").split()).strip()
    return normalized[:-1] if normalized.endswith(".") else normalized


def _parse_policy_claims_table(policy_path: Path) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    rows: Dict[str, Dict[str, str]] = {}
    errors: List[str] = []
    in_claims_section = False

    content = policy_path.read_text(encoding="utf-8")
    for raw_line in content.splitlines():
        line = raw_line.strip()
        lower_line = line.lower()

        if lower_line == "## claims":
            in_claims_section = True
            continue
        if in_claims_section and line.startswith("## "):
            break
        if not in_claims_section or not line.startswith("|"):
            continue

        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        if cells[0].lower() == "claim id":
            continue
        if all(re.fullmatch(r"[-: ]+", cell or "-") for cell in cells[:3]):
            continue

        claim_id = cells[0].strip("` ").strip()
        claim_text = cells[1]
        enforcement = cells[2].strip().lower()
        if not claim_id:
            errors.append("policy table contains a row with an empty claim ID")
            continue
        if claim_id in rows:
            errors.append(f"policy table contains duplicate claim ID: {claim_id}")
            continue

        rows[claim_id] = {
            "statement": claim_text,
            "enforcement": enforcement,
        }

    if not in_claims_section:
        errors.append("policy document missing required '## Claims' section")
    if not rows:
        errors.append("policy claims table has no claim rows")
    return rows, errors


def _validate_policy_manifest_alignment(
    claims: List[Any],
    policy_rows: Mapping[str, Mapping[str, str]],
) -> List[str]:
    errors: List[str] = []
    manifest_claims: Dict[str, Dict[str, str]] = {}

    for idx, raw_claim in enumerate(claims, start=1):
        if not isinstance(raw_claim, Mapping):
            errors.append(f"manifest claim at index {idx} must be an object")
            continue

        claim_id = str(raw_claim.get("id", "")).strip()
        if not claim_id:
            errors.append(f"manifest claim at index {idx} is missing a non-empty id")
            continue
        if claim_id in manifest_claims:
            errors.append(f"manifest contains duplicate claim ID: {claim_id}")
            continue

        enforcement = str(raw_claim.get("enforcement", "")).strip().lower()
        statement_raw = raw_claim.get("statement")
        if not isinstance(statement_raw, str) or not statement_raw.strip():
            errors.append(f"manifest claim {claim_id} is missing a non-empty statement")
            statement = ""
        else:
            statement = statement_raw.strip()

        manifest_claims[claim_id] = {
            "enforcement": enforcement,
            "statement": statement,
        }

    manifest_ids = set(manifest_claims)
    policy_ids = set(policy_rows)

    for claim_id in sorted(manifest_ids - policy_ids):
        errors.append(f"claim missing from policy table: {claim_id}")
    for claim_id in sorted(policy_ids - manifest_ids):
        errors.append(f"policy table claim not present in manifest: {claim_id}")

    for claim_id in sorted(manifest_ids & policy_ids):
        manifest_claim = manifest_claims[claim_id]
        policy_claim = policy_rows[claim_id]

        manifest_enforcement = manifest_claim["enforcement"]
        policy_enforcement = str(policy_claim.get("enforcement", "")).strip().lower()
        if manifest_enforcement != policy_enforcement:
            errors.append(
                (
                    f"claim enforcement mismatch for {claim_id} "
                    f"(manifest={manifest_enforcement!r}, policy={policy_enforcement!r})"
                )
            )

        manifest_statement = _normalize_claim_text(manifest_claim["statement"])
        policy_statement = _normalize_claim_text(str(policy_claim.get("statement", "")))
        if manifest_statement != policy_statement:
            errors.append(f"claim statement mismatch for {claim_id}")

    return errors


def _run_check(
    check: Mapping[str, Any],
    *,
    plugin_root: Path,
    text_cache: Dict[Path, str],
    json_cache: Dict[Path, Any],
) -> Tuple[bool, str]:
    check_type = str(check.get("type", "")).strip()
    relative_file = check.get("file")
    if not isinstance(relative_file, str) or not relative_file:
        return False, "missing required check field: file"

    try:
        target = _resolve_target(plugin_root, relative_file)
    except ValueError as exc:
        return False, str(exc)

    if check_type == "file_exists":
        return target.exists(), f"file does not exist: {relative_file}"

    if not target.exists():
        return False, f"target file not found: {relative_file}"

    if check_type == "contains":
        needle = check.get("value")
        if not isinstance(needle, str):
            return False, "contains check requires string field: value"
        haystack = _read_text(target, text_cache)
        if needle in haystack:
            return True, f"contains matched in {relative_file}"
        return False, f"expected string not found in {relative_file}: {needle!r}"

    if check_type == "regex":
        pattern = check.get("pattern")
        if not isinstance(pattern, str):
            return False, "regex check requires string field: pattern"

        flags_value = 0
        raw_flags = check.get("flags", [])
        if raw_flags is None:
            raw_flags = []
        if not isinstance(raw_flags, list):
            return False, "regex check field flags must be a list"
        for raw in raw_flags:
            if not isinstance(raw, str):
                return False, "regex check flags must be strings"
            flag_name = raw.strip().upper()
            if flag_name not in FLAG_MAP:
                return False, f"unsupported regex flag: {raw}"
            flags_value |= FLAG_MAP[flag_name]

        content = _read_text(target, text_cache)
        if re.search(pattern, content, flags=flags_value):
            return True, f"regex matched in {relative_file}"
        return False, f"regex did not match in {relative_file}: {pattern!r}"

    if check_type == "json_value":
        dotted_path = check.get("path")
        if not isinstance(dotted_path, str) or not dotted_path:
            return False, "json_value check requires string field: path"
        expected = check.get("equals")
        payload = _read_json(target, json_cache)
        found, actual = _json_lookup(payload, dotted_path)
        if not found:
            return False, f"json path not found in {relative_file}: {dotted_path}"
        if actual == expected:
            return True, f"json value matched in {relative_file}:{dotted_path}"
        return (
            False,
            (
                f"json value mismatch in {relative_file}:{dotted_path} "
                f"(expected {expected!r}, found {actual!r})"
            ),
        )

    return False, f"unsupported check type: {check_type!r}"


def evaluate_claims(manifest_path: Path, plugin_root: Path, policy_path: Path) -> int:
    try:
        manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"ERROR: manifest file not found: {_format_path(manifest_path)}")
        return 2
    except json.JSONDecodeError as exc:
        print(f"ERROR: failed to parse manifest JSON: {_format_path(manifest_path)}: {exc}")
        return 2

    claims = manifest_raw.get("claims")
    if not isinstance(claims, list):
        print("ERROR: manifest must contain a 'claims' list")
        return 2

    try:
        policy_rows, policy_parse_errors = _parse_policy_claims_table(policy_path)
    except FileNotFoundError:
        print(f"ERROR: policy file not found: {_format_path(policy_path)}")
        return 2

    alignment_errors = list(policy_parse_errors)
    alignment_errors.extend(_validate_policy_manifest_alignment(claims, policy_rows))

    text_cache: Dict[Path, str] = {}
    json_cache: Dict[Path, Any] = {}

    automated_pass = 0
    automated_fail = 0
    manual_review = 0
    errors: List[str] = []

    print("Privacy Policy Consistency Report")
    print(f"manifest: {_format_path(manifest_path)}")
    print(f"plugin_root: {_format_path(plugin_root)}")
    print(f"policy: {_format_path(policy_path)}")
    print("")

    if alignment_errors:
        print("[FAIL] POLICY-MANIFEST alignment")
        for item in alignment_errors:
            print(f"  reason: {item}")
            errors.append(f"POLICY-MANIFEST: {item}")
    else:
        print("[PASS] POLICY-MANIFEST alignment")
    print("")

    for claim in sorted(claims, key=lambda c: str(c.get("id", ""))):
        claim_id = str(claim.get("id", "<missing-id>"))
        title = str(claim.get("title", "")).strip() or "(untitled claim)"
        enforcement = str(claim.get("enforcement", "")).strip()

        if enforcement == "manual_review":
            manual_review += 1
            reason = str(claim.get("manual_review_reason", "")).strip()
            print(f"[MANUAL] {claim_id}: {title}")
            if reason:
                print(f"  reason: {reason}")
            continue

        if enforcement != "automated":
            automated_fail += 1
            msg = f"claim has unsupported enforcement mode {enforcement!r}"
            errors.append(f"{claim_id}: {msg}")
            print(f"[FAIL] {claim_id}: {title}")
            print(f"  reason: {msg}")
            continue

        checks = claim.get("checks")
        if not isinstance(checks, list) or not checks:
            automated_fail += 1
            msg = "automated claim must define a non-empty checks list"
            errors.append(f"{claim_id}: {msg}")
            print(f"[FAIL] {claim_id}: {title}")
            print(f"  reason: {msg}")
            continue

        check_failures: List[str] = []
        for idx, raw_check in enumerate(checks, start=1):
            if not isinstance(raw_check, Mapping):
                check_failures.append(f"check {idx}: check entry must be an object")
                continue
            ok, detail = _run_check(
                raw_check,
                plugin_root=plugin_root,
                text_cache=text_cache,
                json_cache=json_cache,
            )
            if not ok:
                check_failures.append(f"check {idx}: {detail}")

        if check_failures:
            automated_fail += 1
            print(f"[FAIL] {claim_id}: {title}")
            for failure in check_failures:
                print(f"  reason: {failure}")
                errors.append(f"{claim_id}: {failure}")
        else:
            automated_pass += 1
            print(f"[PASS] {claim_id}: {title}")

    total = automated_pass + automated_fail + manual_review
    print("")
    print(
        "Summary: "
        f"alignment_fail={len(alignment_errors)} "
        f"automated_pass={automated_pass} "
        f"automated_fail={automated_fail} "
        f"manual_review={manual_review} "
        f"total={total}"
    )

    if automated_fail or alignment_errors:
        print("")
        print("Claim mismatches detected:")
        for item in errors:
            print(f"- {item}")
        return 1
    return 0


def main() -> int:
    args = parse_args()
    plugin_root = args.plugin_root.resolve()
    manifest_path = args.manifest.resolve()
    policy_path = args.policy.resolve()
    return evaluate_claims(manifest_path, plugin_root, policy_path)


if __name__ == "__main__":
    raise SystemExit(main())
