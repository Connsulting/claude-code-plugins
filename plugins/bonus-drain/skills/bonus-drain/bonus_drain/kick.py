"""Shared application service for one manual Bonus Drain kickoff."""

from __future__ import annotations

import os
import stat
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


_ACTIVE_MARKER_LIMIT = 4096


def _read_active_marker(path: Path) -> str:
    if not path.is_absolute():
        raise dispatcher.InvalidRoute("active account marker path must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise dispatcher.InvalidRoute("active account marker is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise dispatcher.InvalidRoute("active account marker path contains a symlink")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise dispatcher.InvalidRoute("active account marker is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _ACTIVE_MARKER_LIMIT:
            raise dispatcher.InvalidRoute("active account marker must be a bounded regular file")
        raw = os.read(descriptor, _ACTIVE_MARKER_LIMIT + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _ACTIVE_MARKER_LIMIT:
        raise dispatcher.InvalidRoute("active account marker exceeds its size limit")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeError as exc:
        raise dispatcher.InvalidRoute("active account marker is not UTF-8") from exc
    if not value or any(character in value for character in "\r\n\x00"):
        raise dispatcher.InvalidRoute("active account marker must contain one label")
    return value


def active_marker_for_account(config: RuntimeConfig, account_id: str) -> str | None:
    account = config.account(account_id)
    if account.activation_adapter_id is None:
        return None
    adapter = config.adapter(account.activation_adapter_id)
    active_path = _adapter_option(adapter.argv, "--active-path")
    if not active_path:
        return None
    return _read_active_marker(Path(active_path))


def resolve_active_account_id(
    config: RuntimeConfig,
    queue: QueueDB,
    requested_provider: str,
) -> str:
    """Resolve one provider's configured active account without guessing."""

    provider = dispatcher._provider(config, requested_provider)
    accounts = config.accounts_for_provider(provider.id)
    if not accounts:
        raise dispatcher.InvalidRoute(f"provider {provider.id} has no configured accounts")
    if len(accounts) == 1:
        return accounts[0].id

    leases = queue.activation_leases(provider_id=provider.id)
    leased_account_ids = {lease.account_id for lease in leases}
    if len(leased_account_ids) > 1:
        raise dispatcher.InvalidRoute(
            f"provider {provider.id} has multiple conflicting activation leases"
        )

    configured_ids = {account.id for account in accounts}
    if leased_account_ids and not leased_account_ids <= configured_ids:
        raise dispatcher.InvalidRoute(
            f"provider {provider.id} activation lease names an unknown configured account"
        )

    labels_by_path: dict[Path, str] = {}
    active_accounts: list[str] = []
    for account in accounts:
        if account.activation_adapter_id is None:
            raise dispatcher.InvalidRoute(
                f"provider {provider.id} account {account.id} has no activation adapter"
            )
        adapter = config.adapter(account.activation_adapter_id)
        active_path = _adapter_option(adapter.argv, "--active-path")
        label = _adapter_option(adapter.argv, "--label")
        if not active_path or not label:
            raise dispatcher.InvalidRoute(
                f"provider {provider.id} account {account.id} lacks active account proof"
            )
        path = Path(active_path)
        if path not in labels_by_path:
            labels_by_path[path] = _read_active_marker(path)
        if labels_by_path[path] == label:
            active_accounts.append(account.id)

    if len(active_accounts) != 1:
        detail = "does not match a configured account" if not active_accounts else "matches multiple accounts"
        raise dispatcher.InvalidRoute(
            f"provider {provider.id} active account marker {detail}"
        )
    active_account_id = active_accounts[0]
    if leased_account_ids and active_account_id not in leased_account_ids:
        raise dispatcher.InvalidRoute(
            f"provider {provider.id} active account marker and activation lease disagree"
        )
    return active_account_id


def resolve_active_accounts(
    config: RuntimeConfig,
    queue: QueueDB,
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve every provider while containing identity failures to that provider."""

    active: dict[str, str] = {}
    failures: dict[str, str] = {}
    for provider in config.providers:
        try:
            active[provider.id] = resolve_active_account_id(config, queue, provider.id)
        except dispatcher.InvalidRoute as exc:
            failures[provider.id] = str(exc)
    return active, failures


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
    if account_id is not None and requested_provider == "auto":
        raise dispatcher.InvalidRoute("--account requires a concrete provider")
    if account_id is not None and eligibility_key is not None:
        provider = dispatcher._provider(config, requested_provider)
        configured_ids = {item.id for item in config.accounts_for_provider(provider.id)}
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
        accounts = config.accounts_for_provider(provider.id)
        if account_id is not None:
            if not any(account.id == account_id for account in accounts):
                raise dispatcher.InvalidRoute(
                    f"account {account_id} does not belong to provider {provider.id}"
                )
            account = account_id
        else:
            try:
                account = resolve_active_account_id(config, queue, provider.id)
            except dispatcher.InvalidRoute as exc:
                if "has no activation adapter" in str(exc):
                    raise dispatcher.InvalidRoute(
                        f"manual dispatch for multi-account provider {provider.id} requires "
                        "--account and configured active account proof"
                    ) from exc
                raise
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
        now_epoch=now,
    )
