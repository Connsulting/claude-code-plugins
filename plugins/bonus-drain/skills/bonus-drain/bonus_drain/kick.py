"""Shared application service for one manual Bonus Drain kickoff."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from . import dispatcher
from .config import RuntimeConfig
from .db import QueueDB, hour_round


def _adapter_option(argv: tuple[str, ...], option: str) -> str | None:
    try:
        index = argv.index(option)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def _manual_account_id(
    config: RuntimeConfig,
    queue: QueueDB,
    requested_provider: str,
    account_id: str | None,
) -> str | None:
    """Choose a concrete account for a manual launch without falling back to config order."""

    provider = dispatcher._provider(config, requested_provider)
    accounts = config.accounts_for_provider(provider.id)
    if account_id is not None:
        if not any(account.id == account_id for account in accounts):
            raise dispatcher.InvalidRoute(
                f"account {account_id} does not belong to provider {provider.id}"
            )
        return account_id

    leased = {lease.account_id for lease in queue.activation_leases(provider_id=provider.id)}
    if len(leased) == 1:
        return next(iter(leased))
    if len(leased) > 1:
        raise dispatcher.InvalidRoute(
            f"provider {provider.id} has conflicting activation leases; reconcile before manual dispatch"
        )

    active_accounts: list[str] = []
    for account in accounts:
        if account.activation_adapter_id is None:
            continue
        adapter = config.adapter(account.activation_adapter_id)
        active_path = _adapter_option(adapter.argv, "--active-path")
        label = _adapter_option(adapter.argv, "--label")
        if not active_path or not label:
            continue
        try:
            if Path(active_path).read_text(encoding="utf-8").strip() == label:
                active_accounts.append(account.id)
        except OSError:
            continue
    if len(active_accounts) == 1:
        return active_accounts[0]
    if len(accounts) == 1:
        return accounts[0].id
    raise dispatcher.InvalidRoute(
        f"manual dispatch for multi-account provider {provider.id} requires --account"
    )


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
    activation_call: Callable[[str, str], Any] | None = None,
) -> dispatcher.DispatchResult:
    """Dispatch one manual task without retrying or applying pacing gates.

    ``router_call`` and ``activation_call`` are dependency-injection seams for tests. Runtime
    callers leave them unset so every job launch follows the configured dispatcher path.
    """

    now = int(time.time()) if now_epoch is None else int(now_epoch)
    if eligibility_key is None and requested_provider == "auto":
        if account_id is not None:
            raise dispatcher.InvalidRoute("--account requires a concrete provider")
        # Classification happens inside dispatcher; retain the provider-neutral key until then.
        key = f"manual/manual/{hour_round(now + 604800)}"
    elif eligibility_key is None:
        account = _manual_account_id(config, queue, requested_provider, account_id)
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
        activation_call=activation_call,
    )
