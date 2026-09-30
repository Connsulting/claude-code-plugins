"""Best effort scout health notices with durable SQLite deduplication."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence
from urllib.request import Request, urlopen

from .config import RuntimeConfig
from .db import QueueDB, TASK_ID_RE


def scout_health(
    config: RuntimeConfig,
    queue: QueueDB,
    *,
    now_epoch: int,
    exit_status: int,
    errors: Sequence[Mapping[str, Any]] = (),
    blockers: Sequence[Mapping[str, Any]] = (),
    dry_run: bool = False,
    skipped: bool = False,
) -> None:
    """Never let notification storage or transport change the scout's result."""

    if not config.scout_ntfy_url or dry_run or skipped:
        return
    try:
        relevant = [*errors, *(item for item in blockers if item.get("kind") == "reconciliation_required")]
        stuck = exit_status != 0 or bool(relevant)
        # Only structured identifiers leave the host. Error messages, configuration,
        # adapter output, and exception text may contain credentials and are excluded.
        kinds = sorted({
            item["kind"] for item in relevant
            if isinstance(item.get("kind"), str) and re.fullmatch(r"[A-Za-z0-9_]+", item["kind"])
        })
        tasks = sorted({
            task for item in relevant
            for task in [item.get("task_id"), *item.get("tasks", [])]
            if isinstance(task, str) and TASK_ID_RE.fullmatch(task)
        })
        if stuck and not kinds:
            kinds = ["scout_failure"]
        notice = queue.reserve_scout_notice(
            stuck=stuck, kinds=kinds, tasks=tasks, now_epoch=now_epoch,
        )
        if notice is None:
            return
        state = "stuck" if notice["stuck"] else "recovered"
        kind_label = "Kinds" if notice["stuck"] else "Previous kinds"
        message = (
            f"Bonus Drain scout {state}. {kind_label}: {', '.join(notice['kinds'])}. "
            f"Tasks: {', '.join(notice['tasks']) or 'none reported'}. "
            "Inspect: bonus-drain doctor --json."
        )
        request = Request(
            config.scout_ntfy_url, data=message.encode("utf-8"), method="POST",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        with urlopen(request, timeout=15) as response:
            response.close()
    except Exception:
        # The scout's dispatch decisions and exit status remain authoritative even when
        # SQLite is unavailable or ntfy rejects/times out the notification.
        pass
