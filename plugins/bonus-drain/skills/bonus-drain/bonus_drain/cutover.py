"""Read-only cutover inspection and the separate legacy md/jsonl importer.

Database/unit cutover is intentionally report-only in code.  Applying or rolling
back a cutover changes live writers and must be performed by an operator following
MIGRATION.md.  This module never stops, masks, enables, copies, or removes units.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote


class MigrationError(RuntimeError):
    """A migration input or precondition is invalid."""


class ManualActionRequired(MigrationError):
    """The requested state-changing operation is deliberately not automated."""


@dataclass(frozen=True)
class CutoverReport:
    dry_run: bool
    source_database: Path
    source_units: tuple[Path, ...]
    destination: Path
    tables: tuple[str, ...]
    writers: tuple[str, ...]
    steps: tuple[str, ...]
    warnings: tuple[str, ...]
    writer_probe_performed: bool = False

    @property
    def ready_for_manual_apply(self) -> bool:
        return self.writer_probe_performed and not self.writers and not self.warnings

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "source_database": str(self.source_database),
            "source_units": [str(path) for path in self.source_units],
            "destination": str(self.destination),
            "tables": list(self.tables),
            "writers": list(self.writers),
            "steps": list(self.steps),
            "warnings": list(self.warnings),
            "writer_probe_performed": self.writer_probe_performed,
            "ready_for_manual_apply": self.ready_for_manual_apply,
        }


@dataclass(frozen=True)
class ImportResult:
    tasks: int
    runs: int
    skipped_lines: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"tasks": self.tasks, "runs": self.runs, "skipped_lines": self.skipped_lines}


_LEGACY_UNIT_RE = re.compile(
    r"(?:bonus[-_]drain|cc-bonus-scout|cc-weekly-anchor|jobs-viewer)", re.IGNORECASE
)
_OWNED_UNIT_NAMES = {
    "bonus-drain-refresh.service",
    "bonus-drain-refresh.timer",
    "bonus-drain-scout.service",
    "bonus-drain-scout.timer",
    "bonus-drain-viewer.service",
}
_PRIORITY = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
_FIELD = {
    "Priority": "priority",
    "Cadence": "cadence",
    "Cwd": "cwd",
    "Goal": "goal",
    "Context": "context",
    "Constraints": "constraints",
    "Precondition": "precondition",
    "Done when": "done_when",
}


def _sqlite_tables(path: Path) -> tuple[str, ...]:
    try:
        if path.read_bytes()[:16] != b"SQLite format 3\x00":
            raise MigrationError(
                f"{path} is not a SQLite database; markdown/jsonl sources use import-legacy"
            )
        encoded_path = quote(str(path.resolve()), safe="/")
        uri = f"file:{encoded_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
    except MigrationError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise MigrationError(f"cannot inspect source database {path}: {exc}") from exc
    names = tuple(str(row[0]) for row in rows)
    if "tasks" not in names or "runs" not in names:
        raise MigrationError(f"source database lacks tasks/runs tables: {path}")
    return names


def _unit_paths(value: str | Path | bool | None) -> tuple[Path, ...]:
    if value in (None, False):
        return ()
    root = Path.home() / ".config" / "systemd" / "user" if value is True else Path(value)
    root = root.expanduser()
    if not root.exists():
        return ()
    if root.is_symlink():
        raise MigrationError(f"unit source must not be a symlink: {root}")
    candidates: Iterable[Path]
    if root.is_dir():
        candidates = sorted(root.iterdir())
    else:
        candidates = (root,)
    return tuple(
        path for path in candidates
        if path.is_file() and not path.is_symlink()
        and path.suffix in {".service", ".timer"}
        and path.name not in _OWNED_UNIT_NAMES
        and _LEGACY_UNIT_RE.search(path.name)
    )


def migrate(
    *,
    from_db: str | Path,
    from_units: str | Path | bool | None,
    destination: str | Path,
    dry_run: bool,
    writer_probe: Callable[[], Iterable[Any]] | None = None,
) -> CutoverReport:
    """Inspect and render a manual cutover plan without writing any path."""

    if not dry_run:
        raise ManualActionRequired(
            "cutover apply is operator-only; run with --dry-run and follow MIGRATION.md"
        )
    source = Path(from_db).expanduser()
    if source.suffix.lower() in {".md", ".markdown", ".jsonl"}:
        raise MigrationError("markdown/jsonl migration is a separate import-legacy operation")
    if not source.is_file() or source.is_symlink():
        raise MigrationError(f"source database missing or unsafe: {source}")
    destination_path = Path(destination).expanduser()
    if destination_path.exists() and destination_path.is_symlink():
        raise MigrationError(f"destination must not be a symlink: {destination_path}")
    tables = _sqlite_tables(source)
    units = _unit_paths(from_units)
    try:
        raw_writers = list(writer_probe() if writer_probe is not None else ())
    except Exception as exc:
        raise MigrationError(f"writer probe failed: {exc}") from exc
    writers = tuple(str(item) for item in raw_writers if str(item).strip())
    warnings: list[str] = []
    if writer_probe is None:
        warnings.append("source-database writer probe was not performed; writer absence is unproven")
    if writers:
        warnings.append("active source-database writers must be reconciled before manual apply")
    if not units:
        warnings.append("no legacy units were discovered; record their enablement and mask state manually")

    unit_names = ", ".join(path.name for path in units) or "the inventoried legacy units"
    steps = (
        f"Record status, enablement, mask state, and hashes for {unit_names}.",
        "Stop the legacy scout, weekly anchor, refresher, and viewer units explicitly.",
        "Mask every legacy writer unit so a timer or dependency cannot restart it.",
        "Prove no process has the source DB open for write; reconcile every writer shown above.",
        "Create timestamped, mode-preserving backups of the DB, config, and unit-state inventory.",
        f"Install the versioned runtime and target the XDG destination under {destination_path}.",
        "Run doctor and read-only status checks before enabling the new refresh/scout timers.",
        "Keep the old units masked through one observed cycle; do not run old and new writers together.",
        "Rollback manually by stopping/masking new units, restoring the backup and prior unit states, then rechecking writers.",
    )
    return CutoverReport(
        dry_run=True,
        source_database=source,
        source_units=units,
        destination=destination_path,
        tables=tables,
        writers=writers,
        steps=steps,
        warnings=tuple(warnings),
        writer_probe_performed=writer_probe is not None,
    )


def apply(*_args: Any, **_kwargs: Any) -> None:
    raise ManualActionRequired("automated cutover apply is disabled; follow MIGRATION.md")


def rollback(*_args: Any, **_kwargs: Any) -> None:
    raise ManualActionRequired("automated rollback is disabled; follow MIGRATION.md")


def _parse_backlog(path: Path) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    section = "oneoff"
    current: dict[str, Any] | None = None
    current_field: str | tuple[str, str] | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MigrationError(f"cannot read legacy backlog {path}: {exc}") from exc
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("# Recurring"):
            section, current, current_field = "recurring", None, None
            continue
        if line.startswith("# Bonus Backlog"):
            section, current, current_field = "oneoff", None, None
            continue
        if line.lstrip().startswith("<!--"):
            continue
        heading = re.match(r"##\s+([A-Za-z0-9][A-Za-z0-9_-]*):?\s*(.*)", line)
        checkbox = re.match(r"\s*-\s*\[[ xX]\]\s*([A-Za-z0-9][A-Za-z0-9_-]*):?\s*(.*)", line)
        match = heading or checkbox
        if match:
            identifier, title = match.group(1), match.group(2).strip()
            if identifier == "slug-id" or identifier.startswith("<"):
                current = None
                continue
            current = {
                "id": identifier,
                "title": title or identifier,
                "kind": section,
                "priority": 2,
                "extra": {},
            }
            if checkbox and title:
                current["goal"] = title
            tasks.append(current)
            current_field = None
            continue
        if current is None:
            continue
        field_match = re.match(r"\s*-\s+\*\*(.+?):\*\*\s*(.*)", line)
        if field_match:
            label, value = field_match.group(1).strip(), field_match.group(2).strip()
            column = _FIELD.get(label)
            if column:
                current[column] = value
                current_field = column
            else:
                current["extra"][label] = value
                current_field = ("extra", label)
            continue
        if current_field and line.strip():
            if isinstance(current_field, tuple):
                key = current_field[1]
                current["extra"][key] = (current["extra"].get(key, "") + "\n" + line).strip()
            else:
                current[current_field] = (current.get(current_field, "") + "\n" + line).strip()
    return tasks


def _task_payload(task: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(task.get("kind") or "oneoff")
    raw_priority = task.get("priority", 2)
    if isinstance(raw_priority, str):
        priority = _PRIORITY.get(raw_priority.strip().upper(), 2)
    else:
        try:
            priority = int(raw_priority)
        except (TypeError, ValueError):
            priority = 2
    context = str(task.get("context") or "")
    for label, value in task.get("extra", {}).items():
        context = (context + f"\n\n**{label}:** {value}").strip()
    return {
        "id": str(task["id"]),
        "title": str(task.get("title") or task["id"]),
        "kind": kind,
        "priority": min(4, max(0, priority)),
        "cadence": str(task.get("cadence") or "weekly").lower() if kind == "recurring" else None,
        "cwd": str(task.get("cwd") or ""),
        "goal": str(task.get("goal") or task.get("title") or task["id"]),
        "context": context or None,
        "constraints": task.get("constraints") or None,
        "precondition": task.get("precondition") or None,
        "done_when": task.get("done_when") or None,
        "active": True,
    }


def _existing_task_ids(queue: Any) -> set[str]:
    callback = getattr(queue, "tasks", None)
    if not callable(callback):
        return set()
    try:
        rows = callback()
    except Exception:
        return set()
    result = set()
    for row in rows or ():
        value = row.get("id") if isinstance(row, Mapping) else getattr(row, "id", None)
        if isinstance(value, str):
            result.add(value)
    return result


def _import_event(queue: Any, event: Mapping[str, Any]) -> bool:
    callback = getattr(queue, "import_legacy_event", None)
    if callable(callback):
        callback(dict(event))
        return True
    callback = getattr(queue, "record", None)
    if not callable(callback):
        return False
    task = event.get("task") or event.get("task_id")
    status = event.get("status")
    if not isinstance(task, str) or not isinstance(status, str):
        return False
    cycle = event.get("cycle", 0)
    provider = str(event.get("provider_id") or event.get("engine") or "legacy")
    account = str(event.get("account_id") or "legacy")
    eligibility_key = str(event.get("eligibility_key") or f"legacy/{cycle}")
    kwargs = {
        "status": status,
        "provider_id": provider,
        "account_id": account,
        "kind": event.get("kind"),
        "summary": event.get("summary"),
        "branch": event.get("branch"),
        "timestamp": event.get("ts"),
    }
    # QueueDB.record implementations may expose only the durable generic core fields.
    compact = {key: value for key, value in kwargs.items() if value is not None}
    try:
        callback(task, eligibility_key, **compact)
    except TypeError:
        minimal = {key: compact[key] for key in ("status", "provider_id", "account_id") if key in compact}
        callback(task, eligibility_key, **minimal)
    return True


def import_legacy(backlog: str | Path, runs: str | Path, queue: Any) -> ImportResult:
    """Import markdown/jsonl into an initialized queue; never performs unit cutover."""

    backlog_path = Path(backlog).expanduser()
    runs_path = Path(runs).expanduser()
    tasks = _parse_backlog(backlog_path)
    existing = _existing_task_ids(queue)
    task_count = 0
    for task in tasks:
        payload = _task_payload(task)
        if payload["id"] in existing:
            continue
        queue.add_task(payload)
        existing.add(payload["id"])
        task_count += 1

    run_count = 0
    skipped = 0
    try:
        lines = runs_path.read_text(encoding="utf-8").splitlines() if runs_path.exists() else []
    except OSError as exc:
        raise MigrationError(f"cannot read legacy runs {runs_path}: {exc}") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError, json.JSONDecodeError):
            skipped += 1
            continue
        if not isinstance(event, dict) or not _import_event(queue, event):
            skipped += 1
            continue
        run_count += 1
    return ImportResult(task_count, run_count, skipped)
