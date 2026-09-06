"""HTTP and rendering proof for task edits, dependency visibility, and preview isolation."""
import json
import threading
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from unittest import mock

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

    def test_edit_persists_contract_and_rejects_cross_origin_without_writing(self):
        payload = {'id': 'portable', 'changes': {'execution_mode': 'manual', 'work_group': 'New work'}}
        status, _, _ = self.post('/api/bonus/task/edit', payload, Origin='https://other.example')
        self.assertEqual(status, 403)
        self.assertIsNone(self.fixture.queue.task('portable').work_group)
        status, _, _ = self.post('/api/bonus/task/edit', payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.fixture.queue.task('portable').work_group, 'New work')

    def test_dependency_failure_rolls_back_entire_edit(self):
        status, _, body = self.post('/api/bonus/task/edit', {'id': 'portable', 'changes': {'title': 'Changed', 'depends_on': ['missing']}})
        self.assertEqual(status, 400)
        self.assertIn(b'unknown prerequisite', body)
        self.assertEqual(self.fixture.queue.task('portable').title, 'portable')

    def test_edit_body_has_a_bounded_larger_limit(self):
        status, _, _ = self.post('/api/bonus/task/edit', {'id': 'portable', 'changes': {'context': 'x' * 5000}})
        self.assertEqual(status, 200)
        status, _, _ = self.post('/api/bonus/task/edit', {'id': 'portable', 'changes': {'context': 'y' * 65536}})
        self.assertEqual(status, 400)
        self.assertEqual(self.fixture.queue.task('portable').context, 'x' * 5000)

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
                   'readiness': {'child': {'state': 'waiting', 'ready': False, 'reason': 'Waiting for parent'}}}
        with mock.patch.object(self.viewer.subprocess, 'run', return_value=mock.Mock(stdout=json.dumps(payload))):
            tasks = self.viewer._remaining_snapshot(0)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['readiness']['state'], 'waiting')
        self.viewer.PREVIEW = False
        buttons = self.viewer._run_buttons(tasks[0])
        self.assertEqual(buttons.count(' disabled'), 4)
        self.assertIn('Waiting for parent', buttons)

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

    def test_queued_editor_keeps_dependency_options_identifiable(self):
        html = self.viewer._editor_dialog()
        self.assertIn('value="portable"', html)
        self.assertIn('name="depends_on" multiple', html)
        self.assertIn('id="dependency-search"', html)
        self.assertIn("dialog[open]", self.viewer.SCRIPT)

    def test_invalid_mode_type_is_a_client_error(self):
        status, _, body = self.post('/api/bonus/task/edit', {'id': 'portable', 'changes': {'execution_mode': []}})
        self.assertEqual(status, 400)
        self.assertIn(b'execution_mode must be', body)
