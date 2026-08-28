"""SQLite queue, run log, and atomic dispatch claims."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping


class QueueError(RuntimeError):
    """Queue operation failed without changing its requested invariant."""


VALID_STATUSES = frozenset({"dispatched", "done", "skipped", "failed"})
TERMINAL_STATUSES = frozenset({"done", "skipped", "failed"})
LEGACY_EXCLUSIVE_CAPABILITY = "legacy-exclusive"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TASK_SIZES = ("tiny", "small", "medium", "large", "huge")
RECURRING_COOLDOWNS_SECONDS = {
    "weekly": 4 * 24 * 60 * 60,
    "monthly": 28 * 24 * 60 * 60,
}


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


def legacy_exclusive_model(model: str | None) -> bool:
    """Compatibility classification; the generic planner never calls this helper."""

    if not model:
        return False
    return model.startswith("claude-") or model in {"opus", "sonnet", "haiku", "fable"} or "[1m]" in model


def task_requires_legacy_exclusive(task: "Task") -> bool:
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    reconciliation_required: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reconciliation_required": list(self.reconciliation_required),
            "diagnostics": list(self.diagnostics),
        }


class QueueDB:
    """Short-lived-connection SQLite access layer with atomic claim transitions."""

    def __init__(self, path: str | os.PathLike[str] | Path, *, timeout_seconds: float = 5.0):
        self.path = Path(path).expanduser().resolve(strict=False)
        self.timeout_seconds = timeout_seconds

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
        }
        run_columns = {
            "engine": "TEXT",
            "router_job_id": "TEXT",
            "eligibility_key": "TEXT",
            "provider_id": "TEXT",
            "account_id": "TEXT",
        }
        for table, columns in (("tasks", task_columns), ("runs", run_columns)):
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
              PRIMARY KEY(task_id, eligibility_key),
              FOREIGN KEY(task_id, eligibility_key)
                REFERENCES dispatch_claims(task_id, eligibility_key) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_eligibility ON runs(task, eligibility_key);
            CREATE INDEX IF NOT EXISTS idx_claims_task ON dispatch_claims(task_id);
            CREATE INDEX IF NOT EXISTS idx_activation_provider_account
              ON activation_leases(provider_id, account_id);
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
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunEvent:
        provider_id = row["provider_id"] or row["engine"]
        return RunEvent(
            rowid_pk=int(row["rowid_pk"]), task=row["task"], kind=row["kind"], cycle=int(row["cycle"]),
            eligibility_key=row["eligibility_key"], status=row["status"], ts=row["ts"],
            branch=row["branch"], summary=row["summary"], provider_id=provider_id,
            account_id=row["account_id"], router_job_id=row["router_job_id"], engine=row["engine"],
        )

    @staticmethod
    def _claim_from_row(row: sqlite3.Row) -> Claim:
        return Claim(**dict(row))

    def add_task(self, values: Mapping[str, Any]) -> Task:
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
            "model": values.get("model"), "mcp": values.get("mcp"),
            "use_implement": int(bool(values.get("use_implement", False))),
            "allowed": json.dumps(list(allowed)) if allowed else None,
            "required": json.dumps(list(required)) if required else None,
            "size": size,
        }
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO tasks(
                      id,title,kind,priority,cadence,cwd,goal,context,constraints,
                      precondition,done_when,created_at,active,claude_only,model,mcp,
                      use_implement,allowed_providers_json,required_capabilities_json,size
                    ) VALUES(
                      :id,:title,:kind,:priority,:cadence,:cwd,:goal,:context,:constraints,
                      :precondition,:done_when,:created_at,:active,:claude_only,:model,:mcp,
                      :use_implement,:allowed,:required,:size
                    )
                    """, parameters,
                )
        except sqlite3.IntegrityError as exc:
            raise QueueError(f"task insert rejected for {item_id}: {exc}") from exc
        task = self.task(item_id)
        assert task is not None
        return task

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

    @staticmethod
    def _eligible_in_connection(connection: sqlite3.Connection, task: Task, cycle: int) -> bool:
        if not task.active:
            return False
        if connection.execute("SELECT 1 FROM dispatch_claims WHERE task_id=? LIMIT 1", (task.id,)).fetchone():
            return False
        if task.kind == "oneoff":
            return connection.execute("SELECT 1 FROM runs WHERE task=? LIMIT 1", (task.id,)).fetchone() is None
        cooldown = RECURRING_COOLDOWNS_SECONDS.get(task.cadence or "")
        if cooldown is None:
            raise QueueError(f"unsupported recurring cadence: {task.cadence}")
        row = connection.execute(
            "SELECT ts FROM runs WHERE task=? ORDER BY rowid_pk DESC LIMIT 1",
            (task.id,),
        ).fetchone()
        if row is None:
            return True
        try:
            last_run = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
        except ValueError:
            # A malformed run timestamp must not make a recurring job eligible early.
            return False
        return time.time() - last_run.timestamp() >= cooldown

    def eligible_tasks(
        self, cycle: int, *, provider_id: str | None = None, capabilities: Iterable[str] = (),
        portable_only: bool = False, exclusive_only: bool = False, task_id: str | None = None,
        claude_priority: bool = False, limit: int | None = None,
    ) -> list[Task]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tasks WHERE active=1").fetchall()
            candidates: list[Task] = []
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
                if self._eligible_in_connection(connection, task, int(cycle)):
                    candidates.append(task)
        if claude_priority:
            candidates.sort(key=lambda task: (
                not task_requires_legacy_exclusive(task), task.priority,
                task.kind == "recurring", task.created_at, task.id,
            ))
        else:
            candidates.sort(key=lambda task: (task.priority, task.kind == "recurring", task.created_at, task.id))
        return candidates if limit is None else candidates[: max(0, int(limit))]

    def count_eligible(self, cycle: int, **kwargs: Any) -> int:
        return len(self.eligible_tasks(cycle, **kwargs))

    def claim(
        self, task_id: str, eligibility_key: str, provider_id: str, account_id: str | None,
        *, provider_capabilities: Iterable[str] = (),
    ) -> bool:
        if not eligibility_key or not provider_id or provider_id == "auto":
            return False
        self.initialize()
        cycle = cycle_from_key(eligibility_key)
        try:
            with self._transaction() as connection:
                row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if row is None:
                    return False
                task = self._task_from_row(row)
                if not self._provider_compatible(task, provider_id, provider_capabilities):
                    return False
                if not self._eligible_in_connection(connection, task, cycle):
                    return False
                connection.execute(
                    """INSERT INTO dispatch_claims(
                         task_id,eligibility_key,provider_id,account_id,state,claimed_at,detail
                       ) VALUES(?,?,?,?, 'claimed', ?, NULL)""",
                    (task_id, eligibility_key, provider_id, account_id, utc_now()),
                )
                return True
        except sqlite3.IntegrityError:
            return False

    def acquire_activation(
        self,
        task_id: str,
        eligibility_key: str,
        provider_id: str,
        account_id: str,
        activate: Callable[[], None],
    ) -> bool:
        """Acquire one durable claim-scoped activation lease with a two-phase transition."""

        self.initialize()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            wait_for_activation = False
            try:
                with self._transaction() as connection:
                    claim = connection.execute(
                        """SELECT provider_id,account_id FROM dispatch_claims
                             WHERE task_id=? AND eligibility_key=?""",
                        (task_id, eligibility_key),
                    ).fetchone()
                    if claim is None or claim["provider_id"] != provider_id or claim["account_id"] != account_id:
                        raise QueueError("activation lease requires the matching dispatch claim")
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
                                 task_id,eligibility_key,provider_id,account_id,state,acquired_at
                               ) VALUES(?,?,?,?,?,?)""",
                            (task_id, eligibility_key, provider_id, account_id, state, utc_now()),
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
                     WHERE task_id=? AND eligibility_key=? AND state='activating'""",
                (task_id, eligibility_key),
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

    def release_claim(self, task_id: str, eligibility_key: str, *, reason: str | None = None) -> bool:
        del reason
        self.initialize()
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

    def mark_ambiguous(self, task_id: str, eligibility_key: str, *, detail: str) -> None:
        self.initialize()
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE dispatch_claims SET state='ambiguous', detail=? WHERE task_id=? AND eligibility_key=?",
                (detail[:1000], task_id, eligibility_key),
            )
            if cursor.rowcount != 1:
                raise QueueError(f"cannot mark missing claim ambiguous: {task_id}")

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
        self, task_id: str, eligibility_key: str | None = None, *, status: str,
        provider_id: str | None = None, account_id: str | None = None, kind: str | None = None,
        cycle: int | None = None, ts: str | None = None, branch: str | None = None,
        summary: str | None = None, router_job_id: str | None = None,
        timestamp: str | None = None,
        release_activation: Callable[[], None] | None = None,
    ) -> RunEvent:
        if status not in VALID_STATUSES:
            raise QueueError(f"invalid run status: {status}")
        if provider_id == "auto":
            raise QueueError("run provider_id must be concrete, never auto")
        self.initialize()
        resolved_cycle = int(cycle if cycle is not None else cycle_from_key(eligibility_key))
        resolved_kind: str | None = None
        needs_release = False

        def resolve(connection: sqlite3.Connection) -> None:
            nonlocal resolved_kind, provider_id, account_id
            task_row = connection.execute("SELECT kind FROM tasks WHERE id=?", (task_id,)).fetchone()
            resolved_kind = kind or (task_row["kind"] if task_row else None)
            if resolved_kind not in {"oneoff", "recurring"}:
                raise QueueError("record kind must be oneoff or recurring")
            if eligibility_key:
                claim_row = connection.execute(
                    """SELECT provider_id,account_id FROM dispatch_claims
                         WHERE task_id=? AND eligibility_key=?""",
                    (task_id, eligibility_key),
                ).fetchone()
                if provider_id is None and claim_row:
                    provider_id = claim_row["provider_id"]
                    account_id = account_id or claim_row["account_id"]

        def finalize(connection: sqlite3.Connection, *, lease_state: str | None = None) -> sqlite3.Row:
            if lease_state is not None:
                cursor = connection.execute(
                    """DELETE FROM activation_leases
                         WHERE task_id=? AND eligibility_key=? AND state=?""",
                    (task_id, eligibility_key, lease_state),
                )
                if cursor.rowcount != 1:
                    raise QueueError("activation release transition requires reconciliation")
            cursor = connection.execute(
                    """INSERT INTO runs(
                         task,kind,cycle,eligibility_key,status,ts,branch,summary,engine,
                         provider_id,account_id,router_job_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, resolved_kind, resolved_cycle, eligibility_key, status, ts or timestamp or utc_now(),
                     branch, summary, provider_id, provider_id, account_id, router_job_id),
                )
            if status in TERMINAL_STATUSES:
                if eligibility_key is None:
                    if connection.execute(
                        "SELECT 1 FROM activation_leases WHERE task_id=?", (task_id,),
                    ).fetchone():
                        raise QueueError("terminal lease release requires an eligibility key")
                    connection.execute("DELETE FROM dispatch_claims WHERE task_id=?", (task_id,))
                else:
                    connection.execute(
                        "DELETE FROM dispatch_claims WHERE task_id=? AND eligibility_key=?", (task_id, eligibility_key),
                    )
            result = connection.execute("SELECT * FROM runs WHERE rowid_pk=?", (cursor.lastrowid,)).fetchone()
            assert result is not None
            return result

        with self._transaction() as connection:
            resolve(connection)
            lease = None
            if status in TERMINAL_STATUSES and eligibility_key:
                lease = connection.execute(
                    """SELECT provider_id,account_id,state FROM activation_leases
                         WHERE task_id=? AND eligibility_key=?""",
                    (task_id, eligibility_key),
                ).fetchone()
            if lease:
                holders = int(connection.execute(
                    """SELECT COUNT(*) FROM activation_leases
                         WHERE provider_id=? AND account_id=?""",
                    (lease["provider_id"], lease["account_id"]),
                ).fetchone()[0])
                if holders == 1:
                    if release_activation is None:
                        raise QueueError("last activation lease requires a verified release callback")
                    if lease["state"] != "active":
                        raise QueueError("incomplete activation lease requires reconciliation")
                    cursor = connection.execute(
                        """UPDATE activation_leases SET state='releasing'
                             WHERE task_id=? AND eligibility_key=? AND state='active'""",
                        (task_id, eligibility_key),
                    )
                    if cursor.rowcount != 1:
                        raise QueueError("activation release transition requires reconciliation")
                    needs_release = True
                    row = None
                else:
                    if lease["state"] != "active":
                        raise QueueError("incomplete activation lease requires reconciliation")
                    row = finalize(connection, lease_state="active")
            else:
                row = finalize(connection)

        if needs_release:
            assert release_activation is not None
            release_activation()
            with self._transaction() as connection:
                resolve(connection)
                row = finalize(connection, lease_state="releasing")
        assert row is not None
        return self._run_from_row(row)

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
        return [self._run_from_row(row) for row in rows]

    def inflight(self, *, provider_id: str | None = None, include_legacy: bool = False) -> list[RunEvent]:
        events = self.runs()
        terminal_after: set[str] = set()
        result: list[RunEvent] = []
        for event in events:
            if event.status in TERMINAL_STATUSES:
                terminal_after.add(event.task)
                continue
            if event.status != "dispatched" or event.task in terminal_after:
                continue
            effective = event.provider_id
            if provider_id is not None and effective != provider_id:
                if not (include_legacy and effective is None):
                    continue
            result.append(event)
        return result

    def requeue(self, task_id: str, eligibility_key: str | None = None) -> bool:
        _require_task_id(task_id)
        self.initialize()
        with self._transaction() as connection:
            task_row = connection.execute("SELECT kind FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task_row is None:
                raise QueueError(f"no such task: {task_id}")
            query = "SELECT * FROM runs WHERE task=?"
            parameters: list[Any] = [task_id]
            if eligibility_key is not None:
                query += " AND (eligibility_key=? OR (eligibility_key IS NULL AND cycle=?))"
                parameters.extend([eligibility_key, cycle_from_key(eligibility_key)])
            query += " ORDER BY rowid_pk DESC LIMIT 1"
            last = connection.execute(query, parameters).fetchone()
            if last and last["status"] == "done":
                raise QueueError(f"{task_id} last ran done; refusing to retry completed work")
            if last and last["status"] == "dispatched":
                raise QueueError(f"{task_id} is still dispatched and cannot be requeued")
            if last and last["status"] not in {"failed", "skipped"}:
                raise QueueError(f"{task_id} has no retryable terminal event")
            if eligibility_key is not None:
                connection.execute(
                    "DELETE FROM runs WHERE task=? AND (eligibility_key=? OR (eligibility_key IS NULL AND cycle=?))",
                    (task_id, eligibility_key, cycle_from_key(eligibility_key)),
                )
                connection.execute(
                    "DELETE FROM dispatch_claims WHERE task_id=? AND eligibility_key=?", (task_id, eligibility_key),
                )
            elif task_row["kind"] == "oneoff":
                connection.execute("DELETE FROM runs WHERE task=?", (task_id,))
                connection.execute("DELETE FROM dispatch_claims WHERE task_id=?", (task_id,))
            elif last:
                cycle_value = int(last["cycle"])
                keys = [row[0] for row in connection.execute(
                    "SELECT eligibility_key FROM runs WHERE task=? AND cycle=? AND eligibility_key IS NOT NULL",
                    (task_id, cycle_value),
                )]
                connection.execute("DELETE FROM runs WHERE task=? AND cycle=?", (task_id, cycle_value))
                for key in keys:
                    connection.execute(
                        "DELETE FROM dispatch_claims WHERE task_id=? AND eligibility_key=?", (task_id, key),
                    )
            return bool(last) or connection.total_changes > 0

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
        self._update_task(task_id, "model", model or None)

    def set_mcp(self, task_id: str, mcp: str | None) -> None:
        canonical = mcp.strip() if mcp is not None else None
        if mcp is not None and not canonical:
            raise QueueError("mcp must be a non-empty selection or omitted")
        self._update_task(task_id, "mcp", canonical)

    def set_active(self, task_id: str, active: bool) -> None:
        self._update_task(task_id, "active", int(active))

    def _update_task(self, task_id: str, column: str, value: Any) -> None:
        _require_task_id(task_id)
        if column not in {"priority", "model", "mcp", "active"}:
            raise QueueError("unsafe task update")
        self.initialize()
        with self._transaction() as connection:
            cursor = connection.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, task_id))
            if cursor.rowcount != 1:
                raise QueueError(f"no such task: {task_id}")

    def snapshot(self, *, cycle: int = 0, run_limit: int = 50) -> dict[str, Any]:
        return {
            "database": str(self.path), "cycle": int(cycle),
            "tasks": [task.to_dict() for task in self.tasks()],
            "claims": [claim.to_dict() for claim in self.claims()],
            "activation_leases": [lease.to_dict() for lease in self.activation_leases()],
            "runs": [event.to_dict() for event in self.runs(limit=run_limit)],
        }


def doctor(queue: QueueDB) -> DoctorReport:
    """Report claims that cannot be reconciled automatically."""

    try:
        queue.initialize()
        ambiguous = tuple(claim.task_id for claim in queue.claims(state="ambiguous"))
        leases = queue.activation_leases()
        dispatched = {
            (event.task, event.eligibility_key)
            for event in queue.runs()
            if event.status == "dispatched" and event.eligibility_key is not None
        }
        incomplete = tuple(lease.task_id for lease in leases if lease.state != "active")
        active_orphan = tuple(
            lease.task_id for lease in leases
            if lease.state == "active"
            and (lease.task_id, lease.eligibility_key) not in dispatched
        )
    except (OSError, sqlite3.Error, QueueError) as exc:
        return DoctorReport(False, (), (f"database unavailable: {exc}",))
    reconciliation = tuple(dict.fromkeys((*ambiguous, *incomplete, *active_orphan)))
    diagnostics = (
        "ambiguous router outcomes or incomplete activation transitions require explicit reconciliation",
    ) if reconciliation else ()
    return DoctorReport(not reconciliation, reconciliation, diagnostics)
