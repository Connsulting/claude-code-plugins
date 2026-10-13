"""Shared application service for one manual Bonus Drain kickoff."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

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
) -> dispatcher.DispatchResult:
    """Dispatch one manual task without retrying or applying pacing gates.

    ``router_call`` is a dependency-injection seam for tests. Runtime
    callers leave them unset so every job launch follows the configured dispatcher path.
    """

    now = int(time.time()) if now_epoch is None else int(now_epoch)
    if eligibility_key is None and requested_provider == "auto":
        # The dispatcher replaces this neutral prefix after choosing the provider pool.
        key = f"manual/manual/{hour_round(now + 604800)}"
    elif eligibility_key is None:
        provider = dispatcher._provider(config, requested_provider)
        key = f"{provider.id}/manual/{hour_round(now + 604800)}"
    else:
        key = eligibility_key
    return dispatcher.dispatch(
        config,
        queue,
        task_id=task_id,
        eligibility_key=key,
        requested_provider=requested_provider,
        router_call=router_call,
        now_epoch=now,
    )
