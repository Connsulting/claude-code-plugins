"""Shared application service for one manual Bonus Drain kickoff."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from . import dispatcher
from .config import RuntimeConfig
from .db import QueueDB, hour_round
from .usage import read_accounts


def kick_task(
    config: RuntimeConfig,
    queue: QueueDB,
    task_id: str,
    requested_provider: str,
    eligibility_key: str | None = None,
    now_epoch: int | None = None,
    account_id: str | None = None,
    *,
    router_call: Callable[..., Any] | None = None,
) -> dispatcher.DispatchResult:
    """Dispatch one manual task without retrying or applying pacing gates.

    ``router_call`` is a dependency-injection seam for tests. Runtime
    callers leave them unset so every job launch follows the configured dispatcher path.
    """

    now = int(time.time()) if now_epoch is None else int(now_epoch)
    if account_id is not None and requested_provider == "auto":
        raise dispatcher.InvalidRoute("--account requires a concrete provider")
    if account_id is not None and eligibility_key is not None:
        provider = dispatcher._provider(config, requested_provider)
        configured_ids = {item.id for item in tuple(item for item in read_accounts(config, now_epoch=now) if item.provider_id == provider.id)}
        if account_id not in configured_ids:
            raise dispatcher.InvalidRoute(
                f"account {account_id} does not belong to provider {provider.id}"
            )
        eligibility_account = eligibility_key.split("/", 1)[0]
        if eligibility_account != account_id:
            raise dispatcher.InvalidRoute("explicit account and eligibility key disagree")
    if eligibility_key is None and requested_provider == "auto":
        # Classification happens inside dispatcher; retain the provider-neutral key until then.
        key = f"manual/manual/{hour_round(now + 604800)}"
    elif eligibility_key is None:
        provider = dispatcher._provider(config, requested_provider)
        accounts = tuple(item for item in read_accounts(config, now_epoch=now) if item.provider_id == provider.id)
        if account_id is not None:
            if not any(account.id == account_id for account in accounts):
                raise dispatcher.InvalidRoute(
                    f"account {account_id} does not belong to provider {provider.id}"
                )
            account = account_id
        else:
            account = accounts[0].id if accounts else provider.id
        key = f"{account}/manual/{hour_round(now + 604800)}"
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
