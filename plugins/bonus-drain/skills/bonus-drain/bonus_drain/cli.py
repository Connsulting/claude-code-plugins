"""One stable CLI for queue, capacity, dispatch, and operational hooks."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import time
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from . import config as config_module
from . import db, dispatcher, planner, scout, usage
from .kick import kick_task


class CLIError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIError(message, exit_code=2)


def _plain(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _plain(value.to_dict())
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return _plain(value.as_dict())
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    return value


def _json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(_plain(value), sort_keys=True, separators=(",", ":")), file=stream)


def _now(args: argparse.Namespace) -> int:
    explicit = getattr(args, "now", None)
    if explicit is not None:
        return int(explicit)
    for name in ("BONUS_DRAIN_NOW", "BONUS_NOW_OVERRIDE"):
        value = os.environ.get(name)
        if value and value.lstrip("-").isdigit():
            return int(value)
    return int(time.time())


def _config_path(args: argparse.Namespace) -> Path | None:
    value = getattr(args, "config", None) or os.environ.get("BONUS_DRAIN_CONFIG")
    if value:
        return Path(value).expanduser()
    default = config_module.default_config_path()
    return default if default.is_file() else None


def _load_config(args: argparse.Namespace, *, graph_required: bool) -> config_module.RuntimeConfig:
    path = _config_path(args)
    if path is not None and path.is_file():
        cfg = config_module.load_config(path)
        configured_graph = True
    elif graph_required:
        raise CLIError(f"configuration is required: {path or config_module.default_config_path()}")
    else:
        cfg = config_module.minimal_config(database=getattr(args, "database", None))
        configured_graph = False
    database = getattr(args, "database", None)
    if database and configured_graph and graph_required:
        requested = Path(database).expanduser().resolve(strict=False)
        authoritative = cfg.database.expanduser().resolve(strict=False)
        if requested != authoritative:
            raise CLIError(
                "--database is deprecated for configured commands and must match "
                f"config database {authoritative}"
            )
    elif database and configured_graph:
        cfg = replace(cfg, database=Path(database).expanduser().resolve(strict=False))
    return cfg


def _queue(args: argparse.Namespace, *, graph_required: bool = False) -> tuple[config_module.RuntimeConfig, db.QueueDB]:
    cfg = _load_config(args, graph_required=graph_required)
    queue = db.QueueDB(cfg.database)
    return cfg, queue


def _cycle_for_plan(cfg: config_module.RuntimeConfig, snapshots: Mapping[Any, Any], now: int) -> int:
    resets: list[int] = []
    for account in cfg.accounts:
        snapshot = snapshots.get((account.provider_id, account.id))
        if snapshot is None:
            continue
        for reading in getattr(snapshot, "limits", {}).values():
            reset = reading.get("resets_at") if isinstance(reading, Mapping) else None
            if isinstance(reset, (int, float)) and not isinstance(reset, bool) and int(reset) > now:
                resets.append(int(reset))
    return db.hour_round(min(resets)) if resets else db.hour_round(now)


def _plan(args: argparse.Namespace) -> tuple[config_module.RuntimeConfig, db.QueueDB, planner.PlanResult]:
    cfg, queue = _queue(args, graph_required=True)
    queue.initialize()
    now = _now(args)
    snapshots = usage.read_all(cfg, now_epoch=now)
    cycle = _cycle_for_plan(cfg, snapshots, now)
    result = planner.build_plan(
        cfg, snapshots, eligible_count=queue.count_eligible(cycle), now_epoch=now,
    )
    return cfg, queue, result


def _task_values(args: argparse.Namespace) -> dict[str, Any]:
    def boolean(name: str) -> bool:
        raw = getattr(args, name)
        if raw not in (0, 1):
            raise CLIError(f"{name.replace('_', '-')} must be 0 or 1")
        return bool(raw)

    return {
        "id": args.id,
        "title": args.title,
        "kind": args.kind,
        "priority": args.priority,
        "size": args.size,
        "cadence": args.cadence,
        "cwd": args.cwd,
        "goal": args.goal,
        "context": args.context,
        "constraints": args.constraints,
        "precondition": args.precondition,
        "done_when": args.done_when,
        "claude_only": boolean("claude_only"),
        "model": args.model,
        "mcp": args.mcp,
        "use_implement": boolean("use_implement"),
        "allowed_providers": tuple(filter(None, (args.providers or "").split(","))),
        "required_capabilities": tuple(filter(None, (args.capabilities or "").split(","))),
    }


def _legacy_filter(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "portable_only": bool(getattr(args, "codex", False) or getattr(args, "grok", False)),
        "exclusive_only": bool(getattr(args, "claude_only_filter", False)),
        "claude_priority": bool(getattr(args, "claude_priority", False)),
    }


def _human_queue_status(queue: db.QueueDB, cycle: int) -> None:
    print(f"== Queue (cycle {cycle}) ==")
    eligible_ids = {task.id for task in queue.eligible_tasks(cycle)}
    for task in queue.tasks():
        state = "eligible" if task.id in eligible_ids else "spent/claimed"
        print(f"{task.id}\tP{task.priority}\t{task.kind}\t{state}\tactive={int(task.active)}")
    print("\n== Recent runs ==")
    for event in queue.runs(limit=10):
        print(f"{event.ts}\t{event.task}\t{event.status}\t{event.provider_id or 'legacy'}")


def _account_records(cfg: config_module.RuntimeConfig, args: argparse.Namespace) -> list[usage.UsageSnapshot]:
    now = _now(args)
    records = usage.read_all(cfg, now_epoch=now)
    result = []
    for account in cfg.accounts:
        if getattr(args, "provider", None) and account.provider_id != args.provider:
            continue
        if getattr(args, "account", None) and account.id != args.account:
            continue
        result.append(records[(account.provider_id, account.id)])
    return result


def _resolve_secret(cfg: config_module.RuntimeConfig, ref_id: str) -> str | None:
    for ref in cfg.secret_refs:
        if ref.id == ref_id:
            return config_module.resolve_secret(ref)
    return None


def _command(args: argparse.Namespace) -> int:
    command = args.command
    if command == "version":
        print(__version__)
        return 0
    if command == "init":
        _cfg, queue = _queue(args)
        queue.initialize()
        if args.json:
            _json({"ok": True, "database": str(queue.path)})
        return 0
    if command == "add":
        _cfg, queue = _queue(args)
        task = queue.add_task(_task_values(args))
        _json({"task": task.to_dict()}) if args.json else print(f"added: {task.id}")
        return 0
    if command in {"eligible", "count-eligible", "pick"}:
        _cfg, queue = _queue(args)
        filters = _legacy_filter(args)
        if command == "eligible":
            print(1 if queue.count_eligible(args.cycle, **filters) else 0)
        elif command == "count-eligible":
            print(queue.count_eligible(args.cycle, **filters))
        else:
            tasks = queue.eligible_tasks(
                args.cycle, task_id=args.id, limit=args.count, **filters,
            )
            _json([task.legacy_pick_dict() for task in tasks])
        return 0
    if command == "contract-task":
        _cfg, queue = _queue(args)
        _json([task.legacy_contract_dict() for task in queue.contract_tasks(args.id, args.title)])
        return 0
    if command == "record":
        cfg, queue = _queue(args)
        if args.eligibility_key is None and args.cycle is None:
            raise CLIError("record requires --eligibility-key or --cycle")
        key = args.eligibility_key or f"legacy/{args.cycle}"
        claim = queue.claim_for(args.task, key)
        account_id = args.account_id or (claim.account_id if claim else None)
        release_activation = None
        if args.status in db.TERMINAL_STATUSES and account_id:
            try:
                account = cfg.account(account_id)
            except config_module.ConfigError:
                account = None
            if account is not None and account.activation_adapter_id:
                release_activation = lambda: dispatcher._activation(
                    cfg, account, "release", None,
                )
        event = queue.record(
            args.task, key, status=args.status, provider_id=args.provider_id or args.engine,
            account_id=account_id, kind=args.kind, cycle=args.cycle, ts=args.ts,
            branch=args.branch, summary=args.summary, router_job_id=args.router_job_id,
            release_activation=release_activation,
        )
        _json({"run": event.to_dict()}) if args.json else print(f"recorded: {args.task} {args.status}")
        return 0
    if command == "inflight":
        _cfg, queue = _queue(args)
        provider = args.provider
        legacy = False
        if args.claude:
            provider, legacy = "claude", True
        elif args.codex:
            provider = "codex"
        events = queue.inflight(provider_id=provider, include_legacy=legacy)
        if args.json:
            _json({"runs": [event.to_dict() for event in events]})
        else:
            for event in events:
                print(event.task)
        return 0
    if command in {"queue", "queue-status"}:
        cfg, queue = _queue(args)
        cycle = args.cycle or 0
        if command == "queue" or args.json:
            snapshot = queue.snapshot(cycle=cycle, run_limit=args.run_limit)
            # `pick` has a historical shape that cannot express explicit provider
            # allowlists. Expose the graph-backed eligibility the scout actually uses.
            eligible_ids_by_provider = {
                provider.id: {
                    task.id for task in queue.eligible_tasks(
                        cycle, provider_id=provider.id, capabilities=provider.capabilities,
                    )
                }
                for provider in cfg.providers
            }
            eligible_task_ids = set().union(*eligible_ids_by_provider.values())
            tasks_by_id = {task.id: task for task in queue.tasks()}
            candidates = [tasks_by_id[task_id] for task_id in eligible_task_ids]
            candidates.sort(key=lambda task: (
                task.priority, task.kind == "recurring", task.created_at, task.id,
            ))
            snapshot["eligible_task_ids"] = [task.id for task in candidates]
            snapshot["eligible_provider_ids"] = {
                task.id: [
                    provider.id for provider in cfg.providers
                    if task.id in eligible_ids_by_provider[provider.id]
                ]
                for task in candidates
            }
            _json(snapshot)
        else:
            _human_queue_status(queue, cycle)
        return 0
    if command == "runs":
        _cfg, queue = _queue(args)
        records = [event.to_dict() for event in queue.runs(limit=args.limit, task_id=args.task)]
        _json({"runs": records}) if args.json else [print(f"{row['ts']}\t{row['task']}\t{row['status']}") for row in records]
        return 0
    if command == "requeue":
        _cfg, queue = _queue(args)
        changed = queue.requeue(args.task, args.eligibility_key)
        _json({"ok": True, "changed": changed, "task": args.task}) if args.json else print(f"requeued: {args.task}")
        return 0
    if command == "set-priority":
        _cfg, queue = _queue(args)
        queue.set_priority(args.task, args.priority)
        return 0
    if command == "set-size":
        _cfg, queue = _queue(args)
        task = queue.set_size(args.task, args.size, args.cycle)
        _json({"task": task.to_dict()}) if args.json else print(f"sized: {task.id} {task.size}")
        return 0
    if command == "set-model":
        _cfg, queue = _queue(args)
        queue.set_model(args.task, args.model)
        return 0
    if command == "set-mcp":
        _cfg, queue = _queue(args)
        queue.set_mcp(args.task, args.mcp)
        task = queue.task(args.task)
        assert task is not None
        _json({"task": task.to_dict()}) if args.json else print(f"mcp: {task.id} {task.mcp or 'default'}")
        return 0
    if command in {"activate", "deactivate"}:
        _cfg, queue = _queue(args)
        queue.set_active(args.task, command == "activate")
        if args.json:
            _json({"ok": True, "task": args.task, "active": command == "activate"})
        return 0
    if command == "gates":
        _cfg, _queue_obj, result = _plan(args)
        _json({"generated_at": result.generated_at, "gates": [gate.to_dict() for gate in result.gates]})
        return 0
    if command == "plan":
        _cfg, _queue_obj, result = _plan(args)
        _json(result.to_dict())
        return 0
    if command == "usage":
        cfg = _load_config(args, graph_required=True)
        records = _account_records(cfg, args)
        if args.legacy_line:
            if len(records) != 1:
                raise CLIError("legacy usage output requires exactly one selected account")
            print(usage.legacy_window_line(cfg, records[0]))
        elif args.legacy_json:
            if len(records) != 1:
                raise CLIError("legacy JSON usage output requires exactly one selected account")
            record = records[0]
            account = cfg.account(record.account_id)
            configured_limits = sorted(
                cfg.limits_for_plan(account.plan_id), key=lambda item: (item.window_seconds, item.id)
            )
            longest = configured_limits[-1] if configured_limits else None
            reading = record.limits.get(longest.id, {}) if longest else {}
            _json({
                "provider_id": record.provider_id,
                "account_id": record.account_id,
                "weekly_percent": reading.get("used_percent"),
                "weekly_reset": reading.get("resets_at"),
                "source_ts": record.captured_at or None,
                "fresh": record.fresh,
            })
        else:
            _json({"accounts": [record.to_dict() for record in records]})
        return 0
    if command == "refresh":
        cfg = _load_config(args, graph_required=True)
        records = usage.refresh_all(cfg, now_epoch=_now(args))
        _json({"accounts": [record.to_dict() for record in records.values()]})
        return 0 if all(record.fresh for record in records.values()) else 1
    if command == "scout":
        cfg, queue = _queue(args, graph_required=True)
        report = scout.run_once(
            cfg, queue, now_epoch=_now(args), dry_run=args.dry_run,
        )
        _json(report.to_dict())
        return 0 if not report.errors else 1
    if command in {"dispatch", "run-now"}:
        cfg, queue = _queue(args, graph_required=True)
        result = kick_task(
            cfg, queue, task_id=args.task, requested_provider=args.provider,
            eligibility_key=getattr(args, "eligibility_key", None), now_epoch=_now(args),
            account_id=getattr(args, "account", None),
        )
        _json({"dispatch": result.to_dict()}) if args.json else print(result.job_id)
        return 0
    if command == "render-prompt":
        cfg, queue = _queue(args, graph_required=True)
        task = queue.task(args.task)
        if task is None:
            raise CLIError(f"unknown task: {args.task}")
        account_id = args.account
        print(dispatcher.render_prompt(cfg, task, args.eligibility_key, args.provider, account_id))
        return 0
    if command == "render-prompt-json":
        cfg = _load_config(args, graph_required=False)
        try:
            raw_task = json.loads(args.task_json)
        except json.JSONDecodeError as exc:
            raise CLIError(f"task JSON is invalid: {exc}") from exc
        if not isinstance(raw_task, Mapping):
            raise CLIError("task JSON must be an object")
        task = db.Task(
            id=str(raw_task.get("id") or ""),
            title=str(raw_task.get("title") or raw_task.get("id") or ""),
            kind=str(raw_task.get("kind") or "oneoff"),
            priority=int(raw_task.get("priority", 2)),
            cadence=raw_task.get("cadence"),
            cwd=str(raw_task.get("cwd") or os.getcwd()),
            goal=str(raw_task.get("goal") or ""),
            context=raw_task.get("context"),
            constraints=raw_task.get("constraints"),
            precondition=raw_task.get("precondition"),
            done_when=raw_task.get("done_when"),
            created_at=str(raw_task.get("created_at") or ""),
            active=bool(raw_task.get("active", True)),
            claude_only=bool(raw_task.get("claude_only", False)),
            model=raw_task.get("model"),
            mcp=raw_task.get("mcp"),
            use_implement=bool(raw_task.get("use_implement", False)),
            allowed_providers=tuple(raw_task.get("allowed_providers") or ()),
            required_capabilities=tuple(raw_task.get("required_capabilities") or ()),
            size=(
                None
                if raw_task.get("size") is None
                else db.require_task_size(raw_task.get("size"))
            ),
        )
        if not task.id or task.kind not in {"oneoff", "recurring"}:
            raise CLIError("task JSON requires a valid id and kind")
        key = f"legacy/{args.cycle}"
        print(dispatcher.render_prompt(cfg, task, key, args.provider, args.account))
        return 0
    if command == "accounts":
        cfg = _load_config(args, graph_required=True)
        return _accounts_command(cfg, args)
    if command == "doctor":
        from . import lifecycle

        cfg, queue = _queue(args, graph_required=True)
        queue_report = db.doctor(queue)
        lifecycle_report = lifecycle.doctor(
            Path.home(), config_path=cfg.source_path,
        )
        diagnostics = list(queue_report.diagnostics) + list(lifecycle_report.diagnostics)
        executable_checks = {
            "record_command_absolute": bool(cfg.record_command and Path(cfg.record_command[0]).is_absolute()),
            "router_only": all(
                cfg.adapter(provider.dispatch.adapter_id).kind == "agent-router"
                for provider in cfg.providers
            ),
        }
        checks = dict(lifecycle_report.checks)
        checks.update(executable_checks)
        ok = queue_report.ok and lifecycle_report.ok and all(executable_checks.values())
        payload = {
            "ok": ok,
            "config": str(cfg.source_path) if cfg.source_path else None,
            "database": str(cfg.database),
            "checks": checks,
            "reconciliation_required": list(queue_report.reconciliation_required),
            "diagnostics": diagnostics,
        }
        _json(payload)
        return 0 if ok else 1
    if command == "install":
        from . import lifecycle
        result = lifecycle.install(args.source, args.home, version=args.install_version)
        _json(result) if args.json else print(result.wrapper)
        return 0
    if command == "uninstall":
        from . import lifecycle
        removed = lifecycle.uninstall(args.home)
        _json({"removed": [str(path) for path in removed]}) if args.json else [print(path) for path in removed]
        return 0
    if command == "status":
        from . import lifecycle
        result = lifecycle.status(args.home)
        _json(result) if args.json else print("installed" if result.ok else "not ready")
        return 0 if result.ok else 1
    if command == "viewer":
        cfg, queue = _queue(args, graph_required=True)
        del queue
        if cfg.source_path is not None:
            os.environ["BONUS_DRAIN_CONFIG"] = str(cfg.source_path)
        skill_root = Path(__file__).resolve().parents[1]
        os.environ["BONUS_SKILL_DIR"] = str(skill_root)
        server = skill_root / "services" / "jobs-viewer" / "server.py"
        previous_argv = sys.argv
        sys.argv = [str(server), "--host", str(cfg.viewer.get("bind", "127.0.0.1")), "--port", str(args.port)]
        try:
            runpy.run_path(str(server), run_name="__main__")
        finally:
            sys.argv = previous_argv
        return 0
    if command == "migrate":
        from . import cutover
        cfg = _load_config(args, graph_required=False)
        destination = args.destination or cfg.state_dir
        report = cutover.migrate(
            from_db=args.from_db, from_units=args.from_units,
            destination=destination, dry_run=args.dry_run,
        )
        _json(report) if args.json else [print(step) for step in report.steps]
        return 0
    if command == "import-legacy":
        from . import cutover
        _cfg, queue = _queue(args)
        queue.initialize()
        report = cutover.import_legacy(args.backlog, args.runs, queue)
        _json(report) if args.json else print(f"imported: tasks={report.tasks} runs={report.runs}")
        return 0
    raise CLIError(f"unknown subcommand: {command}")


def _accounts_command(cfg: config_module.RuntimeConfig, args: argparse.Namespace) -> int:
    action = args.accounts_command
    accounts = list(cfg.accounts)
    if action == "labels":
        for account in accounts:
            print(account.id)
        return 0
    if action == "count":
        print(len(accounts))
        return 0
    if action == "multi":
        return 0 if len(accounts) >= 2 else 1
    if action in {"cycle", "canonical-cycle"}:
        now = args.epoch if args.epoch is not None else _now(args)
        # Stable Saturday 22:00 UTC anchor, rounded to the hour.  It is bookkeeping only.
        period = 7 * 24 * 3600
        anchor = 2 * 24 * 3600 + 22 * 3600  # Unix epoch Thursday -> next Saturday 22:00.
        cycles = (now - anchor + period - 1) // period
        print(anchor + cycles * period)
        return 0
    if action == "usage":
        account = cfg.account(args.account)
        record = usage.read_all(cfg, now_epoch=_now(args))[(account.provider_id, account.id)]
        print(usage.legacy_window_line(cfg, record))
        return 0 if record.fresh else 1
    if action == "select":
        records = usage.read_all(cfg, now_epoch=_now(args))
        result = planner.build_plan(cfg, records, eligible_count=1, now_epoch=_now(args))
        if result.batches:
            batch = result.batches[0]
            print(f"{batch.account_id} {batch.resets_at}")
        return 0
    raise CLIError(f"unknown accounts subcommand: {action}")


def _add_common(subparser: argparse.ArgumentParser, *, database: bool = True) -> None:
    subparser.add_argument("--config")
    if database:
        subparser.add_argument("--database")


def _add_json(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--json", action="store_true")


def _add_filters(subparser: argparse.ArgumentParser) -> None:
    group = subparser.add_mutually_exclusive_group()
    group.add_argument("--codex", action="store_true")
    group.add_argument("--grok", action="store_true")
    group.add_argument("--claude-only", dest="claude_only_filter", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = JSONArgumentParser(prog="bonus-drain")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=JSONArgumentParser)

    sub.add_parser("version")
    init = sub.add_parser("init"); _add_common(init); _add_json(init)

    add = sub.add_parser("add"); _add_common(add); _add_json(add)
    add.add_argument("--id", required=True); add.add_argument("--title", required=True)
    add.add_argument("--kind", choices=("oneoff", "recurring"), required=True)
    add.add_argument("--priority", type=int, default=2)
    add.add_argument("--size", choices=db.TASK_SIZES, required=True); add.add_argument("--cadence")
    add.add_argument("--cwd", required=True); add.add_argument("--goal", required=True)
    add.add_argument("--context"); add.add_argument("--constraints"); add.add_argument("--precondition")
    add.add_argument("--done-when", dest="done_when"); add.add_argument("--claude-only", type=int, default=0)
    add.add_argument("--model"); add.add_argument("--mcp"); add.add_argument("--use-implement", type=int, default=0)
    add.add_argument("--providers"); add.add_argument("--capabilities")

    for name in ("eligible", "count-eligible"):
        item = sub.add_parser(name); _add_common(item); item.add_argument("cycle", type=int); _add_filters(item)
    pick = sub.add_parser("pick"); _add_common(pick); pick.add_argument("count", type=int); pick.add_argument("cycle", type=int)
    _add_filters(pick); pick.add_argument("--claude-priority", action="store_true"); pick.add_argument("--id")
    contract = sub.add_parser("contract-task"); _add_common(contract); contract.add_argument("--id", required=True); contract.add_argument("--title", required=True)

    record = sub.add_parser("record"); _add_common(record); _add_json(record)
    record.add_argument("--task", required=True); record.add_argument("--kind", choices=("oneoff", "recurring"))
    record.add_argument("--eligibility-key"); record.add_argument("--cycle", type=int)
    record.add_argument("--status", choices=sorted(db.VALID_STATUSES), required=True)
    record.add_argument("--provider-id"); record.add_argument("--engine"); record.add_argument("--account-id")
    record.add_argument("--router-job-id"); record.add_argument("--ts"); record.add_argument("--branch"); record.add_argument("--summary")

    inflight = sub.add_parser("inflight"); _add_common(inflight); _add_json(inflight)
    inflight.add_argument("--provider"); inflight.add_argument("--claude", action="store_true"); inflight.add_argument("--codex", action="store_true")
    for name in ("queue", "queue-status"):
        item = sub.add_parser(name); _add_common(item); _add_json(item); item.add_argument("cycle", type=int, nargs="?"); item.add_argument("--run-limit", type=int, default=50)
    runs = sub.add_parser("runs"); _add_common(runs); _add_json(runs); runs.add_argument("--limit", type=int, default=50); runs.add_argument("--task")
    requeue = sub.add_parser("requeue"); _add_common(requeue); _add_json(requeue); requeue.add_argument("task"); requeue.add_argument("--eligibility-key")
    priority = sub.add_parser("set-priority"); _add_common(priority); priority.add_argument("task"); priority.add_argument("priority", type=int)
    size = sub.add_parser("set-size"); _add_common(size); _add_json(size); size.add_argument("task"); size.add_argument("size", choices=db.TASK_SIZES); size.add_argument("--cycle", type=int, required=True)
    model = sub.add_parser("set-model"); _add_common(model); model.add_argument("task"); model.add_argument("model", nargs="?")
    mcp = sub.add_parser("set-mcp"); _add_common(mcp); _add_json(mcp); mcp.add_argument("task"); mcp.add_argument("mcp", nargs="?")
    for name in ("activate", "deactivate"):
        item = sub.add_parser(name); _add_common(item); _add_json(item); item.add_argument("task")

    for name in ("gates", "plan"):
        item = sub.add_parser(name); _add_common(item); _add_json(item); item.add_argument("--now", type=int)
    usage_parser = sub.add_parser("usage"); _add_common(usage_parser, database=False); _add_json(usage_parser)
    usage_parser.add_argument("--provider"); usage_parser.add_argument("--account"); usage_parser.add_argument("--now", type=int)
    usage_parser.add_argument("--legacy-line", action="store_true"); usage_parser.add_argument("--legacy-json", action="store_true")
    refresh = sub.add_parser("refresh"); _add_common(refresh, database=False); _add_json(refresh); refresh.add_argument("--now", type=int)
    scout_parser = sub.add_parser("scout"); _add_common(scout_parser); _add_json(scout_parser); scout_parser.add_argument("--now", type=int); scout_parser.add_argument("--dry-run", action="store_true")

    for name in ("dispatch", "run-now"):
        item = sub.add_parser(name); _add_common(item); _add_json(item); item.add_argument("task"); item.add_argument("provider", nargs="?", default="auto"); item.add_argument("--account"); item.add_argument("--eligibility-key"); item.add_argument("--now", type=int)
    render = sub.add_parser("render-prompt"); _add_common(render); render.add_argument("task"); render.add_argument("--provider", required=True); render.add_argument("--account"); render.add_argument("--eligibility-key", required=True)
    render_json = sub.add_parser("render-prompt-json"); _add_common(render_json); render_json.add_argument("--task-json", required=True); render_json.add_argument("--cycle", type=int, required=True); render_json.add_argument("--provider", required=True); render_json.add_argument("--account")

    accounts = sub.add_parser("accounts"); _add_common(accounts, database=False); accounts.add_argument("accounts_command", choices=("labels", "count", "multi", "cycle", "canonical-cycle", "usage", "select")); accounts.add_argument("account", nargs="?"); accounts.add_argument("epoch", type=int, nargs="?"); accounts.add_argument("--now", type=int)
    doctor = sub.add_parser("doctor"); _add_common(doctor); _add_json(doctor)

    install = sub.add_parser("install"); install.add_argument("--source", type=Path, required=True); install.add_argument("--home", type=Path); install.add_argument("--version", dest="install_version"); _add_json(install)
    uninstall = sub.add_parser("uninstall"); uninstall.add_argument("--home", type=Path); _add_json(uninstall)
    status = sub.add_parser("status"); status.add_argument("--home", type=Path); _add_json(status)
    viewer = sub.add_parser("viewer"); _add_common(viewer); viewer.add_argument("--port", type=int, default=8766); viewer.add_argument("--remote", action="store_true")
    migrate = sub.add_parser("migrate"); _add_common(migrate); _add_json(migrate); migrate.add_argument("--from-db", required=True); migrate.add_argument("--from-units", nargs="?", const=True); migrate.add_argument("--destination"); migrate.add_argument("--dry-run", action="store_true", required=True)
    importer = sub.add_parser("import-legacy"); _add_common(importer); _add_json(importer); importer.add_argument("--backlog", required=True); importer.add_argument("--runs", required=True)
    return parser


def _legacy_plan_invocation(argv: Sequence[str]) -> bool:
    try:
        index = argv.index("plan")
    except ValueError:
        return False
    tail = [item for item in argv[index + 1:] if item != "--json"]
    return bool(tail and not any(item.startswith("--") for item in tail))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if _legacy_plan_invocation(arguments):
        _json({
            "ok": False,
            "error": "fixed positional plan was removed; use bonus-drain gates --json and bonus-drain plan --json",
            "code": "legacy_positional_plan_removed",
        }, stream=sys.stderr)
        return 2
    try:
        args = build_parser().parse_args(arguments)
        return _command(args)
    except CLIError as exc:
        _json({"ok": False, "error": str(exc), "code": "cli_error"}, stream=sys.stderr)
        return exc.exit_code
    except (config_module.ConfigError, db.QueueError, dispatcher.InvalidRoute) as exc:
        _json({"ok": False, "error": str(exc), "code": "invalid_input"}, stream=sys.stderr)
        return 2
    except dispatcher.AlreadyClaimed as exc:
        _json({"ok": False, "error": str(exc), "code": "already_claimed"}, stream=sys.stderr)
        return 1
    except dispatcher.ClassificationFailure as exc:
        _json({"ok": False, "error": str(exc), "code": "classification_failure"}, stream=sys.stderr)
        return 1
    except dispatcher.AmbiguousDispatch as exc:
        _json({"ok": False, "error": str(exc), "code": "ambiguous_dispatch"}, stream=sys.stderr)
        return 3
    except Exception as exc:
        _json({"ok": False, "error": str(exc), "code": "runtime_error"}, stream=sys.stderr)
        return 1
