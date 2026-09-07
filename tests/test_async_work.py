"""Async queue contracts: shared eligibility, dependency safety and editable handoffs."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/bonus-drain/skills/bonus-drain'))
from bonus_drain import cli, db, dispatcher, scout, usage
from dataclasses import replace
from tests import test_bonus_drain_kick as kick_tests


class AsyncWorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = db.QueueDB(Path(self.tmp.name) / 'queue.db')
        self.add('a')

    def add(self, task_id, **values):
        return self.queue.add_task(dict(id=task_id, title=task_id, cwd='/tmp', goal='proof', **values))

    def finish(self, task_id, status='done'):
        return self.queue.record(task_id, 'account/manual/2000000000', status=status, provider_id='alpha')

    def test_every_ready_task_is_eligible_for_automatic_capacity(self):
        self.assertEqual(self.queue.count_eligible(0, automatic=True), 1)
        self.assertTrue(self.queue.claim('a', 'account/limit/2000000000', 'alpha', 'account', automatic=True))

    def test_cli_new_work_has_no_execution_mode_contract(self):
        with mock.patch.object(cli, '_json') as output:
            self.assertEqual(cli.main(['add', '--database', str(self.queue.path), '--id', 'new', '--title', 'New', '--kind', 'oneoff', '--size', 'small', '--cwd', '/tmp', '--goal', 'proof', '--json']), 0)
        self.assertNotIn('execution_mode', self.queue.task('new').to_dict())

    def test_all_dependencies_must_succeed_before_claim(self):
        self.add('b')
        self.add('child', depends_on=['a', 'b'])
        self.finish('a')
        self.assertEqual(self.queue.readiness('child')['state'], 'waiting')
        self.assertFalse(self.queue.claim('child', 'account/manual/2000000000', 'alpha', 'account'))
        self.finish('b')
        self.assertTrue(self.queue.readiness('child')['ready'])
        self.assertTrue(self.queue.claim('child', 'account/manual/2000000000', 'alpha', 'account'))

    def test_failed_and_skipped_do_not_satisfy_dependencies(self):
        for status in ('failed', 'skipped'):
            with self.subTest(status=status):
                parent = 'p-' + status
                self.add(parent)
                self.add('c-' + status, depends_on=[parent])
                self.finish(parent, status)
                self.assertFalse(self.queue.readiness('c-' + status)['ready'])

    def test_bad_dependencies_are_rejected_atomically(self):
        for dependencies in (['a'], ['missing']):
            with self.assertRaises(db.QueueError):
                self.queue.edit_task('a', {'title': 'should not change', 'depends_on': dependencies})
        self.assertEqual(self.queue.task('a').title, 'a')
        self.add('b', depends_on=['a'])
        self.add('c', depends_on=['b'])
        with self.assertRaisesRegex(db.QueueError, 'cycle'):
            self.queue.edit_task('a', {'depends_on': ['c']})
        self.add('weekly', kind='recurring')
        with self.assertRaisesRegex(db.QueueError, 'one-off'):
            self.add('bad', depends_on=['weekly'])
        self.assertIsNone(self.queue.task('bad'))

    def test_edit_cannot_change_claimed_or_completed_contract(self):
        self.assertTrue(self.queue.claim('a', 'account/manual/2000000000', 'alpha', 'account'))
        with self.assertRaises(db.QueueError):
            self.queue.edit_task('a', {'goal': 'different'})
        self.finish('a')
        with self.assertRaises(db.QueueError):
            self.queue.edit_task('a', {'goal': 'different'})

    def test_stale_dispatch_contract_cannot_claim_after_edit(self):
        old = self.queue.task('a')
        self.queue.edit_task('a', {'goal': 'new contract'})
        self.assertFalse(self.queue.claim('a', 'account/manual/2000000000', 'alpha', 'account', expected_task=old))

    def test_handoff_roundtrip_and_validation(self):
        task = self.queue.edit_task('a', {'source_ref': 'https://example.test/plan', 'work_group': 'Release'})
        self.assertEqual(task.legacy_contract_dict()['work_group'], 'Release')
        for changes in ({'execution_mode': 'bonus'}, {'priority': True}, {'depends_on': 'a'}, {'goal': ''}, {'cwd': None}):
            with self.assertRaises(db.QueueError):
                self.queue.edit_task('a', changes)

    def test_work_group_is_a_compact_navigation_label(self):
        self.queue.edit_task('a', {'work_group': 'Curie v0.8.7'})
        self.assertEqual(self.queue.edit_task('a', {'work_group': 'soak-obs'}).work_group, 'Soak Obs')
        with self.assertRaisesRegex(db.QueueError, 'at most 15 characters'):
            self.queue.edit_task('a', {'work_group': 'Curie v0.8.7 hardening'})
        with self.assertRaisesRegex(db.QueueError, 'at most 15 characters'):
            self.add('long-group', work_group='Curie v0.8.7 hardening')

    def test_run_trigger_propagates_to_terminal_and_legacy_stays_unknown(self):
        self.queue.record('a', 'account/manual/2000000000', status='dispatched', provider_id='alpha', trigger='manual')
        self.assertEqual(self.finish('a').trigger, 'manual')
        self.add('old')
        self.assertIsNone(self.finish('old').trigger)

    def test_migration_keeps_existing_work_metadata(self):
        with sqlite3.connect(self.queue.path) as connection:
            for column in ('source_ref', 'work_group', 'depends_on_json'):
                connection.execute(f'ALTER TABLE tasks DROP COLUMN {column}')
        self.queue.initialize()
        self.assertIsNone(self.queue.task('a').source_ref)

    def test_legacy_execution_column_is_ignored(self):
        with sqlite3.connect(self.queue.path) as connection:
            connection.execute("ALTER TABLE tasks ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'manual'")
        self.queue.initialize()
        self.assertNotIn('execution_mode', self.queue.task('a').to_dict())


class DispatchReadinessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = kick_tests.KickContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_waiting_dependency_never_calls_router_or_activation(self):
        f = self.fixture
        f.queue.add_task(dict(id='child', title='child', cwd='/tmp', goal='proof', depends_on=['portable']))
        router, activation = mock.Mock(), mock.Mock()
        with self.assertRaises(dispatcher.AlreadyClaimed):
            dispatcher.dispatch(f.config, f.queue, task_id='child', eligibility_key='alpha-account/manual/2000000000', requested_provider='auto', router_call=router, activation_call=activation)
        router.assert_not_called()
        activation.assert_not_called()

    def test_automatic_trigger_accepts_any_ready_task(self):
        f = self.fixture
        result = dispatcher.dispatch(f.config, f.queue, task_id='portable', eligibility_key='alpha-account/limit/2000000000', requested_provider='alpha', trigger='bonus', router_call=f._router)
        self.assertEqual(result.task_id, 'portable')


class ScoutWorkflowTests(unittest.TestCase):
    def test_automatic_capacity_sees_all_ready_work_and_records_bonus_origin(self):
        fixture = kick_tests.KickContractTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        queue = fixture.queue
        queue.add_task(dict(id='waiting', title='Waiting', cwd='/tmp', goal='proof', depends_on=['portable']))
        queue.add_task(dict(id='other-ready', title='Ready', cwd='/tmp', goal='proof'))
        self.assertEqual({task.id for task in queue.eligible_tasks(0, automatic=True)}, {'portable', 'other-ready'})
        config = replace(fixture.config, adapters=(replace(fixture.config.adapters[0], argv=('/bin/true',)),))
        snapshots = {(provider, provider+'-account'): usage.UsageSnapshot(provider, provider+'-account', kick_tests.NOW,
            {provider+'-weekly': {'used_percent': 20, 'resets_at': kick_tests.NOW+1000}}) for provider in ('alpha', 'beta')}
        with mock.patch.object(scout, 'read_all', return_value=snapshots):
            report = scout.run_once(config, queue, now_epoch=kick_tests.NOW, router_call=fixture._router)
        self.assertEqual({result.task_id for result in report.dispatched}, {'portable', 'other-ready'})
        self.assertEqual(queue.runs(task_id='portable')[0].trigger, 'bonus')
        self.assertEqual(queue.runs(task_id='other-ready')[0].trigger, 'bonus')
        self.assertEqual(queue.runs(task_id='waiting'), [])
