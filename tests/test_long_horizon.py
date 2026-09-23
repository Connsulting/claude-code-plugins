"""Goal coordination on the real SQLite queue; provider execution is a separate proof."""
import json
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from dataclasses import replace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/bonus-drain/skills/bonus-drain'))
from bonus_drain import db, goals, dispatcher, scout, usage
from tests import test_bonus_drain_kick as kick_tests

NOW = 2_000_000_000
CANDIDATE = {'commits': {'example': 'a' * 40}, 'runtime': {}}


def verified_outcome(task_id):
    return {
        'reason': {'code': 'done_when_verified', 'detail': 'fixture proof',
                   'signature': f'done_when_verified:{task_id}'},
        'completion': {'verified': True, 'mechanism': 'command',
                       'evidence': [f'fixture://long-horizon/{task_id}']},
    }


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = db.QueueDB(Path(self.tmp.name) / 'queue.db')
        self.store = goals.GoalStore(self.queue)

    def contract(self, **changes):
        return dict(id='release', title='Example release', cwd=self.tmp.name,
                    outcome='The combined example works', authority='Local disposable work only.',
                    acceptance=[{'id': 'journey', 'proof': 'Execute the combined positive and negative paths.'}],
                    merge_policy='stack', max_turns=8, deadline=NOW + 3600,
                    max_inflight=2, coordinator={'model': 'gpt-6-astra'},
                    task_ids=[], **changes)

    def create(self, **changes):
        contract = self.contract()
        contract.update(changes)
        return self.store.create(contract, now=NOW)

    def finish(self, task_id, status='done'):
        key = 'account/manual/2000000000'
        claim = self.queue.claim_for(task_id, key)
        attempt_id = claim.attempt_id if claim else None
        if status == 'done' and attempt_id is None:
            attempt = self.queue.claim(task_id, key, 'alpha', 'account')
            self.assertIsNotNone(attempt)
            attempt_id = attempt.id
        kwargs = {'attempt_id': attempt_id} if attempt_id is not None else {}
        if status == 'done':
            kwargs['outcome'] = verified_outcome(task_id)
        elif attempt_id is not None:
            kwargs['outcome'] = {
                'reason': {'code': 'retryable', 'detail': 'fixture failure',
                           'signature': f'retryable:{task_id}'},
            }
        self.queue.record(task_id, key, status=status, provider_id='alpha', **kwargs)

    def start_turn(self):
        self.store.tick(now=NOW)
        goal = self.store.show('release')
        task = goal['coordinator_task']
        attempt = self.queue.claim(task, 'account/manual/2000000000', 'alpha', 'account')
        self.assertIsNotNone(attempt)
        self.queue.record(
            task, 'account/manual/2000000000', attempt_id=attempt.id,
            status='dispatched', provider_id='alpha',
        )
        return task, goal['revision']

    def job(self, task_id, **changes):
        value = dict(id=task_id, title=task_id, cwd=self.tmp.name, goal='Prove the example',
                     size='small', done_when='Evidence retained', use_implement=True)
        value.update(changes)
        return {'task': value, 'role': 'implementation'}

    def test_optional_start_ref_preserves_old_goal_hash_until_set(self):
        queued = self.queue.add_task(self.job('standalone')['task'])
        value = queued.to_dict()
        self.assertIsNone(value.pop('start_ref'))
        for field in ('priority', 'size', 'active'):
            value.pop(field)
        old_hash = hashlib.sha256(goals._json(value).encode()).hexdigest()
        self.assertEqual(goals._contract_hash(queued), old_hash)
        selected = self.queue.edit_task('standalone', {'start_ref': 'epic/next'})
        self.assertNotEqual(goals._contract_hash(selected), old_hash)

    def advance(self, turn, revision, **changes):
        value = dict(expected_revision=revision, action='wait', summary='Ready for jobs',
                     tasks=[], wait_for=[], candidate=CANDIDATE)
        value.update(changes)
        return self.store.advance('release', turn, value, now=NOW)

    def test_create_is_queue_only_and_does_not_adopt_group_by_title(self):
        self.queue.add_task(dict(id='unrelated', title='release', work_group='Example', cwd=self.tmp.name))
        self.create()
        self.assertEqual(self.queue.runs(), [])
        self.assertEqual(self.store.show('release')['members'], [])

    def test_concurrent_ticks_enqueue_exactly_one_coordinator(self):
        self.create()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: goals.GoalStore(self.queue).tick(now=NOW), range(8)))
        tasks = [t for t in self.queue.tasks() if t.id != 'unrelated']
        self.assertEqual(len(tasks), 1)
        self.assertFalse(tasks[0].use_implement)
        self.assertEqual(tasks[0].model, 'gpt-6-astra')
        self.assertEqual(self.store.show('release')['turn'], 1)

    def test_dry_tick_does_not_enqueue_or_advance_state(self):
        self.create()
        before = self.store.show('release')
        self.store.tick(now=NOW, dry_run=True)
        self.assertEqual(self.store.show('release'), before)
        self.assertEqual(self.queue.tasks(), [])

    def test_waiting_survives_restart_partial_completion_and_repeated_polls(self):
        self.create()
        turn, revision = self.start_turn()
        self.advance(turn, revision, tasks=[self.job('a'), self.job('b')], wait_for=['a', 'b'])
        # The coordinator records its decision and exits; a goal is not an in-flight job.
        self.finish(turn)
        self.finish('a')
        for _ in range(20):
            goals.GoalStore(db.QueueDB(self.queue.path)).tick(now=NOW)
        self.assertEqual(self.store.show('release')['turn'], 1)
        self.assertEqual(self.queue.inflight(), [])
        self.finish('b')
        self.store.tick(now=NOW)
        self.assertEqual(self.store.show('release')['turn'], 2)
        self.store.tick(now=NOW)
        self.assertEqual(self.store.show('release')['turn'], 2)

    def test_decision_retry_is_idempotent_but_stale_conflicting_write_fails(self):
        self.create()
        turn, revision = self.start_turn()
        kwargs = dict(tasks=[self.job('a')], wait_for=['a'])
        first = self.advance(turn, revision, **kwargs)
        self.assertEqual(self.advance(turn, revision, **kwargs), first)
        with self.assertRaisesRegex(db.QueueError, 'conflict'):
            self.advance(turn, revision, tasks=[self.job('b')], wait_for=['b'])
        self.assertIsNone(self.queue.task('b'))

    def test_invalid_join_rolls_back_new_tasks_and_decision(self):
        self.create()
        turn, revision = self.start_turn()
        with self.assertRaises(db.QueueError):
            self.advance(turn, revision, tasks=[self.job('a')], wait_for=['missing'])
        self.assertIsNone(self.queue.task('a'))
        self.assertEqual(self.store.show('release')['revision'], revision)

    def test_dependency_join_unblocks_on_failed_ancestor_without_marking_success(self):
        self.create()
        turn, revision = self.start_turn()
        self.advance(turn, revision, tasks=[self.job('a'), self.job('child', depends_on=['a'])],
                     wait_for=['child'])
        self.finish(turn)
        self.finish('a', 'failed')
        self.store.tick(now=NOW)
        self.assertEqual(self.store.show('release')['turn'], 2)
        self.assertFalse(self.queue.readiness('child')['ready'])

    def test_live_or_ambiguous_coordinator_is_never_replaced(self):
        self.create()
        turn, revision = self.start_turn()
        self.advance(turn, revision, tasks=[self.job('a')], wait_for=['a'])
        # This contract predates attempt provenance: the completed child is an
        # authentic migrated row, so recording it must not claim around the
        # still-live coordinator's admission guard.
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute(
                "INSERT INTO runs(task,kind,cycle,eligibility_key,status,ts,summary) "
                "VALUES('a','oneoff',?,NULL,'done',?,'historical completion')",
                (NOW, '2033-05-18T03:33:20Z'),
            )
        self.store.tick(now=NOW)
        self.assertEqual(self.store.show('release')['coordinator_task'], turn)
        with self.assertRaises(db.QueueError):
            self.store.resume('release', self.store.show('release')['revision'], 'retry', now=NOW)

    def test_terminal_coordinator_without_decision_pauses_instead_of_looping(self):
        self.create()
        turn, _ = self.start_turn()
        self.finish(turn)
        self.store.tick(now=NOW)
        self.assertEqual(self.store.show('release')['state'], 'paused')
        self.assertIn('decision', self.store.show('release')['summary'])

    def test_deadline_closes_unclaimed_goal_work_without_touching_other_tasks(self):
        self.create()
        turn, revision = self.start_turn()
        self.advance(turn, revision, tasks=[self.job('a')], wait_for=['a'])
        self.finish(turn)
        self.queue.add_task(dict(id='other', title='Other', cwd=self.tmp.name))
        self.store.tick(now=NOW + 3601)
        self.assertFalse(self.queue.claim('a', 'account/manual/2000000000', 'alpha', 'account'))
        self.assertTrue(self.queue.claim('other', 'account/manual/2000000000', 'alpha', 'account'))

    def test_goal_concurrency_is_enforced_by_claim_in_both_launch_paths(self):
        self.create(max_inflight=1)
        turn, revision = self.start_turn()
        self.advance(turn, revision, tasks=[self.job('a'), self.job('b')], wait_for=['a', 'b'])
        self.finish(turn)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda task: self.queue.claim(
                task, 'account/manual/2000000000', 'alpha', 'account'), ['a', 'b']))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_explicit_membership_does_not_rewrite_existing_contract(self):
        self.queue.add_task(dict(id='existing', title='Existing', cwd=self.tmp.name,
                                 constraints='Do not merge', use_implement=False))
        before = self.queue.task('existing')
        self.create(task_ids=['existing'])
        self.assertEqual(self.queue.task('existing'), before)
        self.assertEqual(self.store.show('release')['members'][0]['task_id'], 'existing')

    def test_completion_requires_completed_acceptance_on_exact_candidate(self):
        self.create()
        turn, revision = self.start_turn()
        verifier = self.job('verify', use_implement=False)
        verifier.update(role='acceptance', candidate=CANDIDATE)
        self.advance(turn, revision, tasks=[verifier], wait_for=['verify'])
        self.finish(turn)
        self.finish('verify')
        turn, revision = self.start_turn()
        with self.assertRaisesRegex(db.QueueError, 'acceptance'):
            self.advance(turn, revision, action='complete')
        evidence = [{'criterion': 'journey', 'task_id': 'verify', 'outcome': 'pass',
                     'evidence': '/proofs/journey.json'}]
        with self.assertRaisesRegex(db.QueueError, 'candidate'):
            self.advance(turn, revision, action='complete', observations=evidence,
                         candidate={'commits': {'example': 'b' * 40}, 'runtime': {}})
        self.advance(turn, revision, action='complete', observations=evidence)
        self.assertEqual(self.store.show('release')['state'], 'finishing')
        self.finish(turn)
        self.store.tick(now=NOW)
        self.assertEqual(self.store.show('release')['state'], 'complete')

    def test_two_rounds_preserve_failure_and_require_fresh_acceptance(self):
        self.create()
        turn, revision = self.start_turn()
        verifier = self.job('verify-1', use_implement=False)
        verifier.update(role='acceptance', candidate=CANDIDATE)
        self.advance(turn, revision, tasks=[verifier], wait_for=['verify-1'])
        self.finish(turn)
        self.finish('verify-1', 'failed')
        turn, revision = self.start_turn()
        # A correction references the observation; it must not depend on the failed verifier.
        fix = self.job('fix', context='Repair the finding in verify-1')
        self.advance(turn, revision, tasks=[fix], wait_for=['fix'],
                     observations=[{'criterion': 'journey', 'task_id': 'verify-1',
                                    'outcome': 'fail', 'evidence': '/proofs/failure.json'}])
        self.finish(turn)
        self.assertTrue(self.queue.readiness('fix')['ready'])
        self.finish('fix')
        # Only after the fix result exists does the next turn pin new acceptance.
        turn, revision = self.start_turn()
        changed = {'commits': {'example': 'b' * 40}, 'runtime': {}}
        verify2 = self.job('verify-2', depends_on=['fix'], use_implement=False)
        verify2.update(role='acceptance', candidate=changed)
        self.advance(turn, revision, tasks=[verify2], wait_for=['verify-2'], candidate=changed)
        self.finish(turn)
        self.finish('verify-2')
        turn, revision = self.start_turn()
        self.advance(turn, revision, action='complete', candidate=changed,
                     observations=[{'criterion': 'journey', 'task_id': 'verify-2',
                                    'outcome': 'pass', 'evidence': '/proofs/pass.json'}])
        self.finish(turn)
        self.store.tick(now=NOW)
        goal = self.store.show('release')
        self.assertEqual(goal['state'], 'complete')
        self.assertEqual(self.queue.runs(task_id='verify-1')[0].status, 'failed')
        self.assertEqual(len(goal['decisions']), 4)

    def test_failed_closeout_does_not_leave_goal_falsely_complete(self):
        self.create()
        turn, revision = self.start_turn()
        verifier = self.job('verify', use_implement=False)
        verifier.update(role='acceptance', candidate=CANDIDATE)
        self.advance(turn, revision, tasks=[verifier], wait_for=['verify'])
        self.finish(turn)
        self.finish('verify')
        turn, revision = self.start_turn()
        self.advance(turn, revision, action='complete', observations=[{
            'criterion': 'journey', 'task_id': 'verify', 'outcome': 'pass', 'evidence': '/proofs/pass.json'}])
        self.finish(turn, 'failed')
        self.store.tick(now=NOW)
        self.assertEqual(self.store.show('release')['state'], 'paused')

    def test_cannot_rearm_already_settled_join_and_burn_turns(self):
        self.create()
        turn, revision = self.start_turn()
        self.queue.add_task(dict(id='old', title='Old', cwd=self.tmp.name))
        self.finish('old')
        with self.assertRaises(db.QueueError):
            self.advance(turn, revision, task_ids=['old'], wait_for=['old'])

    def test_pause_and_resume_do_not_take_over_live_jobs(self):
        self.create()
        turn, revision = self.start_turn()
        self.advance(turn, revision, action='pause', summary='Need an input')
        self.finish(turn)
        state = self.store.show('release')
        self.store.resume('release', state['revision'], 'Input is now available', now=NOW)
        self.store.tick(now=NOW)
        self.assertEqual(self.store.show('release')['turn'], 2)

    def test_merge_grant_is_structural_and_only_for_the_recorded_coordinator(self):
        fixture = kick_tests.KickContractTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        from dataclasses import replace
        config = replace(fixture.config, database=self.queue.path)
        self.create(merge_policy='merge', authority='Merge verified PRs for this example goal.')
        turn, revision = self.start_turn()
        task = self.queue.task(turn)
        prompt = dispatcher.render_prompt(config, task, 'account/manual/2000000000', 'alpha', None)
        self.assertIn('coordinator may merge', prompt)
        self.assertNotIn('never merge it', prompt)
        self.advance(turn, revision, tasks=[self.job('child')], wait_for=['child'])
        child = dispatcher.render_prompt(config, self.queue.task('child'), 'account/manual/2000000000', 'alpha', None)
        self.assertNotIn('coordinator may merge', child)
        forged = replace(task, goal='different goal')
        self.assertNotIn('coordinator may merge', dispatcher.render_prompt(
            config, forged, 'account/manual/2000000000', 'alpha', None))

    def test_coordinator_commands_are_bound_to_this_queue_and_config(self):
        fixture = kick_tests.KickContractTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        from dataclasses import replace
        import shlex
        config = replace(fixture.config, database=self.queue.path)
        self.create()
        turn, _ = self.start_turn()
        prompt = dispatcher.render_prompt(config, self.queue.task(turn), 'account/manual/2000000000', 'alpha', None)
        line = next(line for line in prompt.splitlines() if line.startswith('Goal read command: '))
        argv = shlex.split(line.removeprefix('Goal read command: '))
        self.assertEqual(argv[argv.index('--database') + 1], str(self.queue.path))
        self.assertEqual(argv[argv.index('--config') + 1], str(config.source_path))
        self.assertTrue(Path(argv[0]).is_absolute())

    def test_goal_hold_has_an_honest_readiness_reason(self):
        self.create()
        turn, revision = self.start_turn()
        self.advance(turn, revision, tasks=[self.job('a')], action='pause')
        self.finish(turn)
        status = self.queue.readiness('a')
        self.assertEqual(status['state'], 'waiting')
        self.assertIn('goal', status['reason'].lower())
        self.assertNotIn('recurrence', status['reason'])

    def test_steering_coalesces_without_replacing_the_active_owner(self):
        self.create()
        turn, revision = self.start_turn()
        self.store.steer('release', revision, 'Keep the first criterion', now=NOW)
        self.store.tick(now=NOW)
        self.assertEqual(self.store.show('release')['coordinator_task'], turn)
        with self.assertRaisesRegex(db.QueueError, 'conflict'):
            self.advance(turn, revision, tasks=[self.job('a')], wait_for=['a'])
        current = self.store.show('release')
        self.advance(turn, current['revision'], tasks=[self.job('a')], wait_for=['a'])
        self.finish(turn)
        self.store.steer('release', current['revision'] + 1, 'New integration decision', now=NOW)
        self.store.tick(now=NOW)
        self.assertEqual(self.store.show('release')['turn'], 2)

    def test_resume_can_extend_only_explicit_bounds_and_keeps_consumed_turns(self):
        self.create(max_turns=1)
        turn, revision = self.start_turn()
        self.advance(turn, revision, action='pause', summary='Need another turn')
        self.finish(turn)
        current = self.store.show('release')
        with self.assertRaises(db.QueueError):
            self.store.resume('release', current['revision'], 'Resume', now=NOW)
        self.store.resume('release', current['revision'], 'User grants two more turns', now=NOW, max_turns=3)
        self.store.tick(now=NOW)
        self.assertEqual(self.store.show('release')['turn'], 2)

    def test_resume_retires_a_coordinator_paused_before_any_dispatch(self):
        self.create()
        self.store.tick(now=NOW)
        current = self.store.show('release')
        old = current['coordinator_task']
        paused = self.store.steer('release', current['revision'], 'Hold', pause=True)
        self.assertFalse(self.queue.claim(old, 'account/manual/2000000000', 'alpha', 'account'))
        self.store.resume('release', paused['revision'], 'Continue', now=NOW)
        self.store.tick(now=NOW)
        resumed = self.store.show('release')
        self.assertNotEqual(resumed['coordinator_task'], old)
        self.assertFalse(self.queue.task(old).active)
        self.assertTrue(self.queue.claim(resumed['coordinator_task'], 'account/manual/2000000000', 'alpha', 'account'))

    def test_resume_retires_an_undispatched_coordinator_after_deadline(self):
        self.create()
        self.store.tick(now=NOW)
        self.store.tick(now=NOW + 3601)
        current = self.store.show('release')
        self.store.resume('release', current['revision'], 'New user deadline', now=NOW+3601, deadline=NOW+7200)
        self.store.tick(now=NOW+3601)
        self.assertEqual(self.store.show('release')['turn'], 2)

    def test_operation_intent_and_receipt_are_idempotent_and_stack_forbids_merge(self):
        self.create()
        turn, _ = self.start_turn()
        operation = {'key': 'join-1', 'intent': {'action': 'stack', 'heads': ['a' * 40]}}
        first = self.store.operation('release', turn, operation)
        self.assertEqual(self.store.operation('release', turn, operation), first)
        with self.assertRaises(db.QueueError):
            self.store.operation('release', turn, {'key': 'join-1', 'intent': {'action': 'stack', 'heads': ['b' * 40]}})
        with self.assertRaisesRegex(db.QueueError, 'merge'):
            self.store.operation('release', turn, {'key': 'merge-1', 'intent': {'action': 'merge', 'pr': 1}})
        operation['receipt'] = {'head': 'c' * 40}
        self.store.operation('release', turn, operation)
        self.assertEqual(self.store.show('release')['operations'][0]['receipt'], operation['receipt'])

    def test_bad_coordinator_routing_is_rejected_before_persisting_a_goal(self):
        for routing in ({'model': 'gpt-6-astra', 'required_capabilities': 42},
                        {'model': []}, {'model': 'gpt-6-astra', 'allowed_providers': 'alpha'},
                        {'model': 'gpt-6-astra', 'mcp': 12}):
            with self.subTest(routing=routing), self.assertRaises(db.QueueError):
                self.create(coordinator=routing)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.tick(now=NOW), [])

    def test_protected_jobs_reject_edits_through_real_queue_consumers(self):
        self.create(merge_policy='merge')
        self.store.tick(now=NOW)
        task_id = self.store.show('release')['coordinator_task']
        with self.assertRaises(db.QueueError):
            self.queue.edit_task(task_id, {'goal': 'Unrelated merges', 'constraints': 'Merge everything'})
        with self.assertRaises(db.QueueError):
            self.queue.set_model(task_id, 'different-model')
        turn, revision = self.start_turn()
        verifier = self.job('verify', use_implement=False)
        verifier.update(role='acceptance', candidate=CANDIDATE)
        self.advance(turn, revision, tasks=[verifier], wait_for=['verify'])
        self.finish(turn)
        with self.assertRaises(db.QueueError):
            self.queue.edit_task('verify', {'context': 'Verify a different candidate', 'cwd': '/tmp'})
        self.queue.set_priority('verify', 0)
        self.assertTrue(self.queue.readiness('verify')['ready'])

    def test_changed_persisted_acceptance_contract_cannot_supply_a_pass(self):
        import sqlite3
        self.create()
        turn, revision = self.start_turn()
        verifier = self.job('verify', use_implement=False)
        verifier.update(role='acceptance', candidate=CANDIDATE)
        self.advance(turn, revision, tasks=[verifier], wait_for=['verify'])
        self.finish(turn)
        self.finish('verify')
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute("UPDATE tasks SET context='wrong candidate' WHERE id='verify'")
        turn, revision = self.start_turn()
        with self.assertRaisesRegex(db.QueueError, 'contract'):
            self.advance(turn, revision, action='complete', observations=[{
                'criterion': 'journey', 'task_id': 'verify', 'outcome': 'pass', 'evidence': '/proofs/pass.json'}])

    def test_managed_failure_history_cannot_be_erased_with_requeue(self):
        self.create()
        turn, revision = self.start_turn()
        self.advance(turn, revision, tasks=[self.job('fix')], wait_for=['fix'])
        self.finish(turn)
        self.finish('fix', 'failed')
        with self.assertRaises(db.QueueError):
            self.queue.requeue('fix')
        self.assertEqual(self.queue.runs(task_id='fix')[0].status, 'failed')
        with self.assertRaises(db.QueueError):
            self.queue.requeue(turn)

    def test_shared_ancestor_join_is_evaluated_in_bounded_work(self):
        self.create()
        turn, revision = self.start_turn()
        jobs = [self.job('node-0'), self.job('node-1')]
        for index in range(2, 20):
            jobs.append(self.job(f'node-{index}', depends_on=[f'node-{index-1}', f'node-{index-2}']))
        self.advance(turn, revision, tasks=jobs, wait_for=['node-19'])
        self.finish(turn)
        statements = []
        original = self.queue._connect

        def traced():
            connection = original()
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch.object(self.queue, '_connect', side_effect=traced):
            self.store.tick(now=NOW)
        self.assertLess(sum(x.startswith('SELECT') for x in statements), 500)
        self.assertEqual(self.store.show('release')['turn'], 1)


class GoalScoutTests(unittest.TestCase):
    def test_scout_routes_one_turn_per_join_through_existing_dispatcher(self):
        fixture = kick_tests.KickContractTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        queue = fixture.queue
        queue.set_active('portable', False)
        config = replace(fixture.config, adapters=(replace(fixture.config.adapters[0], argv=('/bin/true',)),))
        store = goals.GoalStore(queue)
        store.create(dict(id='goal', title='Goal', cwd=str(fixture.root), outcome='Assembled proof',
                          authority='Fixture only', acceptance=[{'id': 'check', 'proof': 'Exercise the fixture'}],
                          merge_policy='stack', max_turns=5, max_inflight=2, deadline=NOW+3600,
                          coordinator={'model': 'gpt-6-astra'}), now=NOW)
        snapshots = {(provider, provider+'-account'): usage.UsageSnapshot(provider, provider+'-account', NOW,
                     {provider+'-weekly': {'used_percent': 20, 'resets_at': NOW+1000}}) for provider in ('alpha', 'beta')}
        calls = []

        def router(argv, **kwargs):
            calls.append(argv)
            return {'dispatch': {'job_id': f'fixture-job-{len(calls)}', 'launched': True}}

        def tick():
            return scout.run_once(config, queue, now_epoch=NOW, router_call=router)

        def finish(task):
            event = queue.runs(task_id=task)[0]
            queue.record(
                task, event.eligibility_key, attempt_id=event.attempt_id,
                status='done', provider_id=event.provider_id,
                outcome=verified_outcome(task),
            )

        with mock.patch.object(scout, 'read_all', return_value=snapshots), \
                mock.patch.object(dispatcher, 'record_factory_run', return_value=True):
            initial = tick()
            self.assertEqual(len(initial.dispatched), 1)
            turn = initial.dispatched[0].task_id
            goal = store.show('goal')
            tasks = [{'role': 'implementation', 'task': dict(id=x, title=x, cwd=str(fixture.root),
                       goal='Exercise the fixture', size='small', done_when='Fixture outcome', use_implement=True)}
                     for x in ('a', 'b')]
            store.advance('goal', turn, dict(expected_revision=goal['revision'], action='wait',
                          summary='Wait for both roots', tasks=tasks, wait_for=['a', 'b']), now=NOW)
            finish(turn)
            roots = tick()
            self.assertEqual({x.task_id for x in roots.dispatched}, {'a', 'b'})
            finish('a')
            launch_count = len(calls)
            for _ in range(5):
                self.assertEqual(tick().dispatched, ())
            self.assertEqual(len(calls), launch_count)
            self.assertEqual(store.show('goal')['turn'], 1)
            finish('b')
            next_turn = tick()
            self.assertEqual(len(next_turn.dispatched), 1)
            self.assertEqual(store.show('goal')['turn'], 2)
            self.assertNotEqual(next_turn.dispatched[0].task_id, turn)
            # No source skill or queue polling loop remains running after this fixture.
            finish(next_turn.dispatched[0].task_id)
            self.assertEqual(queue.inflight(), [])


if __name__ == '__main__':
    unittest.main()
