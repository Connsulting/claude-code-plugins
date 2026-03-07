#!/usr/bin/env python3
"""Collect deterministic git diff context for semantic change explanations."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


DEFAULT_UNIFIED_LINES = 6
DEFAULT_MAX_PATCH_CHARS = 6000
DEFAULT_MAX_FILES = 60

STATUS_LABELS = {
    "A": "added",
    "C": "copied",
    "D": "deleted",
    "M": "modified",
    "R": "renamed",
    "T": "type-changed",
    "U": "unmerged",
    "X": "unknown",
}


@dataclass(frozen=True)
class DiffSelection:
    mode: str
    range_expr: Optional[str] = None
    base_ref: Optional[str] = None
    head_ref: Optional[str] = None
    operator: Optional[str] = None


class ArgumentError(ValueError):
    """Raised when CLI arguments are invalid."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Argparse variant that raises instead of exiting on parse failures."""

    def error(self, message: str) -> None:
        raise ArgumentError(message)


def _decode_text(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


def _run_git(repo_root: str, args: List[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", repo_root, *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _resolve_repo_root(cwd: str) -> str:
    probe = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if probe.returncode != 0:
        raise RuntimeError("Current directory is not inside a git repository.")
    return _decode_text(probe.stdout).strip()


def parse_range_expression(range_expr: str) -> DiffSelection:
    raw = range_expr.strip()
    if not raw:
        raise ArgumentError("Range cannot be empty.")

    if "..." in raw:
        base, head = raw.split("...", 1)
        operator = "..."
        normalized = raw
    elif ".." in raw:
        base, head = raw.split("..", 1)
        operator = ".."
        normalized = raw
    else:
        base = raw
        head = "HEAD"
        operator = ".."
        normalized = f"{base}..{head}"

    if not base or not head:
        raise ArgumentError(
            "Invalid range format. Use <base>..<head>, <base>...<head>, or a single base ref."
        )

    return DiffSelection(
        mode="range",
        range_expr=normalized,
        base_ref=base,
        head_ref=head,
        operator=operator,
    )


def resolve_selection(args: argparse.Namespace) -> DiffSelection:
    if args.range and (args.staged or args.working_tree):
        raise ArgumentError("Use either --range or --staged/--working-tree, not both.")
    if args.staged and args.working_tree:
        raise ArgumentError("Use either --staged or --working-tree, not both.")

    if args.range:
        return parse_range_expression(args.range)
    if args.staged:
        return DiffSelection(mode="staged")
    if args.working_tree:
        return DiffSelection(mode="working_tree")
    return DiffSelection(mode="workspace")


def _ref_exists(repo_root: str, ref_name: str) -> bool:
    probe = _run_git(repo_root, ["rev-parse", "--verify", "--quiet", f"{ref_name}^{{commit}}"])
    return probe.returncode == 0


def validate_selection(repo_root: str, selection: DiffSelection) -> Optional[Dict[str, Any]]:
    if selection.mode == "range":
        missing_refs: List[str] = []
        assert selection.base_ref is not None
        assert selection.head_ref is not None

        if not _ref_exists(repo_root, selection.base_ref):
            missing_refs.append(selection.base_ref)
        if not _ref_exists(repo_root, selection.head_ref):
            missing_refs.append(selection.head_ref)

        if missing_refs:
            refs = ", ".join(missing_refs)
            return error_payload(
                code="invalid_ref",
                message=f"Could not resolve git ref(s): {refs}.",
                mode=selection.mode,
                hint="Verify refs with `git show <ref>` or use a valid diff range.",
            )
        return None

    if selection.mode == "workspace" and not _ref_exists(repo_root, "HEAD"):
        return error_payload(
            code="missing_head",
            message="`HEAD` is not available in this repository yet.",
            mode=selection.mode,
            hint="Create the first commit or use --staged/--working-tree explicitly.",
        )

    return None


def build_diff_selector(selection: DiffSelection) -> List[str]:
    if selection.mode == "range":
        assert selection.range_expr is not None
        return [selection.range_expr]
    if selection.mode == "staged":
        return ["--cached"]
    if selection.mode == "working_tree":
        return []
    return ["HEAD"]


def parse_name_status_z(payload: bytes) -> List[Dict[str, Any]]:
    tokens = payload.split(b"\x00")
    if tokens and tokens[-1] == b"":
        tokens = tokens[:-1]

    rows: List[Dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        raw_status = _decode_text(tokens[index]).strip()
        index += 1
        if not raw_status:
            continue

        status_code = raw_status[0]
        similarity_text = raw_status[1:]
        similarity = int(similarity_text) if similarity_text.isdigit() else None

        if status_code in {"R", "C"}:
            if index + 1 >= len(tokens):
                break
            old_path = _decode_text(tokens[index])
            new_path = _decode_text(tokens[index + 1])
            index += 2
            rows.append(
                {
                    "path": new_path,
                    "old_path": old_path,
                    "status": status_code,
                    "raw_status": raw_status,
                    "similarity": similarity,
                }
            )
            continue

        if index >= len(tokens):
            break
        path = _decode_text(tokens[index])
        index += 1
        rows.append(
            {
                "path": path,
                "old_path": None,
                "status": status_code,
                "raw_status": raw_status,
                "similarity": similarity,
            }
        )

    return rows


def _parse_num(value: str) -> Optional[int]:
    if value == "-":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_numstat_z(payload: bytes) -> Dict[str, Dict[str, Any]]:
    tokens = payload.split(b"\x00")
    if tokens and tokens[-1] == b"":
        tokens = tokens[:-1]

    parsed: Dict[str, Dict[str, Any]] = {}
    index = 0
    while index < len(tokens):
        row = _decode_text(tokens[index])
        index += 1
        if not row:
            continue

        fields = row.split("\t")
        if len(fields) < 3:
            continue

        additions_text = fields[0]
        deletions_text = fields[1]
        path_field = "\t".join(fields[2:])
        old_path: Optional[str] = None

        if path_field == "":
            if index + 1 >= len(tokens):
                break
            old_path = _decode_text(tokens[index])
            path_field = _decode_text(tokens[index + 1])
            index += 2

        parsed[path_field] = {
            "additions": _parse_num(additions_text),
            "deletions": _parse_num(deletions_text),
            "is_binary": additions_text == "-" or deletions_text == "-",
            "old_path": old_path,
        }

    return parsed


def _patch_key_from_diff_header(line: str) -> Optional[str]:
    if not line.startswith("diff --git "):
        return None

    try:
        parts = shlex.split(line.strip())
    except ValueError:
        return None

    if len(parts) < 4:
        return None

    left = parts[2]
    right = parts[3]
    if left.startswith("a/"):
        left = left[2:]
    if right.startswith("b/"):
        right = right[2:]

    if right == "/dev/null":
        return left
    if left == "/dev/null":
        return right
    return right


def split_patch_sections(patch_text: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current_key: Optional[str] = None
    current_lines: List[str] = []

    for line in patch_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_key is not None and current_lines:
                chunk = "".join(current_lines)
                if current_key in sections:
                    sections[current_key] = f"{sections[current_key]}\n{chunk}"
                else:
                    sections[current_key] = chunk

            current_key = _patch_key_from_diff_header(line)
            current_lines = [line] if current_key else []
            continue

        if current_key is not None:
            current_lines.append(line)

    if current_key is not None and current_lines:
        chunk = "".join(current_lines)
        if current_key in sections:
            sections[current_key] = f"{sections[current_key]}\n{chunk}"
        else:
            sections[current_key] = chunk

    return sections


def truncate_patch(patch_text: str, max_chars: int) -> tuple[str, bool, int]:
    if len(patch_text) <= max_chars:
        return patch_text, False, 0

    omitted = len(patch_text) - max_chars
    marker = f"\n... [truncated {omitted} chars]\n"
    head_limit = max(0, max_chars - len(marker))
    return f"{patch_text[:head_limit]}{marker}", True, omitted


def error_payload(*, code: str, message: str, mode: str, hint: Optional[str] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema_version": "semantic-diff.v1",
        "status": "error",
        "code": code,
        "mode": mode,
        "message": message,
    }
    if hint:
        payload["hint"] = hint
    return payload


def _selection_range_payload(selection: DiffSelection) -> Optional[Dict[str, str]]:
    if selection.mode != "range":
        return None
    return {
        "base": selection.base_ref or "",
        "head": selection.head_ref or "",
        "operator": selection.operator or "..",
        "expression": selection.range_expr or "",
    }


def collect_semantic_diff(
    *,
    repo_root: str,
    selection: DiffSelection,
    unified_lines: int = DEFAULT_UNIFIED_LINES,
    max_patch_chars: int = DEFAULT_MAX_PATCH_CHARS,
    max_files: int = DEFAULT_MAX_FILES,
) -> Dict[str, Any]:
    selector = build_diff_selector(selection)
    git_diff_prefix = ["diff", "--no-ext-diff", "--find-renames=90%"]

    name_status_proc = _run_git(
        repo_root,
        [*git_diff_prefix, "--name-status", "-z", *selector],
    )
    if name_status_proc.returncode != 0:
        return error_payload(
            code="git_diff_failed",
            message=_decode_text(name_status_proc.stderr).strip() or "git diff failed.",
            mode=selection.mode,
            hint="Confirm the repository and diff parameters are valid.",
        )

    numstat_proc = _run_git(
        repo_root,
        [*git_diff_prefix, "--numstat", "-z", *selector],
    )
    if numstat_proc.returncode != 0:
        return error_payload(
            code="git_numstat_failed",
            message=_decode_text(numstat_proc.stderr).strip() or "git diff --numstat failed.",
            mode=selection.mode,
            hint="Retry with a smaller range or verify repository integrity.",
        )

    patch_proc = _run_git(
        repo_root,
        [*git_diff_prefix, "--patch", "--no-color", f"--unified={unified_lines}", *selector],
    )
    if patch_proc.returncode != 0:
        return error_payload(
            code="git_patch_failed",
            message=_decode_text(patch_proc.stderr).strip() or "git diff --patch failed.",
            mode=selection.mode,
            hint="Retry with a smaller range or verify repository integrity.",
        )

    name_rows = parse_name_status_z(name_status_proc.stdout)
    numstat_map = parse_numstat_z(numstat_proc.stdout)
    patch_map = split_patch_sections(_decode_text(patch_proc.stdout))

    files_by_path: Dict[str, Dict[str, Any]] = {}
    for row in name_rows:
        path = row["path"]
        files_by_path[path] = {
            "path": path,
            "status": row["status"],
            "change_type": STATUS_LABELS.get(row["status"], "unknown"),
            "raw_status": row["raw_status"],
            "old_path": row["old_path"],
            "similarity": row["similarity"],
            "additions": 0,
            "deletions": 0,
            "is_binary": False,
            "patch": "",
            "patch_truncated": False,
            "patch_chars": 0,
            "truncated_chars": 0,
        }

    for path, stats in numstat_map.items():
        if path not in files_by_path:
            files_by_path[path] = {
                "path": path,
                "status": "M",
                "change_type": STATUS_LABELS["M"],
                "raw_status": "M",
                "old_path": stats.get("old_path"),
                "similarity": None,
                "additions": 0,
                "deletions": 0,
                "is_binary": False,
                "patch": "",
                "patch_truncated": False,
                "patch_chars": 0,
                "truncated_chars": 0,
            }

        files_by_path[path]["additions"] = stats["additions"] if stats["additions"] is not None else 0
        files_by_path[path]["deletions"] = stats["deletions"] if stats["deletions"] is not None else 0
        files_by_path[path]["is_binary"] = bool(stats["is_binary"])
        if files_by_path[path]["old_path"] is None and stats.get("old_path"):
            files_by_path[path]["old_path"] = stats["old_path"]

    for path, patch_text in patch_map.items():
        if path not in files_by_path:
            files_by_path[path] = {
                "path": path,
                "status": "M",
                "change_type": STATUS_LABELS["M"],
                "raw_status": "M",
                "old_path": None,
                "similarity": None,
                "additions": 0,
                "deletions": 0,
                "is_binary": False,
                "patch": "",
                "patch_truncated": False,
                "patch_chars": 0,
                "truncated_chars": 0,
            }

        truncated_patch, was_truncated, omitted_chars = truncate_patch(patch_text, max_patch_chars)
        files_by_path[path]["patch"] = truncated_patch
        files_by_path[path]["patch_truncated"] = was_truncated
        files_by_path[path]["patch_chars"] = len(truncated_patch)
        files_by_path[path]["truncated_chars"] = omitted_chars

    ordered_files = sorted(files_by_path.values(), key=lambda item: item["path"])
    total_files = len(ordered_files)

    omitted_count = 0
    if total_files > max_files:
        omitted_count = total_files - max_files
        ordered_files = ordered_files[:max_files]

    untracked_proc = _run_git(repo_root, ["ls-files", "--others", "--exclude-standard"])
    untracked_files = []
    if untracked_proc.returncode == 0:
        untracked_files = sorted(
            [line for line in _decode_text(untracked_proc.stdout).splitlines() if line.strip()]
        )

    total_insertions = sum(max(0, int(file_row.get("additions") or 0)) for file_row in files_by_path.values())
    total_deletions = sum(max(0, int(file_row.get("deletions") or 0)) for file_row in files_by_path.values())
    binary_files = sum(1 for file_row in files_by_path.values() if file_row.get("is_binary"))
    renamed_files = sum(1 for file_row in files_by_path.values() if file_row.get("status") == "R")
    truncated_files = sum(1 for file_row in ordered_files if file_row.get("patch_truncated"))
    status_counts = Counter(file_row.get("status", "M") for file_row in files_by_path.values())

    notes: List[str] = []
    if omitted_count:
        notes.append(f"Omitted {omitted_count} file(s) due to --max-files={max_files}.")
    if untracked_files:
        notes.append(
            "Untracked files are listed separately and are not included in git diff output until staged."
        )

    base_payload: Dict[str, Any] = {
        "schema_version": "semantic-diff.v1",
        "status": "ok",
        "mode": selection.mode,
        "range": _selection_range_payload(selection),
        "repo_root": repo_root,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limits": {
            "max_files": max_files,
            "max_patch_chars": max_patch_chars,
            "unified_lines": unified_lines,
        },
        "summary": {
            "files_changed": total_files,
            "files_included": len(ordered_files),
            "files_omitted": omitted_count,
            "insertions": total_insertions,
            "deletions": total_deletions,
            "renamed_files": renamed_files,
            "binary_files": binary_files,
            "truncated_patches": truncated_files,
            "status_counts": dict(sorted(status_counts.items())),
        },
        "files": ordered_files,
        "untracked_files": untracked_files,
        "notes": notes,
    }

    if total_files == 0:
        base_payload["status"] = "empty"
        base_payload["message"] = (
            "No tracked code changes detected for the selected diff. "
            "If you only changed new files, stage them first with `git add`."
        )

    return base_payload


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description=(
            "Collect structured git diff context for semantic explanation. "
            "Default mode compares workspace changes against HEAD."
        )
    )
    parser.add_argument(
        "--range",
        dest="range",
        default=None,
        help="Diff range (<base>..<head>, <base>...<head>, or single <base> to compare against HEAD).",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Compare staged changes against HEAD.",
    )
    parser.add_argument(
        "--working-tree",
        action="store_true",
        help="Compare unstaged working tree changes against index.",
    )
    parser.add_argument(
        "--cwd",
        default=os.getcwd(),
        help="Directory inside target git repository (default: current working directory).",
    )
    parser.add_argument(
        "--unified",
        type=int,
        default=DEFAULT_UNIFIED_LINES,
        help=f"Unified context lines for patch snippets (default: {DEFAULT_UNIFIED_LINES}).",
    )
    parser.add_argument(
        "--max-patch-chars",
        type=int,
        default=DEFAULT_MAX_PATCH_CHARS,
        help=f"Maximum characters per file patch snippet (default: {DEFAULT_MAX_PATCH_CHARS}).",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help=f"Maximum number of files to include in output (default: {DEFAULT_MAX_FILES}).",
    )
    return parser


def _print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except ArgumentError as exc:
        _print_json(
            error_payload(
                code="invalid_arguments",
                message=str(exc),
                mode="unknown",
                hint="Run with --help to see valid flags and combinations.",
            )
        )
        return 2

    if args.unified < 0:
        _print_json(
            error_payload(
                code="invalid_arguments",
                message="--unified must be >= 0.",
                mode="unknown",
            )
        )
        return 2

    if args.max_patch_chars < 200:
        _print_json(
            error_payload(
                code="invalid_arguments",
                message="--max-patch-chars must be >= 200.",
                mode="unknown",
            )
        )
        return 2

    if args.max_files < 1:
        _print_json(
            error_payload(
                code="invalid_arguments",
                message="--max-files must be >= 1.",
                mode="unknown",
            )
        )
        return 2

    try:
        selection = resolve_selection(args)
        repo_root = _resolve_repo_root(args.cwd)
    except ArgumentError as exc:
        _print_json(
            error_payload(
                code="invalid_arguments",
                message=str(exc),
                mode="unknown",
                hint="Run with --help to see valid flags and combinations.",
            )
        )
        return 2
    except RuntimeError as exc:
        _print_json(
            error_payload(
                code="not_git_repo",
                message=str(exc),
                mode="unknown",
                hint="Run this command from within a git repository or pass --cwd <repo-path>.",
            )
        )
        return 1

    validation_error = validate_selection(repo_root, selection)
    if validation_error:
        _print_json(validation_error)
        return 1

    payload = collect_semantic_diff(
        repo_root=repo_root,
        selection=selection,
        unified_lines=args.unified,
        max_patch_chars=args.max_patch_chars,
        max_files=args.max_files,
    )

    _print_json(payload)
    return 1 if payload.get("status") == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
