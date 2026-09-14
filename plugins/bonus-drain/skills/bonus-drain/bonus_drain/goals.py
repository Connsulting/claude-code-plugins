"""Durable Long Horizon joins over the existing queue, without an idle model process."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from typing import Any, Mapping

from .db import QueueDB, QueueError, Task, TERMINAL_STATUSES, is_safe_task_id, utc_now


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QueueError(f'{name} must be nonempty text')
    return value


def _ids(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not is_safe_task_id(x) for x in value):
        raise QueueError(f'{name} must be a list of safe task IDs')
    if len(value) != len(set(value)):
        raise QueueError(f'{name} contains duplicate IDs')
    return value


def _candidate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {'commits', 'runtime'}:
        raise QueueError('candidate requires commits and runtime objects')
    commits, runtime = value['commits'], value['runtime']
    if (not isinstance(commits, dict) or not commits or
            any(not isinstance(k, str) or not k or not isinstance(v, str) or
                re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', v) is None for k, v in commits.items())):
        raise QueueError('candidate commits must name repositories and immutable full commit hashes')
    if (not isinstance(runtime, dict) or
            any(not isinstance(k, str) or not k or not isinstance(v, str) or not v.strip()
                for k, v in runtime.items())):
        raise QueueError('candidate runtime must map components to observed build/deployment identities')
    return value


def _status(connection: sqlite3.Connection, task_id: str) -> str:
    if connection.execute('SELECT 1 FROM dispatch_claims WHERE task_id=?', (task_id,)).fetchone():
        return 'running'
    recovery = connection.execute(
        "SELECT state FROM task_recovery WHERE task_id=?", (task_id,),
    ).fetchone()
    if recovery and recovery[0] in {'scheduled', 'backoff'}:
        return 'recovering'
    row = connection.execute(
        'SELECT status FROM runs WHERE task=? ORDER BY rowid_pk DESC LIMIT 1', (task_id,),
    ).fetchone()
    return row[0] if row else 'queued'


def _settled(connection: sqlite3.Connection, task_id: str, cache: dict[str, bool] | None = None) -> bool:
    """A failed ancestor settles a waiting path; it never satisfies a success dependency."""
    cache = {} if cache is None else cache
    if task_id in cache:
        return cache[task_id]
    status = _status(connection, task_id)
    if status in TERMINAL_STATUSES:
        cache[task_id] = True
        return True
    if status != 'queued':
        cache[task_id] = False
        return False
    row = connection.execute('SELECT depends_on_json FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise QueueError(f'missing goal task: {task_id}')
    for parent in json.loads(row[0] or '[]'):
        if _settled(connection, parent, cache) and _status(connection, parent) != 'done':
            cache[task_id] = True
            return True
    cache[task_id] = False
    return False


def _contract_hash(task: Task) -> str:
    value = task.to_dict()
    # Scheduling/display controls do not change what the worker is authorized to do.
    for field in ('priority', 'size', 'active'):
        value.pop(field)
    return hashlib.sha256(_json(value).encode()).hexdigest()


def guard_contract_edit(connection: sqlite3.Connection, task_id: str, fields: set[str]) -> None:
    if fields <= {'priority', 'size', 'active'}:
        return
    if (connection.execute('SELECT 1 FROM goal_turns WHERE task_id=?', (task_id,)).fetchone() or
            connection.execute("SELECT 1 FROM goal_members WHERE task_id=? AND role='acceptance'", (task_id,)).fetchone()):
        raise QueueError('goal coordinator and acceptance contracts are immutable; create a replacement job')


def guard_history(connection: sqlite3.Connection, task_id: str) -> None:
    if (connection.execute('SELECT 1 FROM goal_turns WHERE task_id=?', (task_id,)).fetchone() or
            connection.execute('SELECT 1 FROM goal_members WHERE task_id=? AND managed=1', (task_id,)).fetchone()):
        raise QueueError('goal-owned run history must be retained; create a fresh follow-up job')


def _matches_contract(connection: sqlite3.Connection, task_id: str, expected: str | None) -> bool:
    row = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    return row is not None and expected is not None and _contract_hash(QueueDB._task_from_row(row)) == expected


def task_admitted(
    connection: sqlite3.Connection, task_id: str, *, now_epoch: float | None = None,
) -> bool:
    """Called inside the shared atomic claim, so manual and Bonus launches obey the bound."""
    now = time.time() if now_epoch is None else float(now_epoch)
    coordinator = connection.execute(
        'SELECT g.*,t.contract_hash FROM goals g JOIN goal_turns t ON t.goal_id=g.id WHERE t.task_id=?',
        (task_id,),
    ).fetchone()
    if coordinator:
        contract = json.loads(coordinator['contract_json'])
        return (_matches_contract(connection, task_id, coordinator['contract_hash'])
                and coordinator['state'] == 'queued' and coordinator['coordinator_task'] == task_id
                and now < contract['deadline'])
    member = connection.execute(
        'SELECT g.*,m.contract_hash FROM goals g JOIN goal_members m ON m.goal_id=g.id '
        'WHERE m.task_id=? AND m.managed=1', (task_id,),
    ).fetchone()
    if not member:
        return True
    if member['contract_hash'] and not _matches_contract(connection, task_id, member['contract_hash']):
        return False
    contract = json.loads(member['contract_json'])
    if member['state'] in {'paused', 'finishing', 'complete'} or now >= contract['deadline']:
        return False
    owner = member['coordinator_task']
    if owner and _status(connection, owner) not in TERMINAL_STATUSES:
        return False
    active = sum(
        row[0] != task_id and _status(connection, row[0]) in {'running', 'dispatched', 'recovering'}
        for row in connection.execute('SELECT task_id FROM goal_members WHERE goal_id=?', (member['id'],))
    )
    return active < contract['max_inflight']


def recovery_admission(
    connection: sqlite3.Connection, task_id: str, *, now_epoch: float | None = None,
) -> tuple[bool, str]:
    """Authorize retained same-ID recovery without weakening the public history guard."""

    now = time.time() if now_epoch is None else float(now_epoch)
    if connection.execute(
        'SELECT 1 FROM goal_turns WHERE task_id=?', (task_id,),
    ).fetchone():
        return False, 'fresh_goal_followup_required'
    member = connection.execute(
        'SELECT g.*,m.role,m.contract_hash FROM goals g JOIN goal_members m ON m.goal_id=g.id '
        'WHERE m.task_id=? AND m.managed=1', (task_id,),
    ).fetchone()
    if not member:
        return True, 'admitted'
    if member['role'] not in {'implementation', 'integration'}:
        return False, 'fresh_goal_followup_required'
    if member['contract_hash'] and not _matches_contract(connection, task_id, member['contract_hash']):
        return False, 'goal_contract_mismatch'
    if member['state'] == 'paused':
        return False, 'goal_paused'
    if member['state'] in {'finishing', 'complete'}:
        return False, 'goal_not_recoverable'
    contract = json.loads(member['contract_json'])
    if now >= contract['deadline']:
        return False, 'goal_deadline_expired'
    owner = member['coordinator_task']
    if owner and _status(connection, owner) not in TERMINAL_STATUSES:
        return False, 'goal_coordinator_active'
    if connection.execute(
        'SELECT 1 FROM goal_operations WHERE goal_id=? AND receipt_json IS NULL',
        (member['id'],),
    ).fetchone():
        return False, 'goal_operation_unresolved'
    active = sum(
        row[0] != task_id and _status(connection, row[0]) in {'running', 'dispatched', 'recovering'}
        for row in connection.execute(
            'SELECT task_id FROM goal_members WHERE goal_id=?', (member['id'],),
        )
    )
    if active >= contract['max_inflight']:
        return False, 'goal_concurrency_held'
    return True, 'admitted'


def coordinator_contract(queue: QueueDB, task: Task) -> dict[str, Any] | None:
    """Only the persisted, unchanged coordinator task receives the explicit goal grant."""
    if not queue.path.is_file():
        return None
    connection = sqlite3.connect(queue.path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'").fetchone():
            return None
        stored = connection.execute('SELECT * FROM tasks WHERE id=?', (task.id,)).fetchone()
        if not stored or queue._task_from_row(stored) != task:
            return None
        row = connection.execute(
            'SELECT g.contract_json,t.contract_hash FROM goals g JOIN goal_turns t ON t.goal_id=g.id '
            'WHERE t.task_id=? AND g.coordinator_task=t.task_id', (task.id,),
        ).fetchone()
        return json.loads(row[0]) if row and row['contract_hash'] == _contract_hash(task) else None
    finally:
        connection.close()


class GoalStore:
    def __init__(self, queue: QueueDB):
        self.queue = queue

    @staticmethod
    def _row(connection: sqlite3.Connection, goal_id: str) -> sqlite3.Row:
        row = connection.execute('SELECT * FROM goals WHERE id=?', (goal_id,)).fetchone()
        if row is None:
            raise QueueError(f'unknown goal: {goal_id}')
        return row

    def create(self, contract: Mapping[str, Any], *, now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else now
        value = dict(contract)
        allowed = {'id', 'title', 'cwd', 'outcome', 'authority', 'acceptance', 'merge_policy',
                   'max_turns', 'deadline', 'max_inflight', 'coordinator', 'task_ids',
                   'source_ref', 'work_group'}
        if set(value) - allowed:
            raise QueueError('unknown goal contract fields: ' + ', '.join(sorted(set(value) - allowed)))
        if not is_safe_task_id(value.get('id')):
            raise QueueError('goal requires a safe ID')
        for field in ('title', 'cwd', 'outcome', 'authority'):
            _text(value.get(field), field)
        from pathlib import Path
        if not Path(value['cwd']).is_absolute() or not Path(value['cwd']).is_dir():
            raise QueueError('goal cwd must be an existing absolute directory')
        for field in ('max_turns', 'deadline', 'max_inflight'):
            if type(value.get(field)) is not int or value[field] <= 0:
                raise QueueError(f'{field} must be an explicit positive integer')
        if value['deadline'] <= now:
            raise QueueError('goal deadline has elapsed')
        if value.get('merge_policy') not in {'stack', 'merge'}:
            raise QueueError('merge_policy must be stack or merge')
        acceptance = value.get('acceptance')
        if (not isinstance(acceptance, list) or not acceptance or
                any(not isinstance(x, dict) or set(x) != {'id', 'proof'} for x in acceptance)):
            raise QueueError('acceptance requires a nonempty list of {id, proof} criteria')
        _ids([x['id'] for x in acceptance], 'acceptance IDs')
        for criterion in acceptance:
            _text(criterion['proof'], 'acceptance proof')
        routing = value.get('coordinator', {})
        if not isinstance(routing, dict) or set(routing) - {'model', 'allowed_providers', 'required_capabilities', 'mcp'}:
            raise QueueError('coordinator only accepts ordinary task routing fields')
        _text(routing.get('model'), 'explicit coordinator model')
        for field in ('allowed_providers', 'required_capabilities'):
            if field in routing:
                if (not isinstance(routing[field], list) or
                        any(not isinstance(x, str) or not x.strip() for x in routing[field])):
                    raise QueueError(f'coordinator {field} must be an array of nonempty strings')
        if routing.get('mcp') is not None:
            _text(routing['mcp'], 'coordinator mcp')
        value['coordinator'] = routing
        value['task_ids'] = _ids(value.get('task_ids', []), 'task_ids')
        self.queue._work_fields(value)
        self.queue.initialize()
        with self.queue._transaction() as connection:
            previous = connection.execute('SELECT contract_json FROM goals WHERE id=?', (value['id'],)).fetchone()
            if previous:
                if previous[0] != _json(value):
                    raise QueueError('goal ID conflict: existing contract differs')
            else:
                connection.execute(
                    'INSERT INTO goals(id,contract_json,state,summary,created_at) VALUES(?,?,?,?,?)',
                    (value['id'], _json(value), 'waiting', 'Initial coordination is ready', utc_now()),
                )
                self._link_existing(connection, value['id'], value['task_ids'])
        return self.show(value['id'])

    @staticmethod
    def _link_existing(connection: sqlite3.Connection, goal_id: str, task_ids: list[str]) -> None:
        for task_id in task_ids:
            row = connection.execute('SELECT kind FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row[0] != 'oneoff':
                raise QueueError(f'goal members must be existing one-off tasks: {task_id}')
            other = connection.execute('SELECT goal_id FROM goal_members WHERE task_id=?', (task_id,)).fetchone()
            if other and other[0] != goal_id:
                raise QueueError(f'task already belongs to goal {other[0]}: {task_id}')
            connection.execute(
                "INSERT OR IGNORE INTO goal_members(goal_id,task_id,role,managed) VALUES(?,?,'existing',0)",
                (goal_id, task_id),
            )

    def show(self, goal_id: str) -> dict[str, Any]:
        self.queue.initialize()
        with self.queue._connect() as connection:
            row = self._row(connection, goal_id)
            value = dict(row)
            for field in ('contract', 'wait', 'candidate'):
                raw = value.pop(field + '_json')
                value[field] = json.loads(raw) if raw is not None else None
            members = []
            settled_cache: dict[str, bool] = {}
            for member in connection.execute('SELECT * FROM goal_members WHERE goal_id=? ORDER BY task_id', (goal_id,)):
                item = dict(member)
                raw = item.pop('candidate_json')
                item['candidate'] = json.loads(raw) if raw else None
                item['status'] = _status(connection, item['task_id'])
                item['settled'] = _settled(connection, item['task_id'], settled_cache)
                item['runs'] = [dict(x) for x in connection.execute(
                    'SELECT * FROM runs WHERE task=? ORDER BY rowid_pk', (item['task_id'],))]
                members.append(item)
            value['members'] = members
            value['decisions'] = []
            for turn in connection.execute('SELECT * FROM goal_turns WHERE goal_id=? ORDER BY turn', (goal_id,)):
                item = dict(turn)
                raw = item.pop('decision_json')
                item['decision'] = json.loads(raw) if raw else None
                value['decisions'].append(item)
            value['steering'] = [dict(x) for x in connection.execute(
                'SELECT * FROM goal_steering WHERE goal_id=? ORDER BY revision', (goal_id,))]
            value['operations'] = []
            for operation in connection.execute('SELECT * FROM goal_operations WHERE goal_id=? ORDER BY created_at,key', (goal_id,)):
                item = dict(operation)
                item['intent'] = json.loads(item.pop('intent_json'))
                raw = item.pop('receipt_json')
                item['receipt'] = json.loads(raw) if raw else None
                value['operations'].append(item)
            return value

    def list(self) -> list[dict[str, Any]]:
        self.queue.initialize()
        with self.queue._connect() as connection:
            return [dict(x) for x in connection.execute(
                'SELECT id,state,revision,turn,coordinator_task,summary FROM goals ORDER BY created_at,id')]

    def tick(self, goal_id: str | None = None, *, now: int | None = None,
             dry_run: bool = False) -> list[dict[str, Any]]:
        """Reconcile decision joins and enqueue at most one ordinary turn per goal. No model I/O."""
        now = int(time.time()) if now is None else now
        self.queue.initialize()
        result = []
        with self.queue._transaction() as connection:
            settled_cache: dict[str, bool] = {}
            rows = ([self._row(connection, goal_id)] if goal_id else
                    connection.execute('SELECT * FROM goals ORDER BY id').fetchall())
            for row in rows:
                if row['state'] in {'paused', 'complete'}:
                    continue
                contract = json.loads(row['contract_json'])
                pause = None
                owner = row['coordinator_task']
                if row['state'] == 'finishing':
                    status = _status(connection, owner)
                    if status not in TERMINAL_STATUSES:
                        continue
                    final_state = 'complete' if status == 'done' else 'paused'
                    summary = row['summary'] if status == 'done' else 'Coordinator closeout failed; reconcile before resuming'
                    result.append({'goal_id': row['id'], 'action': final_state, 'reason': summary})
                    if not dry_run:
                        connection.execute('UPDATE goals SET state=?,summary=?,revision=revision+1 WHERE id=?',
                                           (final_state, summary, row['id']))
                    continue
                if now >= contract['deadline']:
                    pause = 'Goal deadline elapsed; preserve running owners and request a revised bound'
                if owner and not pause:
                    status = _status(connection, owner)
                    if status not in TERMINAL_STATUSES:
                        continue
                    decision = connection.execute('SELECT decision_json FROM goal_turns WHERE task_id=?', (owner,)).fetchone()[0]
                    if status != 'done' or decision is None:
                        pause = 'Coordinator ended without a successful recorded decision; reconcile before resuming'
                wait = json.loads(row['wait_json'])
                if not pause and not all(_settled(connection, task, settled_cache) for task in wait):
                    continue
                if not pause and row['turn'] >= contract['max_turns']:
                    pause = 'Goal coordination budget exhausted; request a revised bound'
                if pause:
                    result.append({'goal_id': row['id'], 'action': 'pause', 'reason': pause})
                    if not dry_run:
                        connection.execute("UPDATE goals SET state='paused',revision=revision+1,summary=? WHERE id=?", (pause, row['id']))
                    continue
                turn = row['turn'] + 1
                digest = hashlib.sha256(row['id'].encode()).hexdigest()[:24]
                task_id = f'lh-{digest}-{turn}'
                reason = 'Initial or resumed coordination' if not wait else 'Dependency join settled: ' + ', '.join(wait)
                result.append({'goal_id': row['id'], 'action': 'enqueue', 'task_id': task_id, 'reason': reason})
                if dry_run:
                    continue
                values = {
                    'id': task_id, 'title': f"Long Horizon: {contract['title']} (turn {turn})",
                    'kind': 'oneoff', 'size': 'small', 'priority': 1, 'cwd': contract['cwd'],
                    'goal': f"Use the bonus-drain plugin's long-horizon skill for ONE coordination turn of goal {row['id']}. "
                            'Read the current goal with bonus-drain goal show, collect the joined results, '
                            'record one decision with goal advance, record this job terminal, then END THE TURN. '
                            'The runtime will enqueue your next turn. Do not poll, sleep, or wait on drivers.',
                    'context': _json({'goal_id': row['id'], 'turn_task': task_id,
                                      'expected_revision': row['revision'] + 1, 'reason': reason,
                                      'contract': contract}),
                    'constraints': contract['authority'] + '\nMerge policy: ' + contract['merge_policy'] +
                                   '. Preserve existing task contracts. Delegate each build to /implement; '
                                   'do not duplicate its stages. Create jobs atomically via goal advance. '
                                   'No direct provider dispatch. An ambiguous operation needs reconciliation before retry.',
                    'done_when': 'A durable goal decision has been accepted, with task/acceptance evidence and cleanup links. '
                                 'Waiting for jobs is a successful coordination decision, not goal completion.',
                    'source_ref': contract.get('source_ref'), 'work_group': contract.get('work_group'),
                    **contract['coordinator'],
                }
                inserted = self.queue._insert_task(connection, values)
                connection.execute('INSERT INTO goal_turns(goal_id,turn,task_id,contract_hash,reason,created_at) VALUES(?,?,?,?,?,?)',
                                   (row['id'], turn, task_id, _contract_hash(inserted), reason, utc_now()))
                connection.execute("UPDATE goals SET state='queued',turn=?,coordinator_task=?,revision=revision+1,summary=? WHERE id=?",
                                   (turn, task_id, reason, row['id']))
        return result

    def advance(self, goal_id: str, turn_task: str, decision: Mapping[str, Any], *,
                now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else now
        value = dict(decision)
        allowed = {'expected_revision', 'action', 'summary', 'tasks', 'task_ids', 'wait_for',
                   'candidate', 'observations'}
        if set(value) - allowed:
            raise QueueError('unknown goal decision fields')
        if value.get('action') not in {'wait', 'pause', 'complete'}:
            raise QueueError('decision action must be wait, pause, or complete')
        _text(value.get('summary'), 'summary')
        task_ids = _ids(value.get('task_ids', []), 'task_ids')
        wait = _ids(value.get('wait_for', []), 'wait_for')
        tasks = value.get('tasks', [])
        if not isinstance(tasks, list):
            raise QueueError('tasks must be an array')
        self.queue.initialize()
        with self.queue._transaction() as connection:
            row = self._row(connection, goal_id)
            turn = connection.execute('SELECT * FROM goal_turns WHERE goal_id=? AND task_id=?', (goal_id, turn_task)).fetchone()
            if not turn:
                raise QueueError('unknown coordinator turn')
            if turn['decision_json'] is not None:
                if turn['decision_json'] != _json(value):
                    raise QueueError('goal decision conflict: this turn already recorded a different decision')
                return {'goal_id': goal_id, 'turn_task': turn_task, 'decision': value}
            if type(value.get('expected_revision')) is not int or row['revision'] != value['expected_revision']:
                raise QueueError('goal revision conflict: read the current goal before deciding')
            if row['state'] != 'queued' or row['coordinator_task'] != turn_task:
                raise QueueError('goal is not owned by this coordinator turn')
            if _status(connection, turn_task) not in {'running', 'dispatched'}:
                raise QueueError('coordinator must own an active queue job before deciding')
            contract = json.loads(row['contract_json'])
            if now >= contract['deadline'] and value['action'] != 'pause':
                raise QueueError('deadline elapsed; only a pause decision is allowed')
            candidate = value.get('candidate')
            if candidate is not None:
                _candidate(candidate)
            elif row['candidate_json']:
                candidate = json.loads(row['candidate_json'])
            self._link_existing(connection, goal_id, task_ids)
            for item in tasks:
                if (not isinstance(item, dict) or set(item) - {'task', 'role', 'candidate'} or
                        item.get('role') not in {'implementation', 'integration', 'acceptance'}):
                    raise QueueError('new goal task requires task and a valid role')
                raw = item.get('task')
                if not isinstance(raw, dict) or raw.get('kind', 'oneoff') != 'oneoff':
                    raise QueueError('new goal tasks must be one-off task contracts')
                if raw.get('active', True) is not True:
                    raise QueueError('new goal tasks must be active; pause the goal to hold admission')
                for field in ('id', 'title', 'cwd', 'goal', 'done_when'):
                    _text(raw.get(field), 'task ' + field)
                from .db import require_task_size
                require_task_size(raw.get('size'))
                if item['role'] == 'implementation' and raw.get('use_implement') is not True:
                    raise QueueError('new implementation jobs must use /implement')
                tested = None
                if item['role'] == 'acceptance':
                    tested = _candidate(item.get('candidate'))
                task = dict(raw)
                task.setdefault('source_ref', contract.get('source_ref'))
                task.setdefault('work_group', contract.get('work_group'))
                task['constraints'] = (task.get('constraints') or '') + '\nGoal authority: ' + contract['authority'] + '\nMerge policy: ' + contract['merge_policy']
                task['constraints'] += '\nAny goal merge authority belongs to the coordinator; this task must not merge.'
                if tested:
                    task['context'] = (task.get('context') or '') + '\nRequired assembled acceptance: ' + _json({
                        'candidate': tested, 'criteria': contract['acceptance']})
                inserted = self.queue._insert_task(connection, task)
                connection.execute('INSERT INTO goal_members(goal_id,task_id,role,managed,candidate_json,contract_hash) VALUES(?,?,?,?,?,?)',
                                   (goal_id, task['id'], item['role'], 1, _json(tested) if tested else None,
                                    _contract_hash(inserted) if tested else None))
            members = {m['task_id']: m for m in connection.execute('SELECT * FROM goal_members WHERE goal_id=?', (goal_id,))}
            if set(wait) - members.keys():
                raise QueueError('wait_for must name explicit goal members')
            observations = self._observations(connection, contract, members, value.get('observations', []))
            if value['action'] == 'wait':
                settled_cache: dict[str, bool] = {}
                if not wait or all(_settled(connection, task, settled_cache) for task in wait):
                    raise QueueError('wait requires a join with unfinished work; do not rearm a settled join')
                state = 'waiting'
            elif value['action'] == 'complete':
                if tasks or wait or candidate is None:
                    raise QueueError('complete requires an identified candidate and no new or waiting work')
                passed = {x['criterion'] for x in observations if x['outcome'] == 'pass'}
                if passed != {x['id'] for x in contract['acceptance']}:
                    raise QueueError('every acceptance criterion needs a passing observation')
                for observation in observations:
                    if json.loads(members[observation['task_id']]['candidate_json']) != candidate:
                        raise QueueError('acceptance candidate differs from the final candidate')
                if any(_status(connection, task) not in TERMINAL_STATUSES for task in members):
                    raise QueueError('cannot complete with unfinished goal work')
                if connection.execute('SELECT 1 FROM goal_operations WHERE goal_id=? AND receipt_json IS NULL', (goal_id,)).fetchone():
                    raise QueueError('cannot complete with an unresolved operation')
                state = 'finishing'
            else:
                state = 'paused'
            connection.execute('UPDATE goal_turns SET decision_json=? WHERE task_id=?', (_json(value), turn_task))
            connection.execute('UPDATE goals SET state=?,wait_json=?,candidate_json=?,summary=?,revision=revision+1 WHERE id=?',
                               (state, _json(wait), _json(candidate) if candidate else None, value['summary'], goal_id))
        return {'goal_id': goal_id, 'turn_task': turn_task, 'decision': value}

    @staticmethod
    def _observations(connection: sqlite3.Connection, contract: dict, members: dict, raw: Any) -> list[dict]:
        if not isinstance(raw, list):
            raise QueueError('observations must be an array')
        criteria = {x['id'] for x in contract['acceptance']}
        seen = set()
        for item in raw:
            if not isinstance(item, dict) or set(item) != {'criterion', 'task_id', 'outcome', 'evidence'}:
                raise QueueError('observation requires criterion, task_id, outcome, evidence')
            if item['criterion'] not in criteria or item['criterion'] in seen:
                raise QueueError('unknown or duplicate acceptance criterion')
            seen.add(item['criterion'])
            member = members.get(item['task_id'])
            if not member or member['role'] != 'acceptance':
                raise QueueError('observation must reference a goal acceptance job')
            if not _matches_contract(connection, item['task_id'], member['contract_hash']):
                raise QueueError('acceptance job contract changed after its candidate was frozen')
            status = _status(connection, item['task_id'])
            if status not in TERMINAL_STATUSES or item['outcome'] not in {'pass', 'fail', 'unavailable'}:
                raise QueueError('acceptance observation requires a terminal job and a valid outcome')
            if item['outcome'] == 'pass' and status != 'done':
                raise QueueError('passing acceptance requires a done job')
            _text(item['evidence'], 'acceptance evidence')
        return raw

    def steer(self, goal_id: str, expected_revision: int, message: str, *,
              now: int | None = None, pause: bool = False) -> dict[str, Any]:
        """Record user steering. A running turn reads it; an idle join wakes once."""
        _text(message, 'steering message')
        self.queue.initialize()
        with self.queue._transaction() as connection:
            row = self._row(connection, goal_id)
            if row['revision'] != expected_revision:
                raise QueueError('goal revision conflict')
            if row['state'] == 'complete':
                raise QueueError('completed goals are immutable; create a new goal')
            state = 'paused' if pause else ('waiting' if row['state'] == 'finishing' else row['state'])
            connection.execute('UPDATE goals SET state=?,wait_json=\'[]\',summary=?,revision=revision+1 WHERE id=?',
                               (state, message, goal_id))
            connection.execute('INSERT INTO goal_steering(goal_id,revision,message,created_at) VALUES(?,?,?,?)',
                               (goal_id, expected_revision + 1, message, utc_now()))
        return self.show(goal_id)

    def operation(self, goal_id: str, turn_task: str, value: Mapping[str, Any]) -> dict[str, Any]:
        """Persist intent before an external effect and its receipt after reconciliation."""
        if set(value) - {'key', 'intent', 'receipt'}:
            raise QueueError('operation accepts key, intent, and optional receipt')
        key = _text(value.get('key'), 'operation key')
        intent, receipt = value.get('intent'), value.get('receipt')
        if not isinstance(intent, dict) or not intent:
            raise QueueError('operation intent must be a nonempty object')
        _text(intent.get('action'), 'operation action')
        if receipt is not None and (not isinstance(receipt, dict) or not receipt):
            raise QueueError('operation receipt must be a nonempty object')
        self.queue.initialize()
        with self.queue._transaction() as connection:
            row = self._row(connection, goal_id)
            if row['coordinator_task'] != turn_task or _status(connection, turn_task) not in {'running', 'dispatched'}:
                raise QueueError('operation requires the current live coordinator')
            contract = json.loads(row['contract_json'])
            if intent['action'] == 'merge' and contract['merge_policy'] != 'merge':
                raise QueueError('stack policy does not authorize merge operations')
            previous = connection.execute('SELECT * FROM goal_operations WHERE goal_id=? AND key=?', (goal_id, key)).fetchone()
            if previous:
                if previous['intent_json'] != _json(intent):
                    raise QueueError('operation intent conflict; reconcile the original effect')
                if receipt is not None:
                    if previous['receipt_json'] and previous['receipt_json'] != _json(receipt):
                        raise QueueError('operation receipt conflict')
                    connection.execute('UPDATE goal_operations SET receipt_json=? WHERE goal_id=? AND key=?', (_json(receipt), goal_id, key))
            else:
                if row['state'] != 'queued' or time.time() >= contract['deadline']:
                    raise QueueError('new operation requires an active goal within its deadline')
                if receipt is not None:
                    raise QueueError('record operation intent before supplying its receipt')
                connection.execute('INSERT INTO goal_operations(goal_id,key,turn_task,intent_json,created_at) VALUES(?,?,?,?,?)',
                                   (goal_id, key, turn_task, _json(intent), utc_now()))
        return {'goal_id': goal_id, 'key': key, 'intent': intent, 'receipt': receipt}

    def resume(self, goal_id: str, expected_revision: int, message: str, *,
               now: int | None = None, max_turns: int | None = None,
               deadline: int | None = None) -> dict[str, Any]:
        """Explicit operator resumption preserves consumed turns, history, and live owners."""
        now = int(time.time()) if now is None else now
        _text(message, 'resume message')
        self.queue.initialize()
        with self.queue._transaction() as connection:
            row = self._row(connection, goal_id)
            if row['revision'] != expected_revision:
                raise QueueError('goal revision conflict')
            if row['state'] != 'paused':
                raise QueueError('only a paused goal can resume')
            owner = row['coordinator_task']
            owner_status = _status(connection, owner) if owner else None
            if owner and owner_status not in TERMINAL_STATUSES | {'queued'}:
                raise QueueError('coordinator is still live or ambiguous; reconcile before resuming')
            contract = json.loads(row['contract_json'])
            for field, supplied in (('max_turns', max_turns), ('deadline', deadline)):
                if supplied is not None:
                    if type(supplied) is not int or supplied <= 0:
                        raise QueueError(f'{field} must be a positive integer')
                    contract[field] = supplied
            if now >= contract['deadline'] or row['turn'] >= contract['max_turns']:
                raise QueueError('goal bounds are exhausted; explicitly revise the contract before resuming')
            if owner and owner_status == 'queued':
                # The same write lock that protects claims proves no launch acquired this turn.
                connection.execute('UPDATE tasks SET active=0 WHERE id=?', (owner,))
            # The retired turn is retained in goal_turns; only the active-owner pointer clears.
            connection.execute("UPDATE goals SET state='waiting',wait_json='[]',coordinator_task=NULL,summary=?,contract_json=?,revision=revision+1 WHERE id=?",
                               (message, _json(contract), goal_id))
            connection.execute('INSERT INTO goal_steering(goal_id,revision,message,created_at) VALUES(?,?,?,?)',
                               (goal_id, expected_revision + 1, _json({'message': message, 'max_turns': max_turns, 'deadline': deadline}), utc_now()))
        return self.show(goal_id)
