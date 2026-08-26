"""Shared application service for one manual Bonus Drain kickoff."""

from __future__ import annotations

import time
from typing import Any, Callable

from . import dispatcher
from .config import RuntimeConfig
from .db import QueueDB, hour_round


def kick_task(
    config: RuntimeConfig,
    queue: QueueDB,
    task_id: str,
    requested_provider: str,
    eligibility_key: str | None = None,
    now_epoch: int | None = None,
    *,
    router_call: Callable[..., Any] | None = None,
    activation_call: Callable[[str, str], Any] | None = None,
) -> dispatcher.DispatchResult:
    """Dispatch one manual task without retrying or applying pacing gates.

    ``router_call`` and ``activation_call`` are dependency-injection seams for tests. Runtime
    callers leave them unset so every job launch follows the configured dispatcher path.
    """

    now = int(time.time()) if now_epoch is None else int(now_epoch)
    key = eligibility_key or f"manual/manual/{hour_round(now + 604800)}"
    return dispatcher.dispatch(
        config,
        queue,
        task_id=task_id,
        eligibility_key=key,
        requested_provider=requested_provider,
        router_call=router_call,
        activation_call=activation_call,
    )
