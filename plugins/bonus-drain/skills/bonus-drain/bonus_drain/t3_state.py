"""Read-only T3 orchestration state.

Read the latest unheld run, parent liveness, and recursive subagent liveness in
one SQLite snapshot. This module does not shell out to another tool.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

BUSY_TIMEOUT_SECONDS = 2.0
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
_TERMINAL_RUN_SQL = ", ".join(f"'{status}'" for status in sorted(TERMINAL_RUN_STATUSES))

LATEST_RUN = (
    "SELECT r.run_id FROM orchestration_v2_projection_runs r "
    "WHERE r.thread_id = :thread_id "
    "AND NOT (r.status = 'queued' AND json_extract(r.payload_json, '$.queueHeld') IS 1) "
    "ORDER BY r.ordinal DESC, r.run_id DESC LIMIT 1"
)

LOOKUP_SQL = f"""
WITH RECURSIVE descendants(thread_id) AS (
  SELECT child.thread_id FROM orchestration_v2_projection_threads child
  WHERE json_extract(child.payload_json, '$.lineage.parentThreadId') = :thread_id
    AND json_extract(child.payload_json, '$.lineage.relationshipToParent') = 'subagent'
  UNION
  SELECT child.thread_id FROM orchestration_v2_projection_threads child
  JOIN descendants parent
    ON json_extract(child.payload_json, '$.lineage.parentThreadId') = parent.thread_id
  WHERE json_extract(child.payload_json, '$.lineage.relationshipToParent') = 'subagent'
)
SELECT latest.status,
       EXISTS (
         SELECT 1 FROM orchestration_v2_projection_runs a
         WHERE a.thread_id = t.thread_id
           AND a.status IN ('preparing', 'starting', 'running')
       ),
       COALESCE(
         (SELECT json_extract(s.payload_json, '$.lastError')
          FROM orchestration_v2_projection_provider_sessions s
          JOIN orchestration_v2_projection_provider_session_bindings b
            ON b.provider_session_id = s.provider_session_id
          WHERE b.thread_id = t.thread_id
            AND s.provider_instance_id IS t.provider_instance_id
          ORDER BY s.updated_at DESC, s.provider_session_id DESC LIMIT 1),
         (SELECT json_extract(i.payload_json, '$.failure.message')
          FROM orchestration_v2_projection_turn_items i
          WHERE latest.status = 'failed' AND i.thread_id = t.thread_id
            AND i.run_id = latest.run_id AND i.type = 'error' AND i.status = 'failed'
            AND i.node_id IS json_extract(latest.payload_json, '$.rootNodeId')
          ORDER BY i.updated_at DESC, i.ordinal DESC LIMIT 1)
       ),
       t.updated_at,
       latest.requested_at,
       json_extract(latest.payload_json, '$.startedAt'),
       latest.completed_at,
       COALESCE(
         (SELECT r.status FROM orchestration_v2_projection_runs r
          WHERE r.thread_id = t.thread_id
          ORDER BY r.ordinal DESC, r.run_id DESC LIMIT 1), ''
       ) NOT IN ({_TERMINAL_RUN_SQL}),
       EXISTS (
         SELECT 1 FROM descendants d
         WHERE EXISTS (
           SELECT 1 FROM orchestration_v2_projection_runs a
           WHERE a.thread_id = d.thread_id
             AND a.status IN ('preparing', 'starting', 'running')
         ) OR COALESCE(
           (SELECT r.status FROM orchestration_v2_projection_runs r
            WHERE r.thread_id = d.thread_id
            ORDER BY r.ordinal DESC, r.run_id DESC LIMIT 1), ''
         ) NOT IN ({_TERMINAL_RUN_SQL})
       )
FROM orchestration_v2_projection_threads t
LEFT JOIN orchestration_v2_projection_runs latest
  ON latest.run_id = ({LATEST_RUN})
WHERE t.thread_id = :thread_id AND t.deleted_at IS NULL
"""

ENGINE_BY_PROVIDER = {"claude": "claudeAgent"}


class T3StateError(RuntimeError):
    pass


@dataclass(frozen=True)
class T3Thread:
    run_status: str | None
    active_run: bool
    last_error: str | None
    last_activity_at: str | None
    completed_at: str | None
    unsettled_run: bool
    live_descendant: bool


def state_db_path(home: Path | None = None) -> Path:
    return (Path.home() if home is None else home) / ".t3" / "userdata" / "statev2.sqlite"


def t3_engine(provider_name: str) -> str:
    """Map a Bonus Drain provider id to the T3 provider instance id."""

    return ENGINE_BY_PROVIDER.get(provider_name, provider_name)


def lookup_thread(thread_id: str, path: Path | None = None) -> T3Thread | None:
    """Return one thread, or None when T3 has no undeleted row for it.

    A missing database or a query error raises T3StateError. The connection is
    read-only and does not create the file.
    """

    database = path if path is not None else state_db_path()
    if not database.is_file():
        raise T3StateError(f"T3 state database is missing: {database}")
    uri = f"file:{database}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
    except sqlite3.Error as exc:
        raise T3StateError(f"T3 state database could not be opened: {exc}") from exc
    try:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(LOOKUP_SQL, {"thread_id": thread_id}).fetchone()
    except sqlite3.Error as exc:
        raise T3StateError(f"T3 state query failed: {exc}") from exc
    finally:
        connection.close()
    if row is None:
        return None
    stamps = [item for item in row[3:7] if isinstance(item, str)]
    return T3Thread(
        run_status=row[0] if isinstance(row[0], str) else None,
        active_run=bool(row[1]),
        last_error=row[2] if isinstance(row[2], str) else None,
        last_activity_at=max(stamps) if stamps else None,
        completed_at=row[6] if isinstance(row[6], str) else None,
        unsettled_run=bool(row[7]),
        live_descendant=bool(row[8]),
    )
