"""SQLite queue, run log, and atomic dispatch claims."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal, Mapping
from zoneinfo import ZoneInfo


class QueueError(RuntimeError):
    """Queue operation failed without changing its requested invariant."""


VALID_STATUSES = frozenset({"dispatched", "done", "skipped", "failed", "awaiting_human"})
TERMINAL_STATUSES = frozenset({"done", "skipped", "failed", "awaiting_human"})
ATTEMPT_STATES = frozenset({
    "claimed", "dispatched", "done", "skipped", "failed", "awaiting_human", "ambiguous", "aborted",
})
RECOVERY_MODES = frozenset({"retry", "verification"})
RECOVERY_STATES = frozenset({"scheduled", "backoff", "consumed", "held", "exhausted"})
TRANSIENT_RECOVERY_HOLDS = frozenset({
    "goal_paused",
    "goal_concurrency_held",
    "goal_coordinator_active",
    "goal_operation_unresolved",
})
REASON_CODES = frozenset({
    "retryable", "verification_needed", "authority_required", "permanent",
    "unknown_launch", "done_when_verified",
})
COMPLETION_MECHANISMS = frozenset({
    "command", "artifact", "operator_receipt", "goal_acceptance",
})
LEGACY_EXCLUSIVE_CAPABILITY = "legacy-exclusive"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TASK_SIZES = ("tiny", "small", "medium", "large", "huge")
CANONICAL_FABLE_MODEL = "claude-fable-5-1"
_FABLE_MODEL_ALIASES = frozenset({"fable", "claude-fable-5"})
RECURRING_COOLDOWNS_SECONDS = {
    "monthly": 28 * 24 * 60 * 60,
}

# Work groups are queue-navigation labels, not another place for a task title.  Keeping them
# short makes the work-group facet practical beside the readiness chips on narrow screens.
WORK_GROUP_MAX_LENGTH = 15


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise QueueError("outcome must contain JSON values") from exc
    if len(encoded.encode("utf-8")) > 65_536:
        raise QueueError("outcome exceeds 64 KiB")
    return encoded


def _bounded_text(value: Any, name: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise QueueError(f"outcome {name} must be non-empty text")
    value = value.strip()
    if len(value) > 2_000:
        raise QueueError(f"outcome {name} is too long")
    return value or None


def validate_outcome(
    status: str,
    outcome: Mapping[str, Any] | None,
    *,
    require_structured_reason: bool = True,
) -> dict[str, Any] | None:
    """Validate and canonicalize attempt outcome evidence.

    A run that opened or updated a PR records done with an artifact completion naming the PR.
    awaiting_human parks work for Brian and must never claim verified completion.
    Legacy callers can omit outcomes only when ``require_structured_reason`` is false.
    """

    if outcome is None:
        if status in TERMINAL_STATUSES and require_structured_reason:
            raise QueueError(f"{status} requires a structured outcome")
        return None
    if not isinstance(outcome, Mapping):
        raise QueueError("outcome must be a JSON object")
    value = dict(outcome)
    reason = value.get("reason")
    if status in TERMINAL_STATUSES and require_structured_reason:
        if not isinstance(reason, Mapping):
            if status == "done":
                raise QueueError("done requires verified completion evidence and a structured reason")
            raise QueueError(f"{status} requires a structured reason")
        code = reason.get("code")
        if code not in REASON_CODES:
            raise QueueError("outcome reason code is unsupported")
        detail = _bounded_text(reason.get("detail"), "reason.detail")
        signature = _bounded_text(reason.get("signature"), "reason.signature", required=False)
        if not signature:
            normalized = re.sub(r"[^a-z0-9]+", ":", str(detail).lower()).strip(":")[:500]
            signature = f"{code}:{normalized}"
        value["reason"] = {"code": code, "detail": detail, "signature": signature}
    if status == "awaiting_human" and require_structured_reason:
        if value["reason"]["code"] == "done_when_verified":
            raise QueueError("awaiting_human cannot use the done_when_verified reason code")
        completion = value.get("completion")
        if isinstance(completion, Mapping) and completion.get("verified") is True:
            raise QueueError("awaiting_human must not claim verified completion")
    if status == "done":
        if require_structured_reason and value.get("reason", {}).get("code") != "done_when_verified":
            raise QueueError("done requires a done_when_verified outcome")
        completion = value.get("completion")
        if not isinstance(completion, Mapping) or completion.get("verified") is not True:
            raise QueueError("done requires verified completion evidence")
        mechanism = completion.get("mechanism")
        evidence = completion.get("evidence")
        if mechanism not in COMPLETION_MECHANISMS:
            raise QueueError("verified completion mechanism is unsupported")
        if (not isinstance(evidence, list) or not evidence or
                any(not isinstance(item, str) or not item.strip() or len(item) > 2_000 for item in evidence)):
            raise QueueError("verified completion requires non-empty bounded evidence")
        value["completion"] = {
            "verified": True,
            "mechanism": mechanism,
            "evidence": [item.strip() for item in evidence],
        }
    repository = value.get("repository")
    if repository is not None and not isinstance(repository, Mapping):
        raise QueueError("outcome repository must be an object")
    _canonical_json(value)
    return value


def _contract_hash(task: "Task") -> str:
    value = task.to_dict()
    for field in ("priority", "size", "active"):
        value.pop(field)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def is_safe_task_id(value: Any) -> bool:
    """Return whether a task id is safe for DB identity and positional CLI use."""

    return isinstance(value, str) and TASK_ID_RE.fullmatch(value) is not None


def _require_task_id(value: Any) -> str:
    if not is_safe_task_id(value):
        raise QueueError("task id must be 1-128 safe id characters and must not start with a dash")
    return value


def require_task_size(value: Any) -> str:
    """Return a canonical task-size estimate or reject the supplied metadata."""

    if not isinstance(value, str) or value not in TASK_SIZES:
        raise QueueError(f"task size must be one of: {', '.join(TASK_SIZES)}")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_epoch(value: str) -> float:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def hour_round(epoch: int) -> int:
    return ((int(epoch) + 1800) // 3600) * 3600


def cycle_from_key(eligibility_key: str | None, fallback: int = 0) -> int:
    if eligibility_key:
        tail = eligibility_key.rsplit("/", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return int(fallback)


def _json_tuple(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [part.strip() for part in value.split(",") if part.strip()]
    else:
        parsed = value
    if not isinstance(parsed, (list, tuple)):
        raise QueueError("provider/capability routing must be an array")
    return tuple(str(item) for item in parsed)


def canonical_model(model: str | None) -> str | None:
    """Persist and launch Fable pins as claude-fable-5-1."""

    if not isinstance(model, str):
        return None
    stripped = model.strip() or None
    if stripped in _FABLE_MODEL_ALIASES:
        return CANONICAL_FABLE_MODEL
    return stripped


def legacy_exclusive_model(model: str | None) -> bool:
    """Compatibility classification; the generic planner never calls this helper."""

    pinned = canonical_model(model)
    if not pinned:
        return False
    return (
        pinned.startswith("claude-")
        or pinned in {"opus", "sonnet", "haiku", "fable"}
        or "[1m]" in pinned
    )


def task_requires_legacy_exclusive(task: Task) -> bool:
    """Return whether a migrated compatibility row needs the reserved capability."""

    return task.claude_only or legacy_exclusive_model(task.model)


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    kind: str
    priority: int
    cadence: str | None
    cwd: str
    goal: str
    context: str | None
    constraints: str | None
    precondition: str | None
    done_when: str | None
    created_at: str
    active: bool
    claude_only: bool = False
    model: str | None = None
    mcp: str | None = None
    use_implement: bool = False
    allowed_providers: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    size: str | None = None
    source_ref: str | None = None
    work_group: str | None = None
    depends_on: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["active"] = int(self.active)
        value["claude_only"] = int(self.claude_only)
        value["use_implement"] = int(self.use_implement)
        value["allowed_providers"] = list(self.allowed_providers)
        value["required_capabilities"] = list(self.required_capabilities)
        return value

    def legacy_pick_dict(self) -> dict[str, Any]:
        """Historical ``bonusdb.sh pick`` row shape plus optional task metadata."""

        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "priority": self.priority,
            "cadence": self.cadence,
            "cwd": self.cwd,
            "goal": self.goal,
            "context": self.context or "",
            "constraints": self.constraints or "",
            "precondition": self.precondition or "",
            "done_when": self.done_when or "",
            "claude_only": int(self.claude_only),
            "model": self.model,
            "mcp": self.mcp,
            "use_implement": int(self.use_implement),
            "size": self.size,
            "source_ref": self.source_ref,
            "work_group": self.work_group,
            "depends_on": list(self.depends_on),
        }

    def legacy_contract_dict(self) -> dict[str, Any]:
        """Historical canonical-registration lookup shape plus optional task metadata."""

        value = self.legacy_pick_dict()
        value["active"] = int(self.active)
        return value


@dataclass(frozen=True)
class RunEvent:
    rowid_pk: int
    task: str
    kind: str
    cycle: int
    eligibility_key: str | None
    status: str
    ts: str
    branch: str | None
    summary: str | None
    provider_id: str | None
    account_id: str | None
    router_job_id: str | None
    engine: str | None = None
    trigger: str | None = None
    received_at: str | None = None
    attempt_id: str | None = None
    outcome: dict[str, Any] | None = None
    requeue: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Claim:
    task_id: str
    eligibility_key: str
    provider_id: str
    account_id: str | None
    state: str
    claimed_at: str
    detail: str | None
    attempt_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ActivationLease:
    task_id: str
    eligibility_key: str
    provider_id: str
    account_id: str
    state: str
    acquired_at: str
    attempt_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DispatchAttempt:
    id: str
    task_id: str
    ordinal: int
    eligibility_key: str | None
    mode: str
    origin: str
    state: str
    recovery_of: str | None
    recovery_of_legacy_run_rowid: int | None
    contract_hash: str
    reason_code: str | None
    reason_signature: str | None
    outcome: dict[str, Any] | None
    created_at: str
    terminal_at: str | None

    @property
    def attempt_id(self) -> str:
        return self.id

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["attempt_id"] = self.id
        return value


@dataclass(frozen=True)
class RecoveryDecision:
    task_id: str
    after_attempt_id: str | None
    after_legacy_run_rowid: int | None
    mode: str
    origin: str
    state: str
    consumed_by_attempt_id: str | None
    contract_hash: str
    not_before: str | None
    reason_code: str
    reason_signature: str | None
    detail: str | None
    updated_at: str
    blocked_descendants: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _DependencySnapshot:
    task_id: str
    contract_hash: str
    evidence: tuple[tuple[str, int, str | None, str | None], ...]
    base: dict[str, Any] | None


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    reconciliation_required: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()
    provider_holds: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reconciliation_required": list(self.reconciliation_required),
            "diagnostics": list(self.diagnostics),
            "provider_holds": list(self.provider_holds),
        }


class QueueDB:
    """Short-lived-connection SQLite access layer with atomic claim transitions."""

    def __init__(
        self,
        path: str | os.PathLike[str] | Path,
        *,
        timeout_seconds: float = 5.0,
        recurrence_timezone: str = "America/New_York",
    ):
        self.path = Path(path).expanduser().resolve(strict=False)
        self.timeout_seconds = timeout_seconds
        self.recurrence_timezone = ZoneInfo(recurrence_timezone)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schema.sql"
        try:
            schema = schema_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise QueueError(f"cannot read queue schema: {schema_path}") from exc
        with self._connect() as connection:
            connection.executescript(schema)
            self._additive_migrations(connection)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                (utc_now(),),
            )
            self._relax_status_checks(connection)

    @staticmethod
    def _relax_status_checks(connection: sqlite3.Connection) -> None:
        """Admit awaiting_human into the CHECK constraints of databases created before it existed.

        SQLite cannot ALTER a CHECK, so this follows the documented writable_schema procedure:
        loosening a CHECK IN-list changes no on-disk format, only the stored table SQL.
        """
        changed = False
        for table, column in (("runs", "status"), ("task_attempts", "state")):
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone()
            if row is None or "'awaiting_human'" in row[0]:
                continue
            match = re.search(rf"CHECK \({column} IN \([^)]*'failed'[^)]*\)\)", row[0])
            if match is None:
                # Tables from before the status CHECK existed already admit awaiting_human.
                continue
            relaxed = match.group(0).replace("'failed'", "'failed','awaiting_human'", 1)
            sql = row[0][:match.start()] + relaxed + row[0][match.end():]
            version = connection.execute("PRAGMA schema_version").fetchone()[0]
            connection.execute("PRAGMA writable_schema=ON")
            try:
                connection.execute(
                    "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?", (sql, table),
                )
                connection.execute(f"PRAGMA schema_version={int(version) + 1}")
            finally:
                connection.execute("PRAGMA writable_schema=OFF")
            changed = True
        if not changed:
            return
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise QueueError(f"status CHECK migration failed integrity_check: {result}")
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)",
            (utc_now(),),
        )

    def record_usage(self, snapshots) -> int:
        """Append one row per (account, limit) reading so the weekly curve is recoverable.

        The `*.usage.json` snapshots are overwritten on every refresh, so without this the only
        observable is the latest point and no review can show a trajectory. Stale (`fresh=False`)
        readings are skipped rather than written twice: a repeated cache read is not a new
        measurement, and PRIMARY KEY(ts, ...) makes a same-second re-run idempotent.
        """
        rows = []
        for snapshot in snapshots:
            if not getattr(snapshot, "fresh", True):
                continue
            stamp = snapshot.captured_at
            for limit_id, reading in (snapshot.limits or {}).items():
                rows.append((
                    utc_now(), snapshot.provider_id, snapshot.account_id, limit_id,
                    reading.get("used_percent"), reading.get("resets_at"),
                ))
        if not rows:
            return 0
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO usage_history"
                "(ts, provider_id, account_id, limit_id, used_percent, resets_at) "
                "VALUES(?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    @staticmethod
    def _additive_migrations(connection: sqlite3.Connection) -> None:
        task_columns = {
            "claude_only": "INTEGER NOT NULL DEFAULT 0",
            "model": "TEXT",
            "mcp": "TEXT",
            "use_implement": "INTEGER NOT NULL DEFAULT 0",
            "allowed_providers_json": "TEXT",
            "required_capabilities_json": "TEXT",
            "size": "TEXT",
            "source_ref": "TEXT",
            "work_group": "TEXT",
            "depends_on_json": "TEXT",
        }
        run_columns = {
            "engine": "TEXT",
            "trigger": "TEXT",
            "router_job_id": "TEXT",
            "eligibility_key": "TEXT",
            "provider_id": "TEXT",
            "account_id": "TEXT",
            "attempt_id": "TEXT",
            "outcome_json": "TEXT",
            "received_at": "TEXT",
        }
        for table, columns in (("tasks", task_columns), ("runs", run_columns)):
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        ownership_columns = {
            "dispatch_claims": {"attempt_id": "TEXT"},
            "activation_leases": {"attempt_id": "TEXT"},
        }
        for table, columns in ownership_columns.items():
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone():
                continue
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS dispatch_claims (
              task_id TEXT NOT NULL,
              eligibility_key TEXT NOT NULL,
              provider_id TEXT NOT NULL,
              account_id TEXT,
              state TEXT NOT NULL CHECK (state IN ('claimed','ambiguous')),
              claimed_at TEXT NOT NULL,
              detail TEXT,
              attempt_id TEXT,
              PRIMARY KEY(task_id, eligibility_key),
              FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS activation_leases (
              task_id TEXT NOT NULL,
              eligibility_key TEXT NOT NULL,
              provider_id TEXT NOT NULL,
              account_id TEXT NOT NULL,
              state TEXT NOT NULL CHECK (state IN ('activating','active','releasing')),
              acquired_at TEXT NOT NULL,
              attempt_id TEXT,
              PRIMARY KEY(task_id, eligibility_key),
              FOREIGN KEY(task_id, eligibility_key)
                REFERENCES dispatch_claims(task_id, eligibility_key) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage_history (
              ts           TEXT    NOT NULL,
              provider_id  TEXT    NOT NULL,
              account_id   TEXT    NOT NULL,
              limit_id     TEXT    NOT NULL,
              used_percent REAL,
              resets_at    INTEGER,
              PRIMARY KEY(ts, provider_id, account_id, limit_id)
            );
            CREATE INDEX IF NOT EXISTS idx_usage_history_account
              ON usage_history(account_id, limit_id, ts);
            CREATE INDEX IF NOT EXISTS idx_runs_eligibility ON runs(task, eligibility_key);
            CREATE INDEX IF NOT EXISTS idx_claims_task ON dispatch_claims(task_id);
            CREATE INDEX IF NOT EXISTS idx_activation_provider_account
              ON activation_leases(provider_id, account_id);
            CREATE TABLE IF NOT EXISTS task_attempts (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL REFERENCES tasks(id),
              ordinal INTEGER NOT NULL,
              eligibility_key TEXT,
              mode TEXT NOT NULL CHECK (mode IN ('normal','retry','verification')),
              origin TEXT NOT NULL CHECK (origin IN ('normal','automatic','operator','continuation')),
              state TEXT NOT NULL CHECK (state IN ('claimed','dispatched','done','skipped','failed','awaiting_human','ambiguous','aborted')),
              recovery_of TEXT REFERENCES task_attempts(id),
              recovery_of_legacy_run_rowid INTEGER REFERENCES runs(rowid_pk),
              contract_hash TEXT NOT NULL,
              reason_code TEXT,
              reason_signature TEXT,
              outcome_json TEXT,
              created_at TEXT NOT NULL,
              terminal_at TEXT,
              UNIQUE(task_id, ordinal),
              CHECK ((mode='normal' AND recovery_of IS NULL AND recovery_of_legacy_run_rowid IS NULL)
                  OR (mode!='normal' AND ((recovery_of IS NULL) != (recovery_of_legacy_run_rowid IS NULL))))
            );
            CREATE TABLE IF NOT EXISTS task_recovery (
              task_id TEXT PRIMARY KEY REFERENCES tasks(id),
              after_attempt_id TEXT REFERENCES task_attempts(id),
              after_legacy_run_rowid INTEGER REFERENCES runs(rowid_pk),
              mode TEXT NOT NULL CHECK (mode IN ('retry','verification')),
              origin TEXT NOT NULL CHECK (origin IN ('automatic','operator')),
              state TEXT NOT NULL CHECK (state IN ('scheduled','backoff','consumed','held','exhausted')),
              consumed_by_attempt_id TEXT REFERENCES task_attempts(id),
              contract_hash TEXT NOT NULL,
              not_before TEXT,
              reason_code TEXT NOT NULL,
              reason_signature TEXT,
              detail TEXT,
              updated_at TEXT NOT NULL,
              CHECK ((after_attempt_id IS NULL) != (after_legacy_run_rowid IS NULL)),
              CHECK ((state='consumed') = (consumed_by_attempt_id IS NOT NULL))
            );
            CREATE INDEX IF NOT EXISTS idx_attempts_task_ordinal
              ON task_attempts(task_id, ordinal);
            CREATE INDEX IF NOT EXISTS idx_runs_task_attempt ON runs(task, attempt_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_attempt_unique
              ON dispatch_claims(attempt_id) WHERE attempt_id IS NOT NULL;
            """
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"], title=row["title"], kind=row["kind"], priority=int(row["priority"]),
            cadence=row["cadence"], cwd=row["cwd"], goal=row["goal"], context=row["context"],
            constraints=row["constraints"], precondition=row["precondition"], done_when=row["done_when"],
            created_at=row["created_at"], active=bool(row["active"]), claude_only=bool(row["claude_only"]),
            model=row["model"], mcp=row["mcp"], use_implement=bool(row["use_implement"]),
            allowed_providers=_json_tuple(row["allowed_providers_json"]),
            required_capabilities=_json_tuple(row["required_capabilities_json"]),
            size=row["size"],
            source_ref=row["source_ref"], work_group=row["work_group"],
            depends_on=_json_tuple(row["depends_on_json"]),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunEvent:
        provider_id = row["provider_id"] or row["engine"]
        outcome = json.loads(row["outcome_json"]) if row["outcome_json"] else None
        return RunEvent(
            rowid_pk=int(row["rowid_pk"]), task=row["task"], kind=row["kind"], cycle=int(row["cycle"]),
            eligibility_key=row["eligibility_key"], status=row["status"], ts=row["ts"],
            received_at=row["received_at"], branch=row["branch"], summary=row["summary"], provider_id=provider_id,
            account_id=row["account_id"], router_job_id=row["router_job_id"], engine=row["engine"],
            trigger=row["trigger"], attempt_id=row["attempt_id"], outcome=outcome,
        )

    @staticmethod
    def _claim_from_row(row: sqlite3.Row) -> Claim:
        return Claim(**dict(row))

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> DispatchAttempt:
        return DispatchAttempt(
            id=row["id"], task_id=row["task_id"], ordinal=int(row["ordinal"]),
            eligibility_key=row["eligibility_key"], mode=row["mode"], origin=row["origin"],
            state=row["state"], recovery_of=row["recovery_of"],
            recovery_of_legacy_run_rowid=row["recovery_of_legacy_run_rowid"],
            contract_hash=row["contract_hash"], reason_code=row["reason_code"],
            reason_signature=row["reason_signature"],
            outcome=json.loads(row["outcome_json"]) if row["outcome_json"] else None,
            created_at=row["created_at"], terminal_at=row["terminal_at"],
        )

    @staticmethod
    def _recovery_from_row(row: sqlite3.Row, *, blocked_descendants: int = 0) -> RecoveryDecision:
        return RecoveryDecision(
            task_id=row["task_id"], after_attempt_id=row["after_attempt_id"],
            after_legacy_run_rowid=row["after_legacy_run_rowid"], mode=row["mode"],
            origin=row["origin"], state=row["state"],
            consumed_by_attempt_id=row["consumed_by_attempt_id"],
            contract_hash=row["contract_hash"], not_before=row["not_before"],
            reason_code=row["reason_code"], reason_signature=row["reason_signature"],
            detail=row["detail"], updated_at=row["updated_at"],
            blocked_descendants=blocked_descendants,
        )

    def add_task(self, values: Mapping[str, Any]) -> Task:
        self.initialize()
        try:
            with self._transaction() as connection:
                return self._insert_task(connection, values)
        except sqlite3.IntegrityError as exc:
            raise QueueError(f"task insert rejected for {values.get('id')}: {exc}") from exc

    def _insert_task(self, connection: sqlite3.Connection, values: Mapping[str, Any]) -> Task:
        """Validate and insert within the caller's transaction (including goal decisions)."""
        item_id = str(values.get("id") or "")
        _require_task_id(item_id)
        kind = str(values.get("kind", "oneoff"))
        if kind not in {"oneoff", "recurring"}:
            raise QueueError("task kind must be oneoff or recurring")
        try:
            priority = int(values.get("priority", 2))
        except (TypeError, ValueError) as exc:
            raise QueueError("task priority must be from 0 through 4") from exc
        if priority not in range(5):
            raise QueueError("task priority must be from 0 through 4")
        cadence = values.get("cadence")
        if kind == "recurring":
            cadence = cadence or "weekly"
            if cadence not in {"weekly", "monthly"}:
                raise QueueError("recurring cadence must be weekly or monthly")
        elif cadence not in (None, ""):
            raise QueueError("oneoff tasks cannot have a cadence")
        allowed = _json_tuple(values.get("allowed_providers") or values.get("provider_ids"))
        required = _json_tuple(values.get("required_capabilities"))
        raw_size = values.get("size")
        size = None if raw_size is None else require_task_size(raw_size)
        parameters = {
            "id": item_id, "title": str(values.get("title") or item_id), "kind": kind,
            "priority": priority, "cadence": cadence or None, "cwd": str(values.get("cwd") or os.getcwd()),
            "goal": str(values.get("goal") or ""), "context": values.get("context"),
            "constraints": values.get("constraints"), "precondition": values.get("precondition"),
            "done_when": values.get("done_when"), "created_at": str(values.get("created_at") or utc_now()),
            "active": int(bool(values.get("active", True))), "claude_only": int(bool(values.get("claude_only", False))),
            "model": canonical_model(values.get("model")), "mcp": values.get("mcp"),
            "use_implement": int(bool(values.get("use_implement", False))),
            "allowed": json.dumps(list(allowed)) if allowed else None,
            "required": json.dumps(list(required)) if required else None,
            "size": size,
        }
        parameters.update(self._work_fields(values))
        self._validate_dependencies(connection, item_id, parameters["depends_on_json"])
        connection.execute(
            """
            INSERT INTO tasks(
              id,title,kind,priority,cadence,cwd,goal,context,constraints,
              precondition,done_when,created_at,active,claude_only,model,mcp,
              use_implement,allowed_providers_json,required_capabilities_json,size,
              source_ref,work_group,depends_on_json
            ) VALUES(
              :id,:title,:kind,:priority,:cadence,:cwd,:goal,:context,:constraints,
              :precondition,:done_when,:created_at,:active,:claude_only,:model,:mcp,
              :use_implement,:allowed,:required,:size,
              :source_ref,:work_group,:depends_on_json
            )
            """, parameters,
        )
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (item_id,)).fetchone()
        return self._task_from_row(row)

    @staticmethod
    def _work_fields(values: Mapping[str, Any], *, validate_work_group: bool = True) -> dict[str, Any]:
        dependencies = values.get("depends_on", ())
        if not isinstance(dependencies, (list, tuple)) or any(not isinstance(x, str) for x in dependencies):
            raise QueueError("depends_on must be a list of task IDs")
        for dependency in dependencies:
            _require_task_id(dependency)
        for field in ("source_ref", "work_group"):
            if values.get(field) is not None and not isinstance(values[field], str):
                raise QueueError(f"{field} must be text")
        work_group = values.get("work_group")
        if isinstance(work_group, str) and work_group.lower().replace("-", " ") == "soak obs":
            work_group = "Soak Obs"
        if validate_work_group and work_group is not None and len(work_group) > WORK_GROUP_MAX_LENGTH:
            raise QueueError(f"work_group must be at most {WORK_GROUP_MAX_LENGTH} characters")
        return {"source_ref": values.get("source_ref"), "work_group": work_group,
                "depends_on_json": json.dumps(sorted(set(dependencies)))}

    @staticmethod
    def _validate_dependencies(connection: sqlite3.Connection, task_id: str, raw: str) -> None:
        dependencies = json.loads(raw)
        graph = {row["id"]: row for row in connection.execute(
            "SELECT id,kind,depends_on_json FROM tasks"
        )}
        for dependency in dependencies:
            if dependency == task_id:
                raise QueueError("a task cannot depend on itself")
            if dependency not in graph:
                raise QueueError(f"unknown prerequisite: {dependency}")
            if graph[dependency]["kind"] != "oneoff":
                raise QueueError("prerequisites must be one-off tasks in this version")
        pending, seen = list(dependencies), set()
        while pending:
            node = pending.pop()
            if node == task_id:
                raise QueueError("dependency cycle rejected")
            if node in seen:
                continue
            seen.add(node)
            if node in graph:
                pending.extend(_json_tuple(graph[node]["depends_on_json"]))

    @staticmethod
    def _verified_done_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
        """Return the newest dependency-satisfying run, including historical NULL done."""

        return connection.execute(
            """SELECT r.* FROM runs r
                 LEFT JOIN task_attempts a ON a.id=r.attempt_id AND a.task_id=r.task
                 WHERE r.task=? AND r.status='done'
                   AND r.kind='oneoff'
                   AND (r.attempt_id IS NULL OR a.state='done')
                 ORDER BY r.rowid_pk DESC LIMIT 1""",
            (task_id,),
        ).fetchone()

    @staticmethod
    def _latest_effective_attempt(
        connection: sqlite3.Connection, task_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT * FROM task_attempts
                 WHERE task_id=? AND state!='aborted'
                 ORDER BY ordinal DESC LIMIT 1""",
            (task_id,),
        ).fetchone()

    @staticmethod
    def _latest_legacy_run(
        connection: sqlite3.Connection, task_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT r.* FROM runs r JOIN tasks t ON t.id=r.task
                 WHERE r.task=? AND r.attempt_id IS NULL AND r.kind=t.kind
                 ORDER BY r.rowid_pk DESC LIMIT 1""",
            (task_id,),
        ).fetchone()

    @staticmethod
    def _dependency_statuses(connection: sqlite3.Connection, task: Task) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for dependency in task.depends_on:
            parent = connection.execute(
                "SELECT title,kind FROM tasks WHERE id=?", (dependency,),
            ).fetchone()
            claim = connection.execute(
                "SELECT state,attempt_id FROM dispatch_claims WHERE task_id=?", (dependency,),
            ).fetchone()
            done = QueueDB._verified_done_row(connection, dependency)
            latest = QueueDB._latest_effective_attempt(connection, dependency)
            legacy = QueueDB._latest_legacy_run(connection, dependency)
            recovery = connection.execute(
                "SELECT * FROM task_recovery WHERE task_id=?", (dependency,),
            ).fetchone()
            if parent is None:
                status = "missing"
            elif parent["kind"] != "oneoff":
                status = "unsupported recurrence"
            elif done is not None:
                status = "done"
            elif claim is not None:
                status = "ambiguous" if claim["state"] == "ambiguous" else "running"
            elif recovery is not None and recovery["state"] in {"scheduled", "backoff", "consumed"}:
                status = "recovering"
            elif recovery is not None:
                status = recovery["state"]
            elif latest is not None:
                status = latest["state"]
            elif legacy is not None:
                status = legacy["status"]
            else:
                status = "queued"
            item: dict[str, Any] = {
                "id": dependency,
                "title": parent["title"] if parent else dependency,
                "status": status,
                "satisfied": done is not None,
                "attempt_id": done["attempt_id"] if done is not None else (
                    latest["id"] if latest is not None else None
                ),
                "verified_completion": bool(done is not None),
            }
            if recovery is not None:
                item["recovery"] = QueueDB._recovery_from_row(recovery).to_dict()
            result.append(item)
        return result

    @staticmethod
    def _dependency_outcomes(
        connection: sqlite3.Connection, task: Task,
    ) -> list[tuple[str, dict[str, Any]]]:
        result: list[tuple[str, dict[str, Any]]] = []
        for dependency in task.depends_on:
            done = QueueDB._verified_done_row(connection, dependency)
            if done is None or not done["outcome_json"]:
                continue
            outcome = json.loads(done["outcome_json"])
            if isinstance(outcome, dict) and isinstance(outcome.get("repository"), dict):
                result.append((dependency, outcome))
        return result

    @staticmethod
    def _dependency_evidence(
        connection: sqlite3.Connection, task: Task,
    ) -> tuple[tuple[str, int, str | None, str | None], ...]:
        evidence: list[tuple[str, int, str | None, str | None]] = []
        for dependency in task.depends_on:
            parent = connection.execute(
                "SELECT kind FROM tasks WHERE id=?", (dependency,),
            ).fetchone()
            done = QueueDB._verified_done_row(connection, dependency)
            if parent is None or parent["kind"] != "oneoff" or done is None:
                raise QueueError(
                    "dependencies_unsatisfied: prerequisite completion is not verified"
                )
            evidence.append((
                dependency,
                int(done["rowid_pk"]),
                done["attempt_id"],
                done["outcome_json"],
            ))
        return tuple(evidence)

    @staticmethod
    def _resolve_dependency_outcomes(
        task: Task, outcomes: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, Any] | None:
        if not outcomes:
            return None
        try:
            from .handoff import DependencyHandoffError, resolve_dependency_base
        except ImportError as exc:
            raise QueueError(
                "dependency_ref_unavailable: dependency handoff validator is unavailable"
            ) from exc
        try:
            return resolve_dependency_base(task.cwd, outcomes)
        except DependencyHandoffError as exc:
            reason_code = getattr(exc, "reason_code", "dependency_ref_unavailable")
            detail = getattr(exc, "detail", str(exc))
            raise QueueError(f"{reason_code}: {detail}") from exc

    @staticmethod
    def _resolve_dependency_base(
        connection: sqlite3.Connection, task: Task,
    ) -> dict[str, Any] | None:
        QueueDB._dependency_evidence(connection, task)
        outcomes = QueueDB._dependency_outcomes(connection, task)
        return QueueDB._resolve_dependency_outcomes(task, outcomes)

    def _dependency_preflight(self, task_id: str) -> _DependencySnapshot:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,),
            ).fetchone()
            if row is None:
                raise QueueError(f"unknown task: {task_id}")
            task = self._task_from_row(row)
            evidence = self._dependency_evidence(connection, task)
            outcomes = self._dependency_outcomes(connection, task)
        base = self._resolve_dependency_outcomes(task, outcomes)
        return _DependencySnapshot(task.id, _contract_hash(task), evidence, base)

    @staticmethod
    def _dependency_snapshot_matches(
        connection: sqlite3.Connection,
        task: Task,
        snapshot: _DependencySnapshot,
    ) -> bool:
        return (
            task.id == snapshot.task_id
            and _contract_hash(task) == snapshot.contract_hash
            and QueueDB._dependency_evidence(connection, task) == snapshot.evidence
        )

    def dependency_base(self, task_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise QueueError(f"unknown task: {task_id}")
            task = self._task_from_row(row)
            dependencies = self._dependency_statuses(connection, task)
            if any(not item["satisfied"] for item in dependencies):
                raise QueueError("dependencies_unsatisfied: prerequisite completion is not verified")
            return self._resolve_dependency_base(connection, task)

    @staticmethod
    def _blocked_descendant_counts(connection: sqlite3.Connection) -> dict[str, int]:
        graph = {
            row["id"]: tuple(_json_tuple(row["depends_on_json"]))
            for row in connection.execute(
                "SELECT id,depends_on_json FROM tasks WHERE active=1"
            )
        }
        completed = {
            row["task"] for row in connection.execute(
                """SELECT DISTINCT r.task FROM runs r
                     JOIN tasks t ON t.id=r.task
                     LEFT JOIN task_attempts a
                       ON a.id=r.attempt_id AND a.task_id=r.task
                     WHERE r.status='done' AND r.kind='oneoff' AND r.kind=t.kind
                       AND (r.attempt_id IS NULL OR a.state='done')"""
            )
        }
        running = {
            row["task_id"] for row in connection.execute(
                "SELECT DISTINCT task_id FROM dispatch_claims"
            )
        }
        counts = {task_id: 0 for task_id in graph}
        for child, parents in graph.items():
            if child in completed or child in running:
                continue
            pending = [parent for parent in parents if parent not in completed]
            if not pending:
                continue
            seen: set[str] = set()
            while pending:
                parent = pending.pop()
                if parent in seen or parent in completed:
                    continue
                seen.add(parent)
                if parent in counts:
                    counts[parent] += 1
                if parent not in running:
                    pending.extend(graph.get(parent, ()))
        return counts

    def readiness(
        self, task_id: str, *, now_epoch: int | float | None = None,
    ) -> dict[str, Any]:
        from .goals import recovery_admission, task_admitted
        now = float(time.time() if now_epoch is None else now_epoch)
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise QueueError(f"unknown task: {task_id}")
            task = self._task_from_row(row)
            dependencies = self._dependency_statuses(connection, task)
            waiting = [item for item in dependencies if not item["satisfied"]]
            claim = connection.execute(
                "SELECT * FROM dispatch_claims WHERE task_id=?", (task_id,),
            ).fetchone()
            recovery = connection.execute(
                "SELECT * FROM task_recovery WHERE task_id=?", (task_id,),
            ).fetchone()
            done = self._verified_done_row(connection, task_id)
            latest = self._latest_effective_attempt(connection, task_id)
            legacy = self._latest_legacy_run(connection, task_id)
            counts = (
                self._blocked_descendant_counts(connection)
                if recovery is not None else {}
            )
            consumed_live = False
            if recovery is not None and recovery["state"] == "consumed":
                consumer = connection.execute(
                    "SELECT state FROM task_attempts WHERE id=? AND task_id=?",
                    (recovery["consumed_by_attempt_id"], task_id),
                ).fetchone()
                owner = connection.execute(
                    "SELECT 1 FROM dispatch_claims WHERE task_id=? AND attempt_id=?",
                    (task_id, recovery["consumed_by_attempt_id"]),
                ).fetchone()
                consumed_live = bool(
                    consumer is not None
                    and consumer["state"] in {"claimed", "dispatched", "ambiguous"}
                    and owner is not None
                )
            source_reason_code = (
                latest["reason_code"]
                if latest is not None else (
                    self._source_reason(None, legacy)[0]
                    if legacy is not None else None
                )
            )
            recovery_value = (
                self._recovery_from_row(
                    recovery, blocked_descendants=counts.get(task_id, 0),
                ).to_dict()
                if recovery is not None else None
            )
            goal_owned = connection.execute(
                """SELECT 1 FROM goal_turns WHERE task_id=?
                     UNION ALL
                     SELECT 1 FROM goal_members WHERE task_id=? AND managed=1
                     LIMIT 1""",
                (task_id, task_id),
            ).fetchone() is not None
            admitted = True
            state, reason, hold_reason = "ready", "Ready to run", None
            dependency_base: dict[str, Any] | None = None
            if not task.active:
                state, reason = "paused", "Paused"
            elif claim is not None:
                if claim["state"] == "ambiguous":
                    state, reason, hold_reason = (
                        "held", "Launch ownership is ambiguous", "unknown_launch",
                    )
                else:
                    state, reason = "running", "Run is active or requires reconciliation"
            elif done is not None and task.kind == "oneoff":
                state, reason = "done", "Verified completion is retained"
            elif waiting:
                state, reason = "waiting", "Waiting for " + ", ".join(
                    item["id"] for item in waiting
                )
            else:
                try:
                    dependency_base = self._resolve_dependency_base(connection, task)
                except QueueError as exc:
                    hold_reason = str(exc).split(":", 1)[0]
                    state, reason = "held", str(exc)
                admitted, admission_reason = recovery_admission(
                    connection, task_id, now_epoch=now,
                )
                if state == "ready" and not task_admitted(connection, task_id, now_epoch=now):
                    state, reason, hold_reason = "waiting", admission_reason, admission_reason
                elif state == "ready" and recovery is not None and recovery["state"] in {"held", "exhausted"}:
                    state, reason, hold_reason = recovery["state"], (
                        recovery["detail"] or recovery["reason_code"]
                    ), recovery["reason_code"]
                elif (
                    state == "ready" and recovery is not None
                    and recovery["state"] == "consumed" and consumed_live
                ):
                    state, reason = "running", "Recovery attempt is active"
                elif state == "ready" and recovery is not None and recovery["state"] in {"scheduled", "backoff"}:
                    due = recovery["not_before"] is None or _timestamp_epoch(recovery["not_before"]) <= now
                    if due:
                        state, reason = "ready", "Recovery is ready to run"
                    else:
                        state, reason = "recovering", "Recovery backoff has not elapsed"
                elif state == "ready" and task.kind == "oneoff" and (latest is not None or legacy is not None):
                    source_state = latest["state"] if latest is not None else legacy["status"]
                    state, reason = source_state, f"Last run: {source_state}"
                    if source_state == "awaiting_human":
                        source_outcome = (
                            json.loads(latest["outcome_json"])
                            if latest is not None and latest["outcome_json"] else {}
                        )
                        detail = (source_outcome.get("reason") or {}).get("detail")
                        if detail:
                            reason = f"Awaiting Brian: {detail}"
                elif state == "ready" and not self._eligible_in_connection(
                    connection, task, 0, now_epoch=now, automatic=False,
                ):
                    state, reason = "cooldown", "Waiting for recurrence cooldown"
            requeue_allowed = bool(
                task.kind == "oneoff" and claim is None and done is None
                and not goal_owned
                and (
                    (latest is not None and latest["state"] in {"failed", "skipped"})
                    or (
                        latest is None and legacy is not None
                        and legacy["status"] in {"failed", "skipped"}
                    )
                )
                and (
                    recovery is None
                    or recovery["state"] in {"scheduled", "backoff"}
                    or (recovery["state"] == "consumed" and not consumed_live)
                    or (
                        recovery["state"] == "exhausted"
                        and recovery["origin"] == "automatic"
                    )
                )
                and source_reason_code not in {
                    "authority_required", "permanent", "unknown_launch",
                }
            )
            if not admitted:
                requeue_allowed = False
            requeue_reason = (
                "Operator retry is available" if requeue_allowed else
                (reason if state in {"held", "exhausted"} else "Task cannot be safely requeued")
            )
            return {
                "state": state,
                "ready": state == "ready",
                "reason": reason,
                "hold_reason": hold_reason,
                "dependencies": dependencies,
                "attempt": (
                    self._attempt_from_row(latest).to_dict()
                    if latest is not None else None
                ),
                "recovery": recovery_value,
                "dependency_base": dependency_base,
                "requeue": {"allowed": requeue_allowed, "reason": requeue_reason},
            }

    def edit_task(self, task_id: str, changes: Mapping[str, Any]) -> Task:
        from .goals import guard_contract_edit
        allowed = {"title", "priority", "size", "cwd", "goal", "context", "constraints",
                   "precondition", "done_when", "source_ref", "work_group", "depends_on"}
        if not changes or set(changes) - allowed:
            raise QueueError("edit requires supported task contract fields")
        self.initialize()
        with self._transaction() as connection:
            guard_contract_edit(connection, task_id, set(changes))
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise QueueError(f"unknown task: {task_id}")
            task = self._task_from_row(row)
            last = connection.execute(
                "SELECT status FROM runs WHERE task=? ORDER BY rowid_pk DESC LIMIT 1", (task_id,),
            ).fetchone()
            if connection.execute(
                "SELECT 1 FROM dispatch_claims WHERE task_id=?", (task_id,),
            ).fetchone():
                raise QueueError("recovery already claimed; the task contract cannot be edited")
            recovery = None
            old_hash = _contract_hash(task)
            latest_attempt = self._latest_effective_attempt(connection, task_id)
            latest_legacy = self._latest_legacy_run(connection, task_id)
            if task.kind == "oneoff" and self._verified_done_row(connection, task_id) is not None:
                raise QueueError("completed work cannot be edited")
            if task.kind == "oneoff" and (latest_attempt is not None or latest_legacy is not None):
                recovery = connection.execute(
                    "SELECT * FROM task_recovery WHERE task_id=?", (task_id,),
                ).fetchone()
                if recovery is None:
                    raise QueueError("operator requeue required before editing retained failed work")
                if recovery["origin"] != "operator":
                    raise QueueError("automatic recovery cannot authorize a contract edit")
                if recovery["state"] == "consumed":
                    raise QueueError("recovery already claimed; the task contract cannot be edited")
                if recovery["state"] not in {"scheduled", "backoff"}:
                    raise QueueError("recovery is held; the task contract cannot be edited")
                expected_attempt = latest_attempt["id"] if latest_attempt is not None else None
                expected_legacy = latest_legacy["rowid_pk"] if latest_attempt is None and latest_legacy is not None else None
                if (recovery["after_attempt_id"] != expected_attempt or
                        recovery["after_legacy_run_rowid"] != expected_legacy or
                        recovery["contract_hash"] != old_hash):
                    raise QueueError("recovery contract changed; edit CAS refused")
            elif last and last[0] == "dispatched":
                raise QueueError("only queued tasks can be edited")
            merged = {**task.to_dict(), **changes}
            # Old imported groups may be longer.  Preserve them until their group is explicitly
            # revised; otherwise an unrelated queued-contract edit would be unexpectedly blocked.
            fields = self._work_fields(merged, validate_work_group="work_group" in changes)
            self._validate_dependencies(connection, task_id, fields["depends_on_json"])
            for key, value in changes.items():
                if key in {"source_ref", "work_group", "depends_on"}:
                    continue
                if key == "priority":
                    if type(value) is not int or value not in range(5):
                        raise QueueError("priority must be from 0 through 4")
                elif key == "size":
                    value = require_task_size(value)
                elif value is not None and not isinstance(value, str):
                    raise QueueError(f"{key} must be text")
                if key in {"title", "cwd", "goal"} and not (value or "").strip():
                    raise QueueError(f"{key} cannot be empty")
                fields[key] = value
            connection.execute("UPDATE tasks SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", (*fields.values(), task_id))
            if recovery is not None:
                changed = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                assert changed is not None
                new_hash = _contract_hash(self._task_from_row(changed))
                cursor = connection.execute(
                    """UPDATE task_recovery SET contract_hash=?,updated_at=?
                         WHERE task_id=? AND origin='operator' AND state IN ('scheduled','backoff')
                           AND contract_hash=? AND after_attempt_id IS ?
                           AND after_legacy_run_rowid IS ?""",
                    (
                        new_hash, utc_now(), task_id, old_hash,
                        recovery["after_attempt_id"], recovery["after_legacy_run_rowid"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise QueueError("recovery contract changed; edit CAS refused")
        return self.task(task_id)

    def task(self, task_id: str) -> Task | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    def tasks(self, *, active: bool | None = None) -> list[Task]:
        self.initialize()
        where = "" if active is None else " WHERE active=?"
        parameters: tuple[Any, ...] = () if active is None else (int(active),)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks{where} ORDER BY priority, (kind='recurring'), created_at, id", parameters,
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    @staticmethod
    def _provider_compatible(task: Task, provider_id: str | None, capabilities: Iterable[str] = ()) -> bool:
        capability_set = set(capabilities)
        if provider_id and task.allowed_providers and provider_id not in task.allowed_providers:
            return False
        if provider_id and task_requires_legacy_exclusive(task):
            if LEGACY_EXCLUSIVE_CAPABILITY not in capability_set:
                return False
        return set(task.required_capabilities).issubset(capability_set)

    def _weekly_window(self, now_epoch: float) -> tuple[float, float, float]:
        """Return Saturday week start and the Sunday-only automatic window."""

        local_now = datetime.fromtimestamp(now_epoch, self.recurrence_timezone)
        saturday = local_now.date() - timedelta(days=(local_now.weekday() - 5) % 7)
        week_start = datetime.combine(saturday, datetime.min.time(), self.recurrence_timezone)
        sunday_start = week_start + timedelta(days=1)
        sunday_end = sunday_start + timedelta(days=1)
        return week_start.timestamp(), sunday_start.timestamp(), sunday_end.timestamp()

    def _eligible_in_connection(
        self,
        connection: sqlite3.Connection,
        task: Task,
        cycle: int,
        *,
        now_epoch: float | None = None,
        automatic: bool = False,
    ) -> bool:
        if not task.active:
            return False
        from .goals import task_admitted
        now = time.time() if now_epoch is None else float(now_epoch)
        if not task_admitted(connection, task.id, now_epoch=now):
            return False
        if any(not d["satisfied"] for d in QueueDB._dependency_statuses(connection, task)):
            return False
        if connection.execute("SELECT 1 FROM dispatch_claims WHERE task_id=? LIMIT 1", (task.id,)).fetchone():
            return False
        if task.kind == "oneoff":
            if self._verified_done_row(connection, task.id) is not None:
                return False
            latest = self._latest_effective_attempt(connection, task.id)
            legacy = self._latest_legacy_run(connection, task.id)
            if latest is None and legacy is None:
                return True
            recovery = connection.execute(
                "SELECT * FROM task_recovery WHERE task_id=?", (task.id,),
            ).fetchone()
            if recovery is None or recovery["state"] not in {"scheduled", "backoff"}:
                return False
            if recovery["contract_hash"] != _contract_hash(task):
                return False
            if recovery["not_before"] is not None:
                try:
                    if _timestamp_epoch(recovery["not_before"]) > now:
                        return False
                except ValueError:
                    return False
            return True
        if cycle > 0 and connection.execute(
            "SELECT 1 FROM runs WHERE task=? AND cycle=? LIMIT 1", (task.id, int(cycle)),
        ).fetchone():
            # Keep provider-reset dedup in addition to calendar/cooldown recurrence.
            return False
        cadence = task.cadence or ""
        if cadence not in {"weekly", *RECURRING_COOLDOWNS_SECONDS}:
            raise QueueError(f"unsupported recurring cadence: {task.cadence}")
        row = connection.execute(
            "SELECT ts FROM runs WHERE task=? ORDER BY rowid_pk DESC LIMIT 1",
            (task.id,),
        ).fetchone()
        if cadence == "weekly":
            week_start, sunday_start, sunday_end = self._weekly_window(now)
            if automatic and not sunday_start <= now < sunday_end:
                return False
            if row is None:
                return True
            try:
                last_run_epoch = _timestamp_epoch(str(row["ts"]))
            except ValueError:
                return False
            # A future timestamp is also fail-closed. Requeue is the explicit retry path.
            return last_run_epoch < week_start
        if row is None:
            return True
        try:
            last_run_epoch = _timestamp_epoch(str(row["ts"]))
        except ValueError:
            # A malformed run timestamp must not make a recurring job eligible early.
            return False
        return now - last_run_epoch >= RECURRING_COOLDOWNS_SECONDS[cadence]

    def _eligible_since_in_connection(
        self,
        connection: sqlite3.Connection,
        task: Task,
        *,
        now_epoch: float | None = None,
        automatic: bool = False,
    ) -> float:
        """Return when an already-eligible task began waiting."""

        try:
            created_at = _timestamp_epoch(task.created_at)
        except ValueError:
            created_at = float("inf")
        if task.kind == "oneoff":
            recovery = connection.execute(
                "SELECT not_before,updated_at FROM task_recovery WHERE task_id=?", (task.id,),
            ).fetchone()
            if recovery is not None:
                stamp = recovery["not_before"] or recovery["updated_at"]
                try:
                    return max(created_at, _timestamp_epoch(stamp))
                except ValueError:
                    return float("inf")
            return created_at
        if task.cadence == "weekly":
            now = time.time() if now_epoch is None else float(now_epoch)
            week_start, sunday_start, _week_end = self._weekly_window(now)
            return max(created_at, sunday_start if automatic else week_start)
        row = connection.execute(
            "SELECT ts FROM runs WHERE task=? ORDER BY rowid_pk DESC LIMIT 1",
            (task.id,),
        ).fetchone()
        if row is None:
            return created_at
        cooldown = RECURRING_COOLDOWNS_SECONDS[task.cadence or ""]
        try:
            return _timestamp_epoch(str(row["ts"])) + cooldown
        except ValueError:
            return float("inf")

    def eligible_tasks(
        self, cycle: int, *, provider_id: str | None = None, capabilities: Iterable[str] = (),
        portable_only: bool = False, exclusive_only: bool = False, task_id: str | None = None,
        claude_priority: bool = False, limit: int | None = None, automatic: bool = False,
        now_epoch: float | None = None,
    ) -> list[Task]:
        now = float(time.time() if now_epoch is None else now_epoch)
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tasks WHERE active=1").fetchall()
            blocked_descendants = self._blocked_descendant_counts(connection)
            candidates: list[tuple[Task, float, int]] = []
            for row in rows:
                task = self._task_from_row(row)
                if task_id and task.id != task_id:
                    continue
                exclusive = task_requires_legacy_exclusive(task)
                if portable_only and exclusive:
                    continue
                if exclusive_only and not exclusive:
                    continue
                if not self._provider_compatible(task, provider_id, capabilities):
                    continue
                if self._eligible_in_connection(
                    connection, task, int(cycle), now_epoch=now, automatic=automatic,
                ):
                    recovery = connection.execute(
                        "SELECT 1 FROM task_recovery WHERE task_id=? AND state IN ('scheduled','backoff')",
                        (task.id,),
                    ).fetchone()
                    candidates.append((task, self._eligible_since_in_connection(
                        connection, task, now_epoch=now, automatic=automatic,
                    ), blocked_descendants.get(task.id, 0) if recovery else 0))
        if claude_priority:
            candidates.sort(key=lambda item: (
                not task_requires_legacy_exclusive(item[0]), item[0].priority,
                -item[2], item[1], item[0].kind == "recurring", item[0].created_at, item[0].id,
            ))
        else:
            candidates.sort(key=lambda item: (
                item[0].priority, -item[2], item[1], item[0].kind == "recurring",
                item[0].created_at, item[0].id,
            ))
        tasks = [task for task, _eligible_since, _descendants in candidates]
        return tasks if limit is None else tasks[: max(0, int(limit))]

    def count_eligible(self, cycle: int, **kwargs: Any) -> int:
        return len(self.eligible_tasks(cycle, **kwargs))

    def claim(
        self, task_id: str, eligibility_key: str, provider_id: str, account_id: str | None,
        *, provider_capabilities: Iterable[str] = (), automatic: bool = False, expected_task: Task | None = None,
        now_epoch: float | None = None,
    ) -> DispatchAttempt | None:
        if not eligibility_key or not provider_id or provider_id == "auto":
            return None
        now = float(time.time() if now_epoch is None else now_epoch)
        stamp = datetime.fromtimestamp(now, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self.initialize()
        try:
            dependency_snapshot = self._dependency_preflight(task_id)
        except QueueError:
            return None
        cycle = cycle_from_key(eligibility_key)
        try:
            with self._transaction() as connection:
                row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if row is None:
                    return None
                task = self._task_from_row(row)
                if expected_task is not None and task != expected_task:
                    return None
                if not self._provider_compatible(task, provider_id, provider_capabilities):
                    return None
                if not self._eligible_in_connection(
                    connection, task, cycle, now_epoch=now, automatic=automatic,
                ):
                    return None
                try:
                    dependency_matches = self._dependency_snapshot_matches(
                        connection, task, dependency_snapshot,
                    )
                except QueueError:
                    return None
                if not dependency_matches:
                    return None
                recovery = connection.execute(
                    "SELECT * FROM task_recovery WHERE task_id=?", (task_id,),
                ).fetchone()
                if recovery is None:
                    mode, origin = "normal", "normal"
                    recovery_of = None
                    recovery_of_legacy = None
                else:
                    if recovery["state"] not in {"scheduled", "backoff"}:
                        return None
                    if recovery["not_before"] is not None and _timestamp_epoch(recovery["not_before"]) > now:
                        return None
                    mode, origin = recovery["mode"], recovery["origin"]
                    recovery_of = recovery["after_attempt_id"]
                    recovery_of_legacy = recovery["after_legacy_run_rowid"]
                ordinal = int(connection.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 FROM task_attempts WHERE task_id=?",
                    (task_id,),
                ).fetchone()[0])
                attempt_id = uuid.uuid4().hex
                contract_hash = _contract_hash(task)
                connection.execute(
                    """INSERT INTO task_attempts(
                         id,task_id,ordinal,eligibility_key,mode,origin,state,recovery_of,
                         recovery_of_legacy_run_rowid,contract_hash,created_at
                       ) VALUES(?,?,?,?,?,?,'claimed',?,?,?,?)""",
                    (
                        attempt_id, task_id, ordinal, eligibility_key, mode, origin,
                        recovery_of, recovery_of_legacy, contract_hash, stamp,
                    ),
                )
                connection.execute(
                    """INSERT INTO dispatch_claims(
                         task_id,eligibility_key,provider_id,account_id,state,claimed_at,detail,attempt_id
                       ) VALUES(?,?,?,?, 'claimed', ?, NULL, ?)""",
                    (task_id, eligibility_key, provider_id, account_id, stamp, attempt_id),
                )
                if recovery is not None:
                    cursor = connection.execute(
                        """UPDATE task_recovery SET state='consumed',consumed_by_attempt_id=?,updated_at=?
                             WHERE task_id=? AND state IN ('scheduled','backoff')
                               AND contract_hash=?
                               AND after_attempt_id IS ? AND after_legacy_run_rowid IS ?""",
                        (
                            attempt_id, stamp, task_id, contract_hash,
                            recovery_of, recovery_of_legacy,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise QueueError("recovery claim lost its source or contract CAS")
                attempt = connection.execute(
                    "SELECT * FROM task_attempts WHERE id=?", (attempt_id,),
                ).fetchone()
                assert attempt is not None
                return self._attempt_from_row(attempt)
        except sqlite3.IntegrityError:
            return None

    def acquire_activation(
        self,
        task_id: str,
        eligibility_key: str,
        provider_id: str,
        account_id: str,
        activate: Callable[[], None],
        *,
        attempt_id: str | None = None,
    ) -> bool:
        """Acquire one durable claim-scoped activation lease with a two-phase transition."""

        self.initialize()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            wait_for_activation = False
            try:
                with self._transaction() as connection:
                    claim = connection.execute(
                        """SELECT provider_id,account_id,attempt_id FROM dispatch_claims
                             WHERE task_id=? AND eligibility_key=?""",
                        (task_id, eligibility_key),
                    ).fetchone()
                    if claim is None or claim["provider_id"] != provider_id or claim["account_id"] != account_id:
                        raise QueueError("activation lease requires the matching dispatch claim")
                    resolved_attempt_id = claim["attempt_id"]
                    if attempt_id is not None and resolved_attempt_id != attempt_id:
                        raise QueueError("activation lease attempt no longer owns the claim")
                    existing = connection.execute(
                        """SELECT account_id,state FROM activation_leases
                             WHERE provider_id=? ORDER BY acquired_at,task_id""",
                        (provider_id,),
                    ).fetchall()
                    if any(row["account_id"] != account_id for row in existing):
                        raise QueueError(
                            f"provider {provider_id} is leased to a different account"
                        )
                    if any(row["state"] != "active" for row in existing):
                        # A same-account peer may be completing the committed activating phase.
                        # Wait briefly for it rather than failing a concurrent compatible launch.
                        wait_for_activation = all(row["state"] == "activating" for row in existing)
                        if not wait_for_activation:
                            raise QueueError(f"provider {provider_id} activation is incomplete")
                    else:
                        state = "active" if existing else "activating"
                        connection.execute(
                            """INSERT INTO activation_leases(
                                 task_id,eligibility_key,provider_id,account_id,state,acquired_at,attempt_id
                               ) VALUES(?,?,?,?,?,?,?)""",
                            (
                                task_id, eligibility_key, provider_id, account_id, state,
                                utc_now(), resolved_attempt_id,
                            ),
                        )
                        needs_activation = not existing
            except sqlite3.IntegrityError as exc:
                raise QueueError("activation lease already exists") from exc
            if not wait_for_activation:
                break
            if time.monotonic() >= deadline:
                raise QueueError(f"provider {provider_id} activation is incomplete")
            time.sleep(0.02)
        if not needs_activation:
            return False

        # The incomplete state commits before the external effect. A crash, adapter failure, or
        # later DB failure therefore leaves durable evidence that blocks all provider dispatches
        # until an operator reconciles the account state.
        activate()
        with self._transaction() as connection:
            cursor = connection.execute(
                """UPDATE activation_leases SET state='active'
                     WHERE task_id=? AND eligibility_key=? AND state='activating'
                       AND attempt_id IS ?""",
                (task_id, eligibility_key, resolved_attempt_id),
            )
            if cursor.rowcount != 1:
                raise QueueError("activation transition requires reconciliation")
        return True

    def activation_leases(self, *, provider_id: str | None = None) -> list[ActivationLease]:
        self.initialize()
        where = "" if provider_id is None else " WHERE provider_id=?"
        parameters: tuple[Any, ...] = () if provider_id is None else (provider_id,)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM activation_leases{where} ORDER BY acquired_at,task_id",
                parameters,
            ).fetchall()
        return [ActivationLease(**dict(row)) for row in rows]

    def release_activation_after_dispatch(
        self,
        task_id: str,
        eligibility_key: str,
        release: Callable[[], None],
        *,
        attempt_id: str | None = None,
    ) -> bool:
        """Release a launch-scoped lease after its router launch is durably recorded.

        The claim and dispatched run remain live.  Only the activation lease is removed, so
        providers that can safely rotate credentials between calls do not stay pinned for the
        full background run.  The last holder performs the external release through the same
        committed ``releasing`` transition used by terminal bookkeeping.
        """

        self.initialize()
        needs_release = False
        with self._transaction() as connection:
            lease = connection.execute(
                """SELECT provider_id,account_id,state,attempt_id FROM activation_leases
                     WHERE task_id=? AND eligibility_key=?""",
                (task_id, eligibility_key),
            ).fetchone()
            if lease is None:
                return False
            if lease["state"] != "active":
                raise QueueError("launch-scoped activation lease requires reconciliation")
            resolved_attempt_id = lease["attempt_id"]
            if attempt_id is not None and resolved_attempt_id != attempt_id:
                raise QueueError("activation release attempt no longer owns the lease")
            dispatched = connection.execute(
                """SELECT 1 FROM runs
                     WHERE task=? AND eligibility_key=? AND status='dispatched'
                       AND provider_id=? AND account_id=? AND attempt_id IS ?""",
                (
                    task_id, eligibility_key, lease["provider_id"], lease["account_id"],
                    resolved_attempt_id,
                ),
            ).fetchone()
            if dispatched is None:
                raise QueueError("activation cannot release before a proven dispatch")
            holders = int(connection.execute(
                """SELECT COUNT(*) FROM activation_leases
                     WHERE provider_id=? AND account_id=?""",
                (lease["provider_id"], lease["account_id"]),
            ).fetchone()[0])
            if holders > 1:
                cursor = connection.execute(
                    """DELETE FROM activation_leases
                         WHERE task_id=? AND eligibility_key=? AND state='active'
                           AND attempt_id IS ?""",
                    (task_id, eligibility_key, resolved_attempt_id),
                )
                if cursor.rowcount != 1:
                    raise QueueError("launch-scoped activation release requires reconciliation")
                return False
            cursor = connection.execute(
                """UPDATE activation_leases SET state='releasing'
                     WHERE task_id=? AND eligibility_key=? AND state='active'
                       AND attempt_id IS ?""",
                (task_id, eligibility_key, resolved_attempt_id),
            )
            if cursor.rowcount != 1:
                raise QueueError("launch-scoped activation release requires reconciliation")
            needs_release = True

        if needs_release:
            release()
            with self._transaction() as connection:
                cursor = connection.execute(
                    """DELETE FROM activation_leases
                         WHERE task_id=? AND eligibility_key=? AND state='releasing'
                           AND attempt_id IS ?""",
                    (task_id, eligibility_key, resolved_attempt_id),
                )
                if cursor.rowcount != 1:
                    raise QueueError("launch-scoped activation release requires reconciliation")
        return needs_release

    def release_claim(self, task_id: str, eligibility_key: str, *, reason: str | None = None) -> bool:
        self.initialize()
        with self._connect() as connection:
            claim = connection.execute(
                "SELECT attempt_id FROM dispatch_claims WHERE task_id=? AND eligibility_key=?",
                (task_id, eligibility_key),
            ).fetchone()
        if claim is not None and claim["attempt_id"] is not None:
            return self.abort_unlaunched_attempt(
                task_id, eligibility_key, claim["attempt_id"],
                reason or "claim released before launch",
            )
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM activation_leases WHERE task_id=? AND eligibility_key=?",
                (task_id, eligibility_key),
            ).fetchone():
                raise QueueError("cannot release a claim while its activation lease is held")
            cursor = connection.execute(
                "DELETE FROM dispatch_claims WHERE task_id=? AND eligibility_key=?", (task_id, eligibility_key),
            )
            return cursor.rowcount > 0

    def abort_unlaunched_attempt(
        self,
        task_id: str,
        eligibility_key: str,
        attempt_id: str,
        reason: str,
        *,
        release_activation: Callable[[], None] | None = None,
    ) -> bool:
        """Atomically retain a proved-unlaunched attempt and release only its ownership."""

        self.initialize()
        detail = str(reason)[:1000]
        signature = "aborted:" + re.sub(r"[^a-z0-9]+", ":", detail.lower()).strip(":")[:500]
        with self._transaction() as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE id=? AND task_id=?",
                (attempt_id, task_id),
            ).fetchone()
            if attempt is None:
                raise QueueError(f"cannot abort missing attempt: {attempt_id}")
            if attempt["state"] == "aborted":
                newer = connection.execute(
                    "SELECT attempt_id FROM dispatch_claims WHERE task_id=? AND eligibility_key=?",
                    (task_id, eligibility_key),
                ).fetchone()
                if newer is not None and newer["attempt_id"] != attempt_id:
                    raise QueueError("aborted attempt no longer owns this claim")
                return False
            if attempt["state"] != "claimed":
                raise QueueError(f"attempt {attempt_id} cannot abort from {attempt['state']}")
            claim = connection.execute(
                "SELECT * FROM dispatch_claims WHERE task_id=? AND eligibility_key=? AND attempt_id=?",
                (task_id, eligibility_key, attempt_id),
            ).fetchone()
            if claim is None:
                raise QueueError("attempt no longer owns the dispatch claim")
            lease = connection.execute(
                "SELECT * FROM activation_leases WHERE task_id=? AND eligibility_key=? AND attempt_id=?",
                (task_id, eligibility_key, attempt_id),
            ).fetchone()
            if lease is not None and lease["state"] == "activating":
                raise QueueError(
                    "unproven activation requires reconciliation before abort"
                )
            if lease is not None and lease["state"] in {"active", "releasing"}:
                holders = int(connection.execute(
                    """SELECT COUNT(*) FROM activation_leases
                         WHERE provider_id=? AND account_id=?""",
                    (lease["provider_id"], lease["account_id"]),
                ).fetchone()[0])
                if holders == 1:
                    if release_activation is None:
                        raise QueueError("active activation release requires a verified callback")
                    release_activation()
            if lease is not None:
                connection.execute(
                    "DELETE FROM activation_leases WHERE task_id=? AND eligibility_key=? AND attempt_id=?",
                    (task_id, eligibility_key, attempt_id),
                )
            cursor = connection.execute(
                """UPDATE task_attempts
                     SET state='aborted',reason_code='unknown_launch',reason_signature=?,terminal_at=?
                     WHERE id=? AND task_id=? AND state='claimed'""",
                (signature, utc_now(), attempt_id, task_id),
            )
            if cursor.rowcount != 1:
                raise QueueError("attempt abort lost its state CAS")
            connection.execute(
                """UPDATE task_recovery
                     SET state='scheduled',consumed_by_attempt_id=NULL,updated_at=?,detail=?
                     WHERE task_id=? AND state='consumed' AND consumed_by_attempt_id=?""",
                (utc_now(), detail, task_id, attempt_id),
            )
            cursor = connection.execute(
                """DELETE FROM dispatch_claims
                     WHERE task_id=? AND eligibility_key=? AND attempt_id=?""",
                (task_id, eligibility_key, attempt_id),
            )
            if cursor.rowcount != 1:
                raise QueueError("attempt abort lost its claim CAS")
            return True

    def abandon_unproven_activation(self, task_id: str, eligibility_key: str) -> bool:
        """Drop an unproven ``activating`` lease after a known failed switch.

        Only ``activating`` is removable. ``active`` and ``releasing`` stay fail-closed
        for operator reconciliation because the external pin may already have moved.
        """

        self.initialize()
        with self._transaction() as connection:
            cursor = connection.execute(
                """DELETE FROM activation_leases
                     WHERE task_id=? AND eligibility_key=? AND state='activating'""",
                (task_id, eligibility_key),
            )
            return cursor.rowcount == 1

    def mark_ambiguous(self, task_id: str, eligibility_key: str, *, detail: str) -> None:
        self.initialize()
        with self._connect() as connection:
            claim = connection.execute(
                "SELECT attempt_id FROM dispatch_claims WHERE task_id=? AND eligibility_key=?",
                (task_id, eligibility_key),
            ).fetchone()
        if claim is not None and claim["attempt_id"] is not None:
            self.mark_attempt_ambiguous(
                task_id, eligibility_key, claim["attempt_id"], detail,
            )
            return
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE dispatch_claims SET state='ambiguous', detail=? WHERE task_id=? AND eligibility_key=?",
                (detail[:1000], task_id, eligibility_key),
            )
            if cursor.rowcount != 1:
                raise QueueError(f"cannot mark missing claim ambiguous: {task_id}")

    def mark_attempt_ambiguous(
        self,
        task_id: str,
        eligibility_key: str,
        attempt_id: str,
        reason: str,
        *,
        release_activation: Callable[[], None] | None = None,
    ) -> None:
        """Hold one exact claimed/dispatched attempt when launch ownership is unknown."""

        self.initialize()
        detail = str(reason)[:1000]
        signature = "unknown_launch:" + re.sub(
            r"[^a-z0-9]+", ":", detail.lower(),
        ).strip(":")[:500]
        with self._transaction() as connection:
            attempt = connection.execute(
                "SELECT state FROM task_attempts WHERE id=? AND task_id=?",
                (attempt_id, task_id),
            ).fetchone()
            if attempt is None:
                raise QueueError(f"cannot mark missing attempt ambiguous: {attempt_id}")
            if attempt["state"] == "ambiguous":
                owner = connection.execute(
                    "SELECT attempt_id FROM dispatch_claims WHERE task_id=? AND eligibility_key=?",
                    (task_id, eligibility_key),
                ).fetchone()
                if owner is None or owner["attempt_id"] != attempt_id:
                    raise QueueError("ambiguous attempt lost its retained owner")
                return
            if attempt["state"] not in {"claimed", "dispatched"}:
                raise QueueError(f"attempt {attempt_id} cannot become ambiguous from {attempt['state']}")
            claim = connection.execute(
                "SELECT attempt_id FROM dispatch_claims WHERE task_id=? AND eligibility_key=?",
                (task_id, eligibility_key),
            ).fetchone()
            if claim is None or claim["attempt_id"] != attempt_id:
                raise QueueError("attempt no longer owns the dispatch claim")
            lease = connection.execute(
                "SELECT state FROM activation_leases WHERE task_id=? AND eligibility_key=? AND attempt_id=?",
                (task_id, eligibility_key, attempt_id),
            ).fetchone()
            if lease is not None and release_activation is not None and lease["state"] == "active":
                release_activation()
                connection.execute(
                    "DELETE FROM activation_leases WHERE task_id=? AND eligibility_key=? AND attempt_id=?",
                    (task_id, eligibility_key, attempt_id),
                )
            cursor = connection.execute(
                """UPDATE task_attempts
                     SET state='ambiguous',reason_code='unknown_launch',reason_signature=?,terminal_at=?
                     WHERE id=? AND task_id=? AND state IN ('claimed','dispatched')""",
                (signature, utc_now(), attempt_id, task_id),
            )
            if cursor.rowcount != 1:
                raise QueueError("ambiguous attempt lost its state CAS")
            cursor = connection.execute(
                """UPDATE dispatch_claims SET state='ambiguous',detail=?
                     WHERE task_id=? AND eligibility_key=? AND attempt_id=?""",
                (detail, task_id, eligibility_key, attempt_id),
            )
            if cursor.rowcount != 1:
                raise QueueError("ambiguous attempt lost its claim CAS")

    def claims(self, *, state: str | None = None) -> list[Claim]:
        self.initialize()
        where = "" if state is None else " WHERE state=?"
        parameters: tuple[Any, ...] = () if state is None else (state,)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM dispatch_claims{where} ORDER BY claimed_at, task_id", parameters,
            ).fetchall()
        return [self._claim_from_row(row) for row in rows]

    def claim_for(self, task_id: str, eligibility_key: str | None = None) -> Claim | None:
        self.initialize()
        query = "SELECT * FROM dispatch_claims WHERE task_id=?"
        parameters: list[Any] = [task_id]
        if eligibility_key is not None:
            query += " AND eligibility_key=?"
            parameters.append(eligibility_key)
        query += " ORDER BY claimed_at DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return self._claim_from_row(row) if row else None

    def record(
        self, task_id: str, eligibility_key: str | None = None, *,
        attempt_id: str | None = None, status: str,
        outcome: Mapping[str, Any] | None = None,
        provider_id: str | None = None, account_id: str | None = None, kind: str | None = None,
        cycle: int | None = None, ts: str | None = None, branch: str | None = None,
        summary: str | None = None, router_job_id: str | None = None,
        timestamp: str | None = None, trigger: str | None = None,
        release_activation: Callable[[], None] | None = None,
        now_epoch: float | None = None,
    ) -> RunEvent:
        if trigger not in {None, "manual", "bonus", "scheduled", "continuation"}:
            raise QueueError("invalid run trigger")
        if status not in VALID_STATUSES:
            raise QueueError(f"invalid run status: {status}")
        if provider_id == "auto":
            raise QueueError("run provider_id must be concrete, never auto")
        self.initialize()
        resolved_cycle = int(cycle if cycle is not None else cycle_from_key(eligibility_key))
        receipt_now = float(time.time() if now_epoch is None else now_epoch)
        receipt_timestamp = datetime.fromtimestamp(
            receipt_now, timezone.utc,
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        event_timestamp = ts or timestamp or receipt_timestamp

        def normalized_outcome(require_reason: bool) -> dict[str, Any] | None:
            try:
                return validate_outcome(
                    status, outcome, require_structured_reason=require_reason,
                )
            except QueueError:
                if status not in {"failed", "skipped"} or not require_reason:
                    raise
                detail = (summary or f"{status} completion remains unverified")[:2000]
                return validate_outcome(status, {
                    "reason": {
                        "code": "verification_needed",
                        "detail": detail,
                        "signature": "verification_needed:" + re.sub(
                            r"[^a-z0-9]+", ":", detail.lower(),
                        ).strip(":")[:500],
                    },
                })

        with self._connect() as connection:
            task_row = connection.execute(
                "SELECT kind FROM tasks WHERE id=?", (task_id,),
            ).fetchone()
            initial_claim = (
                connection.execute(
                    "SELECT eligibility_key FROM dispatch_claims WHERE task_id=? AND attempt_id=?",
                    (task_id, attempt_id),
                ).fetchone()
                if attempt_id is not None else None
            )
        if task_row is None:
            raise QueueError(f"unknown task: {task_id}")
        resolved_kind = str(task_row["kind"])
        if kind is not None and kind != resolved_kind:
            raise QueueError(
                f"record kind {kind} does not match task kind {resolved_kind}"
            )
        if eligibility_key is None and initial_claim is not None:
            eligibility_key = initial_claim["eligibility_key"]
        canonical = normalized_outcome(
            attempt_id is not None and status in TERMINAL_STATUSES
        )
        canonical_json = _canonical_json(canonical) if canonical is not None else None

        # Preserve the established two-phase activation release. A failed external
        # release leaves a durable `releasing` lease and does not append a terminal row.
        prepared_release = False
        if (
            attempt_id is not None and status in TERMINAL_STATUSES and
            eligibility_key is not None and release_activation is not None
        ):
            with self._transaction() as connection:
                prior = connection.execute(
                    """SELECT 1 FROM runs WHERE task=? AND attempt_id=?
                         AND status IN ('done','skipped','failed','awaiting_human') LIMIT 1""",
                    (task_id, attempt_id),
                ).fetchone()
                if prior is None:
                    live_attempt = connection.execute(
                        "SELECT state FROM task_attempts WHERE id=? AND task_id=?",
                        (attempt_id, task_id),
                    ).fetchone()
                    owner = connection.execute(
                        """SELECT 1 FROM dispatch_claims
                             WHERE task_id=? AND eligibility_key=? AND attempt_id=?""",
                        (task_id, eligibility_key, attempt_id),
                    ).fetchone()
                    if (
                        live_attempt is None
                        or live_attempt["state"] not in {"claimed", "dispatched", "ambiguous"}
                        or owner is None
                    ):
                        raise QueueError(
                            "activation release requires the exact live attempt owner"
                        )
                lease = connection.execute(
                    """SELECT * FROM activation_leases
                         WHERE task_id=? AND eligibility_key=? AND attempt_id=?""",
                    (task_id, eligibility_key, attempt_id),
                ).fetchone()
                if prior is None and lease is not None and lease["state"] == "active":
                    holders = int(connection.execute(
                        """SELECT COUNT(*) FROM activation_leases
                             WHERE provider_id=? AND account_id=?""",
                        (lease["provider_id"], lease["account_id"]),
                    ).fetchone()[0])
                    if holders == 1:
                        cursor = connection.execute(
                            """UPDATE activation_leases SET state='releasing'
                                 WHERE task_id=? AND eligibility_key=? AND attempt_id=?
                                   AND state='active'""",
                            (task_id, eligibility_key, attempt_id),
                        )
                        if cursor.rowcount != 1:
                            raise QueueError("activation release transition requires reconciliation")
                        prepared_release = True
            if prepared_release:
                release_activation()

        with self._transaction() as connection:
            task_row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task_row is None:
                raise QueueError(f"unknown task: {task_id}")
            if task_row["kind"] != resolved_kind:
                raise QueueError("task kind changed while recording")
            if eligibility_key and trigger is None:
                previous = connection.execute(
                    """SELECT trigger FROM runs WHERE task=? AND eligibility_key=?
                         AND trigger IS NOT NULL ORDER BY rowid_pk DESC LIMIT 1""",
                    (task_id, eligibility_key),
                ).fetchone()
                if previous:
                    trigger = previous[0]

            if attempt_id is not None:
                attempt = connection.execute(
                    "SELECT * FROM task_attempts WHERE id=? AND task_id=?",
                    (attempt_id, task_id),
                ).fetchone()
                if attempt is None:
                    raise QueueError(f"unknown attempt for {task_id}: {attempt_id}")
                prior = connection.execute(
                    """SELECT * FROM runs WHERE task=? AND attempt_id=?
                         AND status IN ('done','skipped','failed','awaiting_human') ORDER BY rowid_pk DESC LIMIT 1""",
                    (task_id, attempt_id),
                ).fetchone()
                if prior is not None:
                    if prior["status"] != status:
                        raise QueueError(
                            f"terminal event already recorded as {prior['status']} for {task_id}"
                        )
                    if prior["outcome_json"] != canonical_json:
                        raise QueueError(
                            f"attempt {attempt_id} already recorded with conflicting terminal outcome"
                        )
                    return self._run_from_row(prior)
                if status == "dispatched" and attempt["state"] != "claimed":
                    existing = connection.execute(
                        "SELECT * FROM runs WHERE task=? AND attempt_id=? AND status='dispatched'",
                        (task_id, attempt_id),
                    ).fetchone()
                    if existing is not None and attempt["state"] == "dispatched":
                        return self._run_from_row(existing)
                    raise QueueError(f"attempt {attempt_id} cannot dispatch from {attempt['state']}")
                if status in TERMINAL_STATUSES and attempt["state"] not in {
                    "claimed", "dispatched", "ambiguous",
                }:
                    raise QueueError(f"attempt {attempt_id} already recorded as {attempt['state']}")
                claim = connection.execute(
                    """SELECT * FROM dispatch_claims
                         WHERE task_id=? AND attempt_id=?""",
                    (task_id, attempt_id),
                ).fetchone()
                if claim is None:
                    raise QueueError(f"attempt {attempt_id} no longer owns a dispatch claim")
                if eligibility_key is None:
                    eligibility_key = claim["eligibility_key"]
                if claim["eligibility_key"] != eligibility_key:
                    raise QueueError("attempt eligibility key no longer owns the claim")
                provider_id = provider_id or claim["provider_id"]
                account_id = account_id or claim["account_id"]
                if status in TERMINAL_STATUSES:
                    lease = connection.execute(
                        """SELECT * FROM activation_leases
                             WHERE task_id=? AND eligibility_key=? AND attempt_id=?""",
                        (task_id, eligibility_key, attempt_id),
                    ).fetchone()
                    if lease is not None:
                        if lease["state"] == "releasing" and prepared_release:
                            pass
                        elif lease["state"] != "active":
                            raise QueueError("incomplete activation lease requires reconciliation")
                        else:
                            holders = int(connection.execute(
                                """SELECT COUNT(*) FROM activation_leases
                                     WHERE provider_id=? AND account_id=?""",
                                (lease["provider_id"], lease["account_id"]),
                            ).fetchone()[0])
                            if holders == 1:
                                raise QueueError("last activation lease requires a verified release callback")
                        connection.execute(
                            """DELETE FROM activation_leases
                                 WHERE task_id=? AND eligibility_key=? AND attempt_id=?""",
                            (task_id, eligibility_key, attempt_id),
                        )
                cursor = connection.execute(
                    """INSERT INTO runs(
                         task,kind,cycle,eligibility_key,status,ts,received_at,branch,summary,engine,
                         provider_id,account_id,router_job_id,trigger,attempt_id,outcome_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        task_id, resolved_kind, resolved_cycle, eligibility_key, status,
                        event_timestamp, receipt_timestamp, branch, summary, provider_id,
                        provider_id, account_id,
                        router_job_id, trigger, attempt_id, canonical_json,
                    ),
                )
                reason = canonical.get("reason") if isinstance(canonical, dict) else None
                terminal_at = receipt_timestamp if status in TERMINAL_STATUSES else None
                update = connection.execute(
                    """UPDATE task_attempts
                         SET state=?,reason_code=?,reason_signature=?,outcome_json=?,terminal_at=?
                         WHERE id=? AND task_id=? AND state=?""",
                    (
                        status,
                        reason.get("code") if isinstance(reason, dict) else None,
                        reason.get("signature") if isinstance(reason, dict) else None,
                        canonical_json, terminal_at, attempt_id, task_id, attempt["state"],
                    ),
                )
                if update.rowcount != 1:
                    raise QueueError("attempt record lost its state CAS")
                if status in TERMINAL_STATUSES:
                    deleted = connection.execute(
                        """DELETE FROM dispatch_claims
                             WHERE task_id=? AND eligibility_key=? AND attempt_id=?""",
                        (task_id, eligibility_key, attempt_id),
                    )
                    if deleted.rowcount != 1:
                        raise QueueError("attempt terminal record lost its claim CAS")
                    if attempt["mode"] != "normal":
                        recovered = connection.execute(
                            """DELETE FROM task_recovery
                                 WHERE task_id=? AND state='consumed'
                                   AND consumed_by_attempt_id=?""",
                            (task_id, attempt_id),
                        )
                        if recovered.rowcount != 1:
                            raise QueueError(
                                "recovery terminal record lost its projection CAS"
                            )
                row = connection.execute(
                    "SELECT * FROM runs WHERE rowid_pk=?", (cursor.lastrowid,),
                ).fetchone()
                assert row is not None
                return self._run_from_row(row)

            # NULL-attempt operations are a bounded compatibility path. They can never
            # terminalize or release a claim owned by the new runtime.
            nonnull_owner = connection.execute(
                "SELECT attempt_id FROM dispatch_claims WHERE task_id=? AND attempt_id IS NOT NULL",
                (task_id,),
            ).fetchone()
            if nonnull_owner is not None:
                raise QueueError(
                    f"attempt id {nonnull_owner['attempt_id']} is required for this claimed execution"
                )
            legacy_claim = None
            if eligibility_key is not None:
                legacy_claim = connection.execute(
                    """SELECT * FROM dispatch_claims
                         WHERE task_id=? AND eligibility_key=? AND attempt_id IS NULL""",
                    (task_id, eligibility_key),
                ).fetchone()
            if status in TERMINAL_STATUSES:
                if eligibility_key is None:
                    prior_rows = connection.execute(
                        """SELECT * FROM runs WHERE task=? AND attempt_id IS NULL
                             AND eligibility_key IS NULL AND cycle=?
                             AND status IN ('done','skipped','failed','awaiting_human') ORDER BY rowid_pk""",
                        (task_id, resolved_cycle),
                    ).fetchall()
                else:
                    prior_rows = connection.execute(
                        """SELECT * FROM runs WHERE task=? AND attempt_id IS NULL
                             AND (eligibility_key=? OR (eligibility_key IS NULL AND cycle=?))
                             AND status IN ('done','skipped','failed','awaiting_human') ORDER BY rowid_pk""",
                        (task_id, eligibility_key, resolved_cycle),
                    ).fetchall()
                if len(prior_rows) > 1:
                    raise QueueError(f"multiple legacy terminal events exist for {task_id}")
                if prior_rows:
                    prior = prior_rows[0]
                    if prior["kind"] != resolved_kind:
                        raise QueueError(
                            "legacy terminal kind conflicts with the canonical task kind"
                        )
                    if prior["status"] != status:
                        raise QueueError(
                            f"terminal event already recorded as {prior['status']} for {task_id}"
                        )
                    if canonical_json is not None and prior["outcome_json"] not in {None, canonical_json}:
                        raise QueueError("legacy terminal outcome conflicts with its existing record")
                    return self._run_from_row(prior)
            if status == "done" and resolved_kind == "oneoff" and legacy_claim is None:
                raise QueueError("new one-off done records require a verified attempt")
            if legacy_claim is not None:
                provider_id = provider_id or legacy_claim["provider_id"]
                account_id = account_id or legacy_claim["account_id"]
            cursor = connection.execute(
                """INSERT INTO runs(
                     task,kind,cycle,eligibility_key,status,ts,received_at,branch,summary,engine,
                     provider_id,account_id,router_job_id,trigger,attempt_id,outcome_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                (
                    task_id, resolved_kind, resolved_cycle, eligibility_key, status,
                    event_timestamp, receipt_timestamp, branch, summary, provider_id,
                    provider_id, account_id,
                    router_job_id, trigger, canonical_json,
                ),
            )
            if status in TERMINAL_STATUSES and legacy_claim is not None:
                if connection.execute(
                    """SELECT 1 FROM activation_leases
                         WHERE task_id=? AND eligibility_key=? AND attempt_id IS NULL""",
                    (task_id, eligibility_key),
                ).fetchone():
                    if release_activation is None:
                        raise QueueError("legacy activation release requires a verified callback")
                    release_activation()
                    connection.execute(
                        """DELETE FROM activation_leases
                             WHERE task_id=? AND eligibility_key=? AND attempt_id IS NULL""",
                        (task_id, eligibility_key),
                    )
                connection.execute(
                    """DELETE FROM dispatch_claims
                         WHERE task_id=? AND eligibility_key=? AND attempt_id IS NULL""",
                    (task_id, eligibility_key),
                )
            row = connection.execute(
                "SELECT * FROM runs WHERE rowid_pk=?", (cursor.lastrowid,),
            ).fetchone()
            assert row is not None
            return self._run_from_row(row)

    def _run_requeue_projection(
        self, connection: sqlite3.Connection, row: sqlite3.Row,
    ) -> dict[str, Any]:
        task_id = row["task"]
        if connection.execute(
            """SELECT 1 FROM goal_turns WHERE task_id=?
                 UNION ALL SELECT 1 FROM goal_members WHERE task_id=? AND managed=1 LIMIT 1""",
            (task_id, task_id),
        ).fetchone():
            return {"allowed": False, "reason": "Fresh goal follow-up required"}
        claim = connection.execute(
            "SELECT state FROM dispatch_claims WHERE task_id=?", (task_id,),
        ).fetchone()
        if claim is not None:
            reason = "Launch ownership is ambiguous" if claim["state"] == "ambiguous" else "Run is active"
            return {"allowed": False, "reason": reason}
        if self._verified_done_row(connection, task_id) is not None:
            return {"allowed": False, "reason": "Verified work is complete"}
        latest = self._latest_effective_attempt(connection, task_id)
        legacy = self._latest_legacy_run(connection, task_id) if latest is None else None
        is_source = (
            (latest is not None and row["attempt_id"] == latest["id"]) or
            (latest is None and legacy is not None and row["rowid_pk"] == legacy["rowid_pk"])
        )
        if not is_source or row["status"] not in {"failed", "skipped"}:
            return {"allowed": False, "reason": "Only the latest failed or skipped outcome can recover"}
        reason_code = (
            latest["reason_code"]
            if latest is not None else self._source_reason(None, legacy)[0]
        )
        if reason_code in {"authority_required", "permanent", "unknown_launch"}:
            return {"allowed": False, "reason": reason_code.replace("_", " ").capitalize()}
        recovery = connection.execute(
            """SELECT state,origin,reason_code,detail,consumed_by_attempt_id
                 FROM task_recovery WHERE task_id=?""",
            (task_id,),
        ).fetchone()
        consumed_live = False
        if recovery is not None and recovery["state"] == "consumed":
            consumer = connection.execute(
                "SELECT state FROM task_attempts WHERE id=? AND task_id=?",
                (recovery["consumed_by_attempt_id"], task_id),
            ).fetchone()
            owner = connection.execute(
                "SELECT 1 FROM dispatch_claims WHERE task_id=? AND attempt_id=?",
                (task_id, recovery["consumed_by_attempt_id"]),
            ).fetchone()
            consumed_live = bool(
                consumer is not None
                and consumer["state"] in {"claimed", "dispatched", "ambiguous"}
                and owner is not None
            )
        if recovery is not None and (
            recovery["state"] == "held"
            or consumed_live
            or (
                recovery["state"] == "exhausted"
                and recovery["origin"] != "automatic"
            )
        ):
            return {
                "allowed": False,
                "reason": recovery["detail"] or recovery["reason_code"].replace("_", " ").capitalize(),
            }
        return {"allowed": True, "reason": "Operator retry is available"}

    def runs(self, *, limit: int | None = None, task_id: str | None = None) -> list[RunEvent]:
        self.initialize()
        where = "" if task_id is None else " WHERE task=?"
        parameters: list[Any] = [] if task_id is None else [task_id]
        query = f"SELECT * FROM runs{where} ORDER BY rowid_pk DESC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(max(0, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [
                replace(
                    self._run_from_row(row),
                    requeue=self._run_requeue_projection(connection, row),
                )
                for row in rows
            ]

    def inflight(self, *, provider_id: str | None = None, include_legacy: bool = False) -> list[RunEvent]:
        events = self.runs()
        terminal_attempts = {
            event.attempt_id for event in events
            if event.attempt_id is not None and event.status in TERMINAL_STATUSES
        }
        terminal_keys: set[tuple[str, str]] = set()
        terminal_cycles: set[tuple[str, int]] = set()
        null_terminal_cycles: set[tuple[str, int]] = set()
        result: list[RunEvent] = []
        for event in events:
            if event.attempt_id is None and event.status in TERMINAL_STATUSES:
                terminal_cycles.add((event.task, event.cycle))
                if event.eligibility_key is None:
                    null_terminal_cycles.add((event.task, event.cycle))
                else:
                    terminal_keys.add((event.task, event.eligibility_key))
                continue
            if event.status != "dispatched":
                continue
            if event.attempt_id is not None:
                completed = event.attempt_id in terminal_attempts
            elif event.eligibility_key is None:
                completed = (event.task, event.cycle) in terminal_cycles
            else:
                completed = (
                    (event.task, event.eligibility_key) in terminal_keys
                    or (event.task, event.cycle) in null_terminal_cycles
                )
            if completed:
                continue
            effective = event.provider_id
            if provider_id is not None and effective != provider_id:
                if not (include_legacy and effective is None):
                    continue
            result.append(event)
        return result

    def inflight_details(
        self, *, provider_id: str | None = None, include_legacy: bool = False,
        now_epoch: int | None = None,
    ) -> list[dict[str, Any]]:
        now = int(time.time() if now_epoch is None else now_epoch)
        result: list[dict[str, Any]] = []
        for event in self.inflight(provider_id=provider_id, include_legacy=include_legacy):
            value = event.to_dict()
            try:
                value["age_seconds"] = max(0, now - int(_timestamp_epoch(event.ts)))
            except ValueError:
                value["age_seconds"] = None
            result.append(value)
        return result

    def attempts(self, *, task_id: str | None = None) -> list[DispatchAttempt]:
        self.initialize()
        query = "SELECT * FROM task_attempts"
        parameters: tuple[Any, ...] = ()
        if task_id is not None:
            query += " WHERE task_id=?"
            parameters = (task_id,)
        query += " ORDER BY task_id,ordinal"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    def recoveries(self) -> list[RecoveryDecision]:
        self.initialize()
        with self._connect() as connection:
            counts = self._blocked_descendant_counts(connection)
            rows = connection.execute("SELECT * FROM task_recovery ORDER BY task_id").fetchall()
        return [
            self._recovery_from_row(row, blocked_descendants=counts.get(row["task_id"], 0))
            for row in rows
        ]

    def recovery_for(self, task_id: str) -> RecoveryDecision | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_recovery WHERE task_id=?", (task_id,),
            ).fetchone()
            if row is None:
                return None
            count = self._blocked_descendant_counts(connection).get(task_id, 0)
        return self._recovery_from_row(row, blocked_descendants=count)

    @staticmethod
    def _recovery_source(
        connection: sqlite3.Connection,
        task_id: str,
        expected_attempt_id: str | None,
        expected_legacy_run_rowid: int | None,
    ) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        if (expected_attempt_id is None) == (expected_legacy_run_rowid is None):
            raise QueueError("exactly one recovery source attempt or legacy run is required")
        if expected_attempt_id is not None:
            source = connection.execute(
                "SELECT * FROM task_attempts WHERE id=? AND task_id=?",
                (expected_attempt_id, task_id),
            ).fetchone()
            if source is None:
                raise QueueError(f"recovery source attempt is stale: {expected_attempt_id}")
            latest = QueueDB._latest_effective_attempt(connection, task_id)
            if latest is None or latest["id"] != expected_attempt_id:
                successor = latest["id"] if latest is not None else "none"
                raise QueueError(f"recovery source is stale; latest successor is {successor}")
            if source["state"] not in {"failed", "skipped"}:
                raise QueueError(f"recovery source is {source['state']}, not failed or skipped")
            return source, None
        if QueueDB._latest_effective_attempt(connection, task_id) is not None:
            latest = QueueDB._latest_effective_attempt(connection, task_id)
            assert latest is not None
            raise QueueError(f"legacy recovery source is stale; successor is {latest['id']}")
        legacy = connection.execute(
            """SELECT * FROM runs WHERE rowid_pk=? AND task=? AND attempt_id IS NULL""",
            (expected_legacy_run_rowid, task_id),
        ).fetchone()
        latest_legacy = QueueDB._latest_legacy_run(connection, task_id)
        if legacy is None or latest_legacy is None or legacy["rowid_pk"] != latest_legacy["rowid_pk"]:
            raise QueueError("legacy recovery source is stale")
        if legacy["status"] not in {"failed", "skipped"}:
            raise QueueError(f"legacy recovery source is {legacy['status']}, not failed or skipped")
        return None, legacy

    @staticmethod
    def _source_reason(
        attempt: sqlite3.Row | None, legacy: sqlite3.Row | None,
    ) -> tuple[str, str | None, str, str]:
        if attempt is not None:
            code = attempt["reason_code"] or "verification_needed"
            signature = attempt["reason_signature"]
            detail = "completion requires another verified attempt"
            if attempt["outcome_json"]:
                parsed = json.loads(attempt["outcome_json"])
                reason = parsed.get("reason") if isinstance(parsed, dict) else None
                if isinstance(reason, dict):
                    detail = str(reason.get("detail") or detail)[:1000]
            mode = "retry" if code == "retryable" else "verification"
            return code, signature, detail, mode
        assert legacy is not None
        if legacy["outcome_json"]:
            parsed = json.loads(legacy["outcome_json"])
            reason = parsed.get("reason") if isinstance(parsed, dict) else None
            if isinstance(reason, dict) and reason.get("code") in REASON_CODES:
                code = str(reason["code"])
                return (
                    code,
                    str(reason.get("signature") or "") or None,
                    str(reason.get("detail") or "legacy completion is unverified")[:1000],
                    "retry" if code == "retryable" else "verification",
                )
        return (
            "verification_needed", None,
            "legacy failure has no structured verification outcome", "verification",
        )

    def _request_recovery_in_connection(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        *,
        expected_attempt_id: str | None,
        expected_legacy_run_rowid: int | None,
        mode: Literal["retry", "verification"] | None,
        source: Literal["automatic", "operator"],
        now_epoch: float,
        max_automatic_recoveries: int = 2,
        backoff_seconds: tuple[int, ...] = (300, 1800),
        dry_run: bool = False,
        blocked_descendants: int = 0,
    ) -> RecoveryDecision:
        from .goals import recovery_admission
        task_row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task_row is None:
            raise QueueError(f"no such task: {task_id}")
        task = self._task_from_row(task_row)
        if task.kind != "oneoff":
            raise QueueError("only one-off work can use retained recovery")
        if self._verified_done_row(connection, task_id) is not None:
            raise QueueError(f"{task_id} is done; refusing recovery")
        if connection.execute(
            "SELECT 1 FROM dispatch_claims WHERE task_id=?", (task_id,),
        ).fetchone():
            raise QueueError(f"{task_id} has active or ambiguous launch ownership")
        attempt, legacy = self._recovery_source(
            connection, task_id, expected_attempt_id, expected_legacy_run_rowid,
        )
        contract_hash = _contract_hash(task)
        if attempt is not None and attempt["contract_hash"] != contract_hash:
            raise QueueError("recovery source contract changed")
        reason_code, reason_signature, detail, inferred_mode = self._source_reason(attempt, legacy)
        chosen_mode = mode or inferred_mode
        if chosen_mode not in RECOVERY_MODES:
            raise QueueError("recovery mode must be retry or verification")
        if reason_code != "retryable":
            chosen_mode = "verification"
        admitted, admission_reason = recovery_admission(
            connection, task_id, now_epoch=float(now_epoch),
        )
        persist = True
        state = "scheduled" if source == "operator" else "backoff"
        decision_code = reason_code
        decision_detail = detail
        automatic_count = int(connection.execute(
            """SELECT COUNT(*) FROM task_attempts
                 WHERE task_id=? AND origin='automatic'
                   AND mode IN ('retry','verification') AND state!='aborted'""",
            (task_id,),
        ).fetchone()[0])
        if not admitted:
            state, decision_code, decision_detail = "held", admission_reason, admission_reason
            persist = admission_reason != "fresh_goal_followup_required"
        elif reason_code in {"authority_required", "permanent", "unknown_launch"}:
            state = "held"
        elif attempt is not None and attempt["recovery_of"] is not None:
            previous = connection.execute(
                "SELECT reason_signature FROM task_attempts WHERE id=?",
                (attempt["recovery_of"],),
            ).fetchone()
            if (
                reason_signature
                and previous is not None
                and previous["reason_signature"] == reason_signature
            ):
                state, decision_code = "held", "no_progress"
                decision_detail = "recovery repeated the same normalized failure"
        if (
            source == "automatic"
            and state not in {"held", "exhausted"}
            and automatic_count >= max_automatic_recoveries
        ):
            state, decision_code = "exhausted", "recovery_exhausted"
            decision_detail = "automatic recovery attempt limit reached"
        not_before: str | None = None
        if state in {"scheduled", "backoff"}:
            if source == "operator":
                due = float(now_epoch)
                state = "scheduled"
            else:
                delay_index = min(automatic_count, max(0, len(backoff_seconds) - 1))
                received_at = (
                    attempt["terminal_at"]
                    if attempt is not None else legacy["received_at"]
                )
                anchor = (
                    _timestamp_epoch(received_at)
                    if received_at is not None else float(now_epoch)
                )
                due = anchor + backoff_seconds[delay_index]
                state = "backoff"
            not_before = datetime.fromtimestamp(
                due, timezone.utc,
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        updated_at = datetime.fromtimestamp(
            float(now_epoch), timezone.utc,
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        after_attempt = attempt["id"] if attempt is not None else None
        after_legacy = legacy["rowid_pk"] if legacy is not None else None
        existing = connection.execute(
            "SELECT * FROM task_recovery WHERE task_id=?", (task_id,),
        ).fetchone()
        same_source_contract = existing is not None and (
            existing["after_attempt_id"] == after_attempt and
            existing["after_legacy_run_rowid"] == after_legacy and
            existing["contract_hash"] == contract_hash
        )
        if (
            same_source_contract
            and source == "automatic"
            and existing["origin"] == "operator"
            and existing["state"] != "consumed"
        ):
            return self._recovery_from_row(
                existing, blocked_descendants=blocked_descendants,
            )
        transient_hold = bool(
            same_source_contract
            and existing["origin"] == source
            and existing["state"] == "held"
            and existing["reason_code"] in TRANSIENT_RECOVERY_HOLDS
        )
        if (
            same_source_contract
            and existing["origin"] == source
            and not transient_hold
        ):
            return self._recovery_from_row(
                existing, blocked_descendants=blocked_descendants,
            )
        decision = RecoveryDecision(
            task_id, after_attempt, after_legacy, chosen_mode, source, state, None,
            contract_hash, not_before, decision_code, reason_signature,
            decision_detail, updated_at, blocked_descendants,
        )
        if dry_run or not persist:
            return decision
        connection.execute(
            """INSERT INTO task_recovery(
                 task_id,after_attempt_id,after_legacy_run_rowid,mode,origin,state,
                 consumed_by_attempt_id,contract_hash,not_before,reason_code,
                 reason_signature,detail,updated_at
               ) VALUES(?,?,?,?,?,?,NULL,?,?,?,?,?,?)
               ON CONFLICT(task_id) DO UPDATE SET
                 after_attempt_id=excluded.after_attempt_id,
                 after_legacy_run_rowid=excluded.after_legacy_run_rowid,
                 mode=excluded.mode,origin=excluded.origin,state=excluded.state,
                 consumed_by_attempt_id=NULL,contract_hash=excluded.contract_hash,
                 not_before=excluded.not_before,reason_code=excluded.reason_code,
                 reason_signature=excluded.reason_signature,detail=excluded.detail,
                 updated_at=excluded.updated_at""",
            (
                task_id, after_attempt, after_legacy, chosen_mode, source, state,
                contract_hash, not_before, decision_code, reason_signature,
                decision_detail, updated_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM task_recovery WHERE task_id=?", (task_id,),
        ).fetchone()
        assert row is not None
        return self._recovery_from_row(row, blocked_descendants=blocked_descendants)

    def request_recovery(
        self,
        task_id: str,
        *,
        expected_attempt_id: str | None = None,
        expected_legacy_run_rowid: int | None = None,
        mode: Literal["retry", "verification"] | None = None,
        source: Literal["automatic", "operator"],
        now_epoch: float | None = None,
    ) -> RecoveryDecision:
        from .goals import guard_history
        _require_task_id(task_id)
        if source not in {"automatic", "operator"}:
            raise QueueError("recovery source must be automatic or operator")
        now = float(time.time() if now_epoch is None else now_epoch)
        self.initialize()
        with self._transaction() as connection:
            if source == "operator":
                guard_history(connection, task_id)
            count = self._blocked_descendant_counts(connection).get(task_id, 0)
            return self._request_recovery_in_connection(
                connection, task_id,
                expected_attempt_id=expected_attempt_id,
                expected_legacy_run_rowid=expected_legacy_run_rowid,
                mode=mode, source=source, now_epoch=now,
                blocked_descendants=count,
            )

    def reconcile_recoveries(
        self,
        *,
        now_epoch: int,
        dry_run: bool,
        max_automatic_recoveries: int = 2,
        backoff_seconds: tuple[int, ...] = (300, 1800),
    ) -> tuple[RecoveryDecision, ...]:
        self.initialize()
        manager = self._connect() if dry_run else self._transaction()
        with manager as connection:
            counts = self._blocked_descendant_counts(connection)
            managed = {
                row["task_id"] for row in connection.execute(
                    "SELECT task_id FROM goal_members WHERE managed=1"
                )
            }
            results: list[RecoveryDecision] = []
            for row in connection.execute(
                "SELECT * FROM tasks WHERE active=1 AND kind='oneoff' ORDER BY priority,created_at,id"
            ):
                task_id = row["id"]
                if counts.get(task_id, 0) <= 0 and task_id not in managed:
                    continue
                if self._verified_done_row(connection, task_id) is not None:
                    continue
                attempt = self._latest_effective_attempt(connection, task_id)
                legacy = self._latest_legacy_run(connection, task_id) if attempt is None else None
                if attempt is not None and attempt["state"] not in {"failed", "skipped"}:
                    continue
                if attempt is None and (legacy is None or legacy["status"] not in {"failed", "skipped"}):
                    continue
                try:
                    decision = self._request_recovery_in_connection(
                        connection, task_id,
                        expected_attempt_id=attempt["id"] if attempt is not None else None,
                        expected_legacy_run_rowid=legacy["rowid_pk"] if legacy is not None else None,
                        mode=None, source="automatic", now_epoch=now_epoch,
                        max_automatic_recoveries=max_automatic_recoveries,
                        backoff_seconds=backoff_seconds, dry_run=dry_run,
                        blocked_descendants=counts.get(task_id, 0),
                    )
                except QueueError as exc:
                    detail = str(exc)
                    decision = RecoveryDecision(
                        task_id,
                        attempt["id"] if attempt is not None else None,
                        legacy["rowid_pk"] if legacy is not None else None,
                        "verification", "automatic", "held", None,
                        _contract_hash(self._task_from_row(row)), None,
                        "recovery_held", None, detail,
                        datetime.fromtimestamp(now_epoch, timezone.utc).replace(
                            microsecond=0,
                        ).isoformat().replace("+00:00", "Z"),
                        counts.get(task_id, 0),
                    )
                if dry_run and decision.state in {"scheduled", "backoff"}:
                    decision = RecoveryDecision(
                        **{**decision.to_dict(), "state": "would_schedule"}
                    )
                results.append(decision)
            return tuple(results)

    def _recovered_completion_replay(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        expected_attempt_id: str | None,
        expected_legacy_run_rowid: int | None,
        canonical: str,
        summary: str,
    ) -> RunEvent | None:
        query = (
            "SELECT a.*,r.rowid_pk FROM task_attempts a JOIN runs r ON r.attempt_id=a.id "
            "WHERE a.task_id=? AND a.mode='verification' AND a.origin='continuation' "
        )
        parameters: list[Any] = [task_id]
        if expected_attempt_id is not None:
            query += "AND a.recovery_of=? AND a.recovery_of_legacy_run_rowid IS NULL "
            parameters.append(expected_attempt_id)
        else:
            query += "AND a.recovery_of IS NULL AND a.recovery_of_legacy_run_rowid=? "
            parameters.append(expected_legacy_run_rowid)
        query += "ORDER BY a.ordinal DESC LIMIT 1"
        replay = connection.execute(query, parameters).fetchone()
        if replay is None:
            return None
        run = connection.execute(
            "SELECT * FROM runs WHERE rowid_pk=?", (replay["rowid_pk"],),
        ).fetchone()
        assert run is not None
        if replay["outcome_json"] == canonical and run["summary"] == summary:
            return self._run_from_row(run)
        raise QueueError("source was already completed with conflicting evidence")

    def recover_complete(
        self,
        task_id: str,
        *,
        expected_attempt_id: str | None = None,
        expected_legacy_run_rowid: int | None = None,
        outcome: Mapping[str, Any],
        summary: str,
        now_epoch: float | None = None,
    ) -> RunEvent:
        from .goals import recovery_admission
        _require_task_id(task_id)
        if (expected_attempt_id is None) == (expected_legacy_run_rowid is None):
            raise QueueError("exactly one completion source attempt or legacy run is required")
        verified = validate_outcome("done", outcome, require_structured_reason=True)
        assert verified is not None
        canonical = _canonical_json(verified)
        now = float(time.time() if now_epoch is None else now_epoch)
        stamp = datetime.fromtimestamp(now, timezone.utc).replace(
            microsecond=0,
        ).isoformat().replace("+00:00", "Z")
        self.initialize()
        with self._connect() as connection:
            replay = self._recovered_completion_replay(
                connection, task_id, expected_attempt_id,
                expected_legacy_run_rowid, canonical, summary,
            )
        if replay is not None:
            return replay
        dependency_snapshot = self._dependency_preflight(task_id)
        with self._transaction() as connection:
            replay = self._recovered_completion_replay(
                connection, task_id, expected_attempt_id,
                expected_legacy_run_rowid, canonical, summary,
            )
            if replay is not None:
                return replay
            attempt, legacy = self._recovery_source(
                connection, task_id, expected_attempt_id, expected_legacy_run_rowid,
            )
            if connection.execute(
                "SELECT 1 FROM dispatch_claims WHERE task_id=?", (task_id,),
            ).fetchone() or connection.execute(
                "SELECT 1 FROM activation_leases WHERE task_id=?", (task_id,),
            ).fetchone():
                successor = self._latest_effective_attempt(connection, task_id)
                successor_id = successor["id"] if successor is not None else "unknown"
                raise QueueError(f"successor attempt {successor_id} owns the task")
            task_row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            assert task_row is not None
            task = self._task_from_row(task_row)
            if task.kind != "oneoff":
                raise QueueError("only one-off work can complete retained recovery")
            contract_hash = _contract_hash(task)
            if attempt is not None and attempt["contract_hash"] != contract_hash:
                raise QueueError("source contract changed; completion CAS refused")
            admitted, admission_reason = recovery_admission(
                connection, task_id, now_epoch=now,
            )
            if not admitted:
                raise QueueError(admission_reason)
            recovery = connection.execute(
                "SELECT * FROM task_recovery WHERE task_id=?", (task_id,),
            ).fetchone()
            source_attempt = attempt["id"] if attempt is not None else None
            source_legacy = legacy["rowid_pk"] if legacy is not None else None
            if recovery is not None and (
                recovery["state"] not in {"scheduled", "backoff"}
                or recovery["after_attempt_id"] != source_attempt
                or recovery["after_legacy_run_rowid"] != source_legacy
                or recovery["contract_hash"] != contract_hash
            ):
                reason = recovery["reason_code"]
                raise QueueError(f"recovery is held or changed: {reason}")
            try:
                dependency_matches = self._dependency_snapshot_matches(
                    connection, task, dependency_snapshot,
                )
            except QueueError as exc:
                raise QueueError("dependency evidence changed during completion") from exc
            if not dependency_matches:
                raise QueueError("dependency evidence changed during completion")
            ordinal = int(connection.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM task_attempts WHERE task_id=?",
                (task_id,),
            ).fetchone()[0])
            attempt_id = uuid.uuid4().hex
            reason = verified["reason"]
            connection.execute(
                """INSERT INTO task_attempts(
                     id,task_id,ordinal,eligibility_key,mode,origin,state,recovery_of,
                     recovery_of_legacy_run_rowid,contract_hash,reason_code,reason_signature,
                     outcome_json,created_at,terminal_at
                   ) VALUES(?,?,?,?,'verification','continuation','done',?,?,?,?,?,?,?,?)""",
                (
                    attempt_id, task_id, ordinal,
                    attempt["eligibility_key"] if attempt is not None else legacy["eligibility_key"],
                    source_attempt, source_legacy, contract_hash, reason["code"],
                    reason["signature"], canonical, stamp, stamp,
                ),
            )
            source_run = connection.execute(
                "SELECT * FROM runs WHERE " + (
                    "attempt_id=?" if source_attempt is not None else "rowid_pk=?"
                ) + " ORDER BY rowid_pk DESC LIMIT 1",
                (source_attempt if source_attempt is not None else source_legacy,),
            ).fetchone()
            eligibility_key = (
                attempt["eligibility_key"] if attempt is not None else legacy["eligibility_key"]
            )
            cursor = connection.execute(
                """INSERT INTO runs(
                     task,kind,cycle,eligibility_key,status,ts,received_at,branch,summary,engine,
                     provider_id,account_id,router_job_id,trigger,attempt_id,outcome_json
                   ) VALUES(?,?,?,?,'done',?,?,?,?,?,?,?,NULL,'continuation',?,?)""",
                (
                    task_id, task.kind,
                    int(source_run["cycle"]) if source_run is not None else cycle_from_key(eligibility_key),
                    eligibility_key, stamp, stamp,
                    source_run["branch"] if source_run is not None else None,
                    summary,
                    source_run["engine"] if source_run is not None else None,
                    source_run["provider_id"] if source_run is not None else None,
                    source_run["account_id"] if source_run is not None else None,
                    attempt_id, canonical,
                ),
            )
            if recovery is not None:
                deleted = connection.execute(
                    """DELETE FROM task_recovery WHERE task_id=?
                         AND state IN ('scheduled','backoff') AND contract_hash=?
                         AND after_attempt_id IS ? AND after_legacy_run_rowid IS ?""",
                    (task_id, contract_hash, source_attempt, source_legacy),
                )
                if deleted.rowcount != 1:
                    raise QueueError("completion lost the recovery projection CAS")
            row = connection.execute(
                "SELECT * FROM runs WHERE rowid_pk=?", (cursor.lastrowid,),
            ).fetchone()
            assert row is not None
            return self._run_from_row(row)

    def requeue(
        self,
        task_id: str,
        eligibility_key: str | None = None,
        *,
        attempt_id: str | None = None,
        mode: Literal["retry", "verification"] | None = None,
        now_epoch: float | None = None,
    ) -> RecoveryDecision:
        from .goals import guard_history
        _require_task_id(task_id)
        now = float(time.time() if now_epoch is None else now_epoch)
        self.initialize()
        with self._transaction() as connection:
            guard_history(connection, task_id)
            if eligibility_key is not None:
                last = connection.execute(
                    """SELECT * FROM runs WHERE task=?
                         AND (eligibility_key=? OR (attempt_id IS NULL AND eligibility_key IS NULL AND cycle=?))
                         ORDER BY rowid_pk DESC LIMIT 1""",
                    (task_id, eligibility_key, cycle_from_key(eligibility_key)),
                ).fetchone()
                if last is None:
                    raise QueueError(f"{task_id} has no retryable terminal event")
                if last["attempt_id"] is not None:
                    inferred_attempt, inferred_legacy = last["attempt_id"], None
                else:
                    inferred_attempt, inferred_legacy = None, last["rowid_pk"]
            else:
                latest = self._latest_effective_attempt(connection, task_id)
                legacy = self._latest_legacy_run(connection, task_id) if latest is None else None
                inferred_attempt = latest["id"] if latest is not None else None
                inferred_legacy = legacy["rowid_pk"] if legacy is not None else None
            if attempt_id is not None and inferred_attempt != attempt_id:
                raise QueueError("operator recovery source attempt is stale")
            count = self._blocked_descendant_counts(connection).get(task_id, 0)
            return self._request_recovery_in_connection(
                connection, task_id,
                expected_attempt_id=inferred_attempt,
                expected_legacy_run_rowid=inferred_legacy,
                mode=mode, source="operator", now_epoch=now,
                blocked_descendants=count,
            )

    def contract_tasks(self, task_id: str, title: str) -> list[Task]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE id=? OR title=? ORDER BY id", (task_id, title),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def set_priority(self, task_id: str, priority: int) -> None:
        if priority not in range(5):
            raise QueueError("priority must be from 0 through 4")
        self._update_task(task_id, "priority", priority)

    def set_size(self, task_id: str, size: str, cycle: int) -> Task:
        task_id = _require_task_id(task_id)
        canonical_size = require_task_size(size)
        if not isinstance(cycle, int) or isinstance(cycle, bool):
            raise QueueError("cycle must be an integer")
        self.initialize()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise QueueError(f"no such task: {task_id}")
            task = self._task_from_row(row)
            if not self._eligible_in_connection(connection, task, cycle):
                raise QueueError(f"task is not upcoming in cycle {cycle}: {task_id}")
            connection.execute(
                "UPDATE tasks SET size=? WHERE id=?",
                (canonical_size, task_id),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        assert row is not None
        return self._task_from_row(row)

    def set_model(self, task_id: str, model: str | None) -> None:
        self._update_task(task_id, "model", canonical_model(model))

    def set_mcp(self, task_id: str, mcp: str | None) -> None:
        canonical = mcp.strip() if mcp is not None else None
        if mcp is not None and not canonical:
            raise QueueError("mcp must be a non-empty selection or omitted")
        self._update_task(task_id, "mcp", canonical)

    def set_providers(self, task_id: str, providers: Iterable[str]) -> None:
        canonical = tuple(str(provider).strip() for provider in providers if str(provider).strip())
        if not canonical:
            raise QueueError("providers must contain at least one provider id")
        if len(canonical) != len(set(canonical)):
            raise QueueError("providers must not contain duplicates")
        if any(not TASK_ID_RE.fullmatch(provider) for provider in canonical):
            raise QueueError("providers contains an invalid provider id")
        self._update_task(task_id, "allowed_providers_json", json.dumps(canonical))

    def set_active(self, task_id: str, active: bool) -> None:
        self._update_task(task_id, "active", int(active))

    def _update_task(self, task_id: str, column: str, value: Any) -> None:
        from .goals import guard_contract_edit
        _require_task_id(task_id)
        if column not in {"priority", "model", "mcp", "allowed_providers_json", "active"}:
            raise QueueError("unsafe task update")
        self.initialize()
        with self._transaction() as connection:
            guard_contract_edit(connection, task_id, {column})
            cursor = connection.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, task_id))
            if cursor.rowcount != 1:
                raise QueueError(f"no such task: {task_id}")

    def snapshot(
        self, *, cycle: int = 0, run_limit: int = 50,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        now = float(time.time() if now_epoch is None else now_epoch)
        tasks = self.tasks()
        return {
            "database": str(self.path), "cycle": int(cycle),
            "tasks": [task.to_dict() for task in tasks],
            "claims": [claim.to_dict() for claim in self.claims()],
            "activation_leases": [lease.to_dict() for lease in self.activation_leases()],
            "runs": [event.to_dict() for event in self.runs(limit=run_limit)],
            "attempts": [attempt.to_dict() for attempt in self.attempts()],
            "recoveries": [recovery.to_dict() for recovery in self.recoveries()],
            "readiness": {
                task.id: self.readiness(task.id, now_epoch=now) for task in tasks
            },
        }


def doctor(queue: QueueDB) -> DoctorReport:
    """Report claims that cannot be reconciled automatically."""

    try:
        queue.initialize()
        ambiguous_claims = tuple(queue.claims(state="ambiguous"))
        ambiguous = tuple(claim.task_id for claim in ambiguous_claims)
        leases = queue.activation_leases()
        dispatched = {
            (event.task, event.eligibility_key, event.attempt_id)
            for event in queue.inflight()
            if event.eligibility_key is not None
        }
        incomplete_leases = tuple(lease for lease in leases if lease.state != "active")
        active_orphan_leases = tuple(
            lease for lease in leases
            if lease.state == "active"
            and (lease.task_id, lease.eligibility_key, lease.attempt_id) not in dispatched
        )
    except (OSError, sqlite3.Error, QueueError) as exc:
        return DoctorReport(False, (), (f"database unavailable: {exc}",))
    incomplete = tuple(lease.task_id for lease in incomplete_leases)
    active_orphan = tuple(lease.task_id for lease in active_orphan_leases)
    reconciliation = tuple(dict.fromkeys((*ambiguous, *incomplete, *active_orphan)))
    provider_holds = tuple(dict.fromkeys((
        *(claim.provider_id for claim in ambiguous_claims),
        *(lease.provider_id for lease in incomplete_leases),
        *(lease.provider_id for lease in active_orphan_leases),
    )))
    diagnostics = (
        "ambiguous router outcomes or incomplete activation transitions require explicit reconciliation",
    ) if reconciliation else ()
    return DoctorReport(
        not reconciliation, reconciliation, diagnostics, provider_holds,
    )
