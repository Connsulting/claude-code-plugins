"""HTTP and rendering proof for monitoring, dependency visibility, and preview isolation."""
import json
import threading
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from unittest import mock
from types import SimpleNamespace

from tests import test_bonus_drain_jobs_viewer as viewer_tests
from tests import test_bonus_drain_kick as kick_tests
from bonus_drain import dispatcher


class AsyncViewerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = kick_tests.KickContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.viewer = viewer_tests._load_server()
        self.viewer.DB_PATH = self.fixture.queue.path
        self.viewer.MUTATIONS_ENABLED = True
        self.viewer.PREVIEW = True
        self.viewer.ALLOWED_HOSTS = ('viewer.example.test',)
        self.viewer.ALLOWED_ORIGINS = ('https://viewer.example.test',)
        self.patch = mock.patch.object(self.viewer.graph_config, 'load_config', return_value=self.fixture.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.viewer.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, payload, **headers):
        return viewer_tests.JobsViewerContractTests._request(self.server, 'POST', path,
            headers={'Host': 'viewer.example.test', 'Origin': 'https://viewer.example.test',
                     'Content-Type': 'application/json', **headers}, body=json.dumps(payload).encode())




    def test_preview_rejects_http_and_shared_dispatch_before_external_calls(self):
        with mock.patch.object(self.viewer, 'kick_task') as launch:
            status, _, body = self.post('/api/bonus/task/run', {'id': 'portable', 'engine': 'auto'})
            self.assertEqual(status, 400)
            self.assertIn(b'Preview', body)
            launch.assert_not_called()
        callback = mock.Mock()
        with self.assertRaisesRegex(dispatcher.InvalidRoute, 'Preview'):
            dispatcher.dispatch(replace(self.fixture.config, viewer={'preview': True}), self.fixture.queue,
                task_id='portable', eligibility_key='alpha-account/manual/2000000000',
                requested_provider='auto', router_call=callback, activation_call=callback)
        callback.assert_not_called()

    def test_waiting_tasks_are_visible_even_without_eligible_provider_ids(self):
        payload = {'tasks': [{'id': 'child', 'title': 'Child', 'priority': 2, 'kind': 'oneoff'}],
                   'eligible_task_ids': [], 'eligible_provider_ids': {},
                   'compatible_provider_ids': {'child': ['claude']},
                   'readiness': {'child': {'state': 'waiting', 'ready': False, 'reason': 'Waiting for parent'}}}
        with mock.patch.object(
            self.viewer.subprocess, 'run',
            return_value=mock.Mock(returncode=0, stdout=json.dumps(payload), stderr=''),
        ):
            tasks = self.viewer._remaining_snapshot(0)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['readiness']['state'], 'waiting')
        self.assertEqual(tasks[0]['eligible_providers'], [])
        self.assertEqual(tasks[0]['compatible_providers'], ['claude'])
        self.assertEqual(self.viewer._facet_providers(tasks[0]), ['claude'])
        self.viewer.PREVIEW = False
        buttons = self.viewer._run_buttons(tasks[0])
        self.assertEqual(buttons.count(' disabled'), 4)
        self.assertIn('Waiting for parent', buttons)

    def test_graph_queue_timeout_is_explicit_and_never_uses_legacy_pick(self):
        legacy = mock.Mock(
            returncode=0,
            stdout=json.dumps([{'id': 'fabricated_ready', 'priority': 0, 'kind': 'oneoff'}]),
            stderr='',
        )
        timeout = self.viewer.subprocess.TimeoutExpired(cmd=['bonusdb', 'queue'], timeout=15)
        with mock.patch.object(
            self.viewer.subprocess, 'run', side_effect=[timeout, legacy],
        ) as run:
            with self.assertRaisesRegex(RuntimeError, 'queue'):
                self.viewer.get_remaining(0)

        self.assertEqual(run.call_count, 1)

    def test_graph_queue_nonzero_exit_is_explicit_and_sanitizes_stderr(self):
        result = mock.Mock(
            returncode=7,
            stdout=json.dumps({
                'tasks': [], 'eligible_task_ids': [], 'eligible_provider_ids': {},
            }),
            stderr='credential=do_not_expose',
        )
        with mock.patch.object(self.viewer.subprocess, 'run', return_value=result) as run:
            with self.assertRaises(RuntimeError) as raised:
                self.viewer.get_remaining(0)

        self.assertEqual(run.call_count, 1)
        self.assertNotIn('do_not_expose', str(raised.exception))

    def test_malformed_graph_queue_shape_is_explicit_and_never_fabricates_ready_work(self):
        malformed = mock.Mock(returncode=0, stdout=json.dumps([]), stderr='')
        legacy = mock.Mock(
            returncode=0,
            stdout=json.dumps([{'id': 'fabricated_ready', 'priority': 0, 'kind': 'oneoff'}]),
            stderr='',
        )
        with mock.patch.object(
            self.viewer.subprocess, 'run', side_effect=[malformed, legacy],
        ) as run:
            with self.assertRaisesRegex(RuntimeError, 'queue'):
                self.viewer.get_remaining(0)

        self.assertEqual(run.call_count, 1)

    def test_missing_or_empty_task_readiness_is_rejected(self):
        for readiness in ({}, {'task': {}}):
            with self.subTest(readiness=readiness):
                payload = {
                    'tasks': [{'id': 'task', 'priority': 2, 'kind': 'oneoff'}],
                    'eligible_task_ids': [], 'eligible_provider_ids': {},
                    'readiness': readiness,
                }
                result = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr='')
                with mock.patch.object(self.viewer.subprocess, 'run', return_value=result):
                    with self.assertRaisesRegex(RuntimeError, 'queue'):
                        self.viewer._remaining_snapshot(0)

    def test_malformed_task_readiness_contract_is_rejected(self):
        malformed_statuses = (
            {'state': 'unknown', 'ready': False, 'reason': 'Unknown state'},
            {'state': 'ready', 'ready': False, 'reason': 'Contradictory ready'},
            {'state': 'waiting', 'ready': True, 'reason': 'Contradictory waiting'},
            {'state': 'waiting', 'ready': False},
            {'state': 'waiting', 'ready': False, 'reason': ''},
        )
        for status in malformed_statuses:
            with self.subTest(status=status):
                payload = {
                    'tasks': [{'id': 'task', 'priority': 2, 'kind': 'oneoff'}],
                    'eligible_task_ids': [], 'eligible_provider_ids': {},
                    'readiness': {'task': status},
                }
                result = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr='')
                with mock.patch.object(self.viewer.subprocess, 'run', return_value=result):
                    with self.assertRaisesRegex(RuntimeError, 'queue'):
                        self.viewer._remaining_snapshot(0)

    def test_queue_failure_surfaces_on_page_and_api(self):
        error = RuntimeError('bonus queue command failed')
        with (
            mock.patch.object(self.viewer, 'render_bonus_body', side_effect=error),
            mock.patch.object(self.viewer, 'render_schedule_body', return_value='scheduled body'),
        ):
            page = self.viewer.render_page().decode()
        self.assertIn('error: bonus queue command failed', page)
        self.assertIn('scheduled body', page)

        with (
            mock.patch.object(self.viewer, 'get_usage', return_value=None),
            mock.patch.object(self.viewer, 'current_cycle', return_value=0),
            mock.patch.object(self.viewer, 'get_remaining', side_effect=error),
        ):
            status, _, body = viewer_tests.JobsViewerContractTests._request(
                self.server, 'GET', '/api/bonus',
                headers={'Host': 'viewer.example.test'},
            )
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {'error': 'bonus queue command failed'})

    def test_authoritative_ready_waiting_and_cooldown_render_with_readiness_filter(self):
        task_ids = ('ready', 'waiting', 'cooldown')
        tasks = [
            {
                'id': task_id, 'title': task_id.title(), 'priority': 2,
                'kind': 'oneoff', 'size': 'small', 'cwd': '/tmp',
                'goal': f'Keep {task_id} visible', 'created_at': str(index),
            }
            for index, task_id in enumerate(task_ids)
        ]
        payload = {
            'tasks': tasks,
            'eligible_task_ids': ['ready'],
            'eligible_provider_ids': {'ready': ['claude']},
            'compatible_provider_ids': {task_id: ['claude'] for task_id in task_ids},
            'readiness': {
                task_id: {
                    'state': task_id, 'ready': task_id == 'ready',
                    'reason': f'{task_id} reason', 'dependencies': [],
                }
                for task_id in task_ids
            },
        }
        result = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr='')
        with mock.patch.object(self.viewer.subprocess, 'run', return_value=result):
            remaining = self.viewer._remaining_snapshot(0)

        self.assertEqual([task['id'] for task in remaining], list(task_ids))
        filters = self.viewer._queue_filters(remaining)
        self.assertIn('aria-label="filter by readiness"', filters)
        for state in task_ids:
            self.assertIn(f'data-value="{state}"', filters)
            self.assertEqual(
                next(task for task in remaining if task['id'] == state)['readiness']['state'],
                state,
            )

    def test_recovering_backoff_held_and_exhausted_tasks_stay_visible_with_recovery_context(self):
        states = {
            'recovering': {
                'state': 'recovering', 'ready': False, 'reason': 'Recovery attempt is running',
                'attempt': {'ordinal': 2, 'mode': 'retry'},
                'recovery': {'state': 'consumed', 'blocked_descendants': 4},
            },
            'backoff': {
                'state': 'backoff', 'ready': False, 'reason': 'Retryable tests failed',
                'attempt': {'ordinal': 1, 'mode': 'normal'},
                'recovery': {
                    'state': 'backoff', 'mode': 'retry',
                    'not_before': '2033-05-18T03:38:20Z', 'blocked_descendants': 3,
                },
            },
            'held': {
                'state': 'held', 'ready': False, 'reason': 'Repository identity is unavailable',
                'attempt': {'ordinal': 2, 'mode': 'verification'},
                'recovery': {
                    'state': 'held', 'reason_code': 'dependency_ref_unavailable',
                    'blocked_descendants': 2,
                },
            },
            'exhausted': {
                'state': 'exhausted', 'ready': False, 'reason': 'Automatic recovery limit reached',
                'attempt': {'ordinal': 3, 'mode': 'retry'},
                'recovery': {'state': 'exhausted', 'blocked_descendants': 1},
            },
        }
        tasks = [{
            'id': task_id, 'title': task_id.title(), 'priority': 2, 'kind': 'oneoff',
            'cwd': '/tmp', 'goal': 'Retain this recovery in the queue',
        } for task_id in states]
        payload = {
            'tasks': tasks,
            'eligible_task_ids': [],
            'eligible_provider_ids': {},
            'compatible_provider_ids': {task_id: ['alpha'] for task_id in states},
            'readiness': states,
        }
        with mock.patch.object(
            self.viewer.subprocess, 'run',
            return_value=mock.Mock(returncode=0, stdout=json.dumps(payload), stderr=''),
        ):
            remaining = self.viewer._remaining_snapshot(0)

        self.assertEqual({item['id'] for item in remaining}, set(states))
        self.assertTrue(all(item['compatible_providers'] == ['alpha'] for item in remaining))
        rendered = {item['id']: self.viewer._work_meta(item) for item in remaining}
        self.assertIn('attempt 2', rendered['recovering'].lower())
        self.assertIn('retry', rendered['recovering'].lower())
        self.assertIn('2033-05-18T03:38:20Z', rendered['backoff'])
        self.assertIn('3 blocked descendants', rendered['backoff'])
        self.assertIn('dependency_ref_unavailable', rendered['held'])
        self.assertIn('Automatic recovery limit reached', rendered['exhausted'])

    def test_run_log_disables_requeue_when_the_projected_action_is_unsafe(self):
        runs = [
            {
                'ts': '2026-09-14T12:00:00Z', 'task': 'ordinary', 'title': 'Ordinary',
                'kind': 'oneoff', 'status': 'failed', 'engine': 'codex', 'cycle': 1,
                'summary': 'retryable', 'branch': None,
                'requeue': {'allowed': True, 'reason': 'Operator retry is available'},
            },
            {
                'ts': '2026-09-14T11:00:00Z', 'task': 'authority', 'title': 'Authority',
                'kind': 'oneoff', 'status': 'failed', 'engine': 'claude', 'cycle': 1,
                'summary': 'authority required', 'branch': None,
                'requeue': {'allowed': False, 'reason': 'Authority is required'},
            },
            {
                'ts': '2026-09-14T10:00:00Z', 'task': 'ambiguous', 'title': 'Ambiguous',
                'kind': 'oneoff', 'status': 'failed', 'engine': 'claude', 'cycle': 1,
                'summary': 'launch unknown', 'branch': None,
                'requeue': {'allowed': False, 'reason': 'Launch ownership is ambiguous'},
            },
            {
                'ts': '2026-09-14T09:00:00Z', 'task': 'goal-member', 'title': 'Goal member',
                'kind': 'oneoff', 'status': 'skipped', 'engine': 'claude', 'cycle': 1,
                'summary': 'fresh verifier required', 'branch': None,
                'requeue': {'allowed': False, 'reason': 'Fresh goal follow-up required'},
            },
        ]
        with (
            mock.patch.object(self.viewer, 'get_usage', return_value=None),
            mock.patch.object(self.viewer, 'get_codex_usage', return_value=None),
            mock.patch.object(self.viewer, 'get_grok_usage', return_value=None),
            mock.patch.object(self.viewer, 'current_cycle', return_value=1),
            mock.patch.object(self.viewer, 'get_remaining', return_value=[]),
            mock.patch.object(self.viewer, 'get_recent_runs', return_value=runs),
            mock.patch.object(self.viewer, 'get_disabled', return_value=[]),
            mock.patch.object(self.viewer, 'get_inflight', return_value=[]),
            mock.patch.object(self.viewer, 'get_gates', return_value={'coordinator': 'none'}),
            mock.patch.object(self.viewer, '_claude_cards', return_value=[]),
            mock.patch.object(self.viewer, '_codex_cards', return_value=[]),
            mock.patch.object(self.viewer, '_grok_cards', return_value=[]),
            mock.patch.object(self.viewer, '_verdict', return_value=('idle', 'idle', 'idle', '')),
            mock.patch.object(self.viewer, 'get_dispatch_times', return_value=[]),
        ):
            body = self.viewer.render_bonus_body()

        ordinary = body[body.index('data-task-id="ordinary"'):]
        self.assertNotIn('disabled', ordinary.split('</button>', 1)[0])
        for task_id, reason in (
            ('authority', 'Authority is required'),
            ('ambiguous', 'Launch ownership is ambiguous'),
            ('goal-member', 'Fresh goal follow-up required'),
        ):
            button = body[body.index(f'data-task-id="{task_id}"'):].split('</button>', 1)[0]
            self.assertIn('disabled', button)
            self.assertIn(reason, button)

    def test_explicit_empty_compatibility_does_not_enable_fallback_providers(self):
        self.viewer.PREVIEW = False
        buttons = self.viewer._run_buttons({'id': 'x', 'eligible_providers': [], 'readiness': {'ready': True}})
        self.assertEqual(buttons.count(' disabled'), 4)
        self.assertIn('No compatible provider', buttons)

    def test_source_and_group_render_as_text_without_script_links(self):
        task = {'id': 'x', 'source_ref': 'javascript:alert(1)', 'work_group': '<script>alert(1)</script>',
                'readiness': {'state': 'ready', 'dependencies': []}}
        html = self.viewer._work_meta(task)
        self.assertNotIn('href=', html)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        task['source_ref'] = 'https://[malformed'
        self.assertNotIn('href=', self.viewer._work_meta(task))
        task['source_ref'] = 'https://example.test/plan'
        self.assertIn('rel="noopener noreferrer"', self.viewer._work_meta(task))



    def test_browser_has_no_contract_edit_endpoint(self):
        payload = {'id': 'portable', 'changes': {'goal': 'not allowed from this UI'}}
        status, _, _ = self.post('/api/bonus/task/edit', payload)
        self.assertEqual(status, 404)
        self.assertEqual(self.fixture.queue.task('portable').goal, 'run portable')

    def test_run_control_rejects_cross_origin_without_launching(self):
        with mock.patch.object(self.viewer, 'kick_task') as launch:
            status, _, _ = self.post('/api/bonus/task/run', {'id': 'portable', 'engine': 'auto'}, Origin='https://other.example')
        self.assertEqual(status, 403)
        launch.assert_not_called()

    def test_preview_usage_comes_from_copied_cache_without_account_credentials(self):
        cfg = mock.Mock()
        cfg.accounts_for_provider.side_effect = lambda provider: [SimpleNamespace(id=provider+'-account', plan_id=provider+'-plan')]
        cfg.limits = [SimpleNamespace(id=p+'-weekly', plan_id=p+'-plan', window_seconds=604800, ceiling_percent=95) for p in ('claude','codex')]
        snapshots = {(p,p+'-account'): SimpleNamespace(limits={p+'-weekly': {'used_percent':42,'resets_at':2000000000}}) for p in ('claude','codex')}
        gates = {}
        with mock.patch.object(self.viewer.graph_usage, 'read_all', return_value=snapshots) as read:
            self.viewer._preview_account_gates(cfg, gates)
        read.assert_called_once_with(cfg)
        self.assertEqual(gates['acct'][0]['u7'], 42)
        self.assertEqual(gates['codex_acct'][0]['r7'], 2000000000)
