"""Best effort scout health notices with durable SQLite deduplication."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence
from urllib.request import Request, urlopen

from .config import RuntimeConfig
from .db import QueueDB, TASK_ID_RE


def _task_ids(candidates: Iterable[Any]) -> list[str]:
    """Sorted unique task identifiers; only these structured values ever leave the host."""

    return sorted({task for task in candidates if isinstance(task, str) and TASK_ID_RE.fullmatch(task)})


def _post_ntfy(config: RuntimeConfig, message: str) -> None:
    request = Request(
        config.scout_ntfy_url, data=message.encode("utf-8"), method="POST",
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    with urlopen(request, timeout=15) as response:
        response.close()


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
        tasks = _task_ids(
            task for item in relevant for task in [item.get("task_id"), *item.get("tasks", [])]
        )
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
        _post_ntfy(config, message)
    except Exception:
        # The scout's dispatch decisions and exit status remain authoritative even when
        # SQLite is unavailable or ntfy rejects/times out the notification.
        pass


def blocker_notice(config: RuntimeConfig, queue: QueueDB, *, now_epoch: int) -> list[str]:
    """Notify Brian once about held work that has no checkable resume condition.

    Reservation precedes the send, so a failed transport never repeats a notice. Only
    task identifiers leave the host; blocker detail may contain sensitive text.
    """

    if not config.scout_ntfy_url:
        return []
    try:
        items = queue.reserve_blocker_notices(now_epoch=now_epoch)
    except Exception:
        return []
    task_ids = _task_ids(item.get("task_id") for item in items)
    if not task_ids:
        return []
    try:
        message = (
            f"Bonus Drain needs you on {len(task_ids)} held task(s): {', '.join(task_ids)}. "
            "Inspect: bonus-drain held-report --json."
        )
        _post_ntfy(config, message)
    except Exception:
        # The reservation is consumed either way, matching scout_health semantics.
        pass
    return task_ids
