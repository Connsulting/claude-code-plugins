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


def evaluate_claims(manifest_path: Path, plugin_root: Path) -> int:
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

    text_cache: Dict[Path, str] = {}
    json_cache: Dict[Path, Any] = {}

    automated_pass = 0
    automated_fail = 0
    manual_review = 0
    errors: List[str] = []

    print("Privacy Policy Consistency Report")
    print(f"manifest: {_format_path(manifest_path)}")
    print(f"plugin_root: {_format_path(plugin_root)}")
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
        f"automated_pass={automated_pass} "
        f"automated_fail={automated_fail} "
        f"manual_review={manual_review} "
        f"total={total}"
    )

    if automated_fail:
        print("")
        print("Automated claim mismatches detected:")
        for item in errors:
            print(f"- {item}")
        return 1
    return 0


def main() -> int:
    args = parse_args()
    plugin_root = args.plugin_root.resolve()
    manifest_path = args.manifest.resolve()
    return evaluate_claims(manifest_path, plugin_root)


if __name__ == "__main__":
    raise SystemExit(main())
