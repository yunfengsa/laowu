"""Slack scope checks use the real SQLite service and scheduler claim path."""
import json
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from laowu.access import SlackAccess, cancel_input_tasks, task_access
from laowu.runtime import Manager, task_record
from laowu.service import Service
from laowu.slack_state import SlackState
from laowu.store import Store


class SlackAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scheduler = patch.object(Manager, '_schedule', return_value=None)
        self.scheduler.start()
        self.addCleanup(self.scheduler.stop)
        self.gui = Service(self.root)
        self.addCleanup(self.gui.close)
        self.state = SlackState(self.gui.store)
        for ident, tools in [('researcher', ['read', 'web', 'memory', 'checkpoint']),
                             ('confidant', ['memory', 'checkpoint']), ('engineer', ['shell', 'memory'])]:
            self.gui.registry.ensure(dict(id=ident, name=ident, description='test', instructions='test',
                                          skills=[], tools=tools, can_write=False))
        self.a, self.input_a = self.scope('CA')
        self.b, self.input_b = self.scope('CB')
        self.private, self.input_p = self.scope('DP', personal=True)

    def scope(self, channel, personal=False):
        self.state.update_channel('TA', channel, personal=int(personal), eligible=1)
        path = self.state.issue_binding('TA', channel)
        service = Service(self.root, slack_token_file=path)
        self.addCleanup(service.close)
        record = self.state.enqueue('TA', channel, '1.00', 'task', reply_ts='0.99')
        self.state.update_input(record['id'], status='running')
        return service, record['id']

    def task(self, service=None, **extra):
        return (service or self.a).call('task_start', dict(friend_id='confidant', request='test', **extra))

    def worker(self, service, task):
        claimed = service.manager._claim()
        self.assertEqual(claimed[0]['id'], task['id'])
        return Service(self.root, worker_run=claimed[2], worker_token=claimed[3]), claimed

    def test_token_file_permissions_symlinks_and_stable_identity(self):
        path = self.state.issue_binding('TA', 'CA')
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            SlackAccess(self.gui.store, path)
        path.chmod(0o600)
        alias = path.parent / 'alias'
        alias.symlink_to(path)
        with self.assertRaises(PermissionError):
            SlackAccess(self.gui.store, alias)
        with self.assertRaises(PermissionError):
            self.a.call('bootstrap', {'host_session_id': 'GUI_OTHER'})
        self.assertEqual(self.a._host_thread(), 'slack:TA:CA:0')
        self.assertEqual(self.a.call('bootstrap', {})['conversation_id'], 'slack:TA:CA:0')

    def test_actual_scoped_stdio_mcp_rejects_rebinding_and_revocation(self):
        self.gui.store.memory_upsert('GUI PERSONAL CANARY', scope='personal')
        self.gui.store.memory_upsert('PUBLIC CANARY', scope='shared')
        path = self.state.issue_binding('TA', 'CA')
        script = Path(__file__).resolve().parents[1] / 'scripts/laowu.py'
        process = subprocess.Popen([sys.executable, str(script), '--root', str(self.root),
                                    'mcp', '--slack-token-file', str(path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        def rpc(ident, name, args):
            request = dict(jsonrpc='2.0', id=ident, method='tools/call', params=dict(name=name, arguments=args))
            process.stdin.write((json.dumps(request) + '\n').encode())
            process.stdin.flush()
            self.assertTrue(selector.select(timeout=5), 'scoped MCP response timed out')
            return json.loads(process.stdout.readline())['result']
        try:
            result = rpc(1, 'memory_search', {})
            self.assertFalse(result['isError'])
            self.assertIn('PUBLIC CANARY', json.dumps(result))
            self.assertNotIn('GUI PERSONAL CANARY', json.dumps(result))
            self.assertTrue(rpc(2, 'bootstrap', {'host_session_id': 'unrelated-gui'})['isError'])
            with self.gui.store.transaction() as db:
                db.execute('UPDATE slack_bindings SET revoked=1')
            self.assertTrue(rpc(3, 'memory_search', {})['isError'])
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            selector.close()
            process.stdout.close()
            process.stderr.close()

    def test_revocation_generation_and_eligibility_checked_every_call(self):
        for column, value in [('revoked', 1), ('generation', 1), ('eligible', 0)]:
            with self.subTest(column=column):
                with self.gui.store.transaction() as db:
                    if column == 'revoked':
                        db.execute('UPDATE slack_bindings SET revoked=? WHERE token_hash=?', (value, self.a.access.token_hash))
                    else:
                        db.execute('UPDATE slack_channels SET ' + column + '=? WHERE channel_id=?', (value, 'CA'))
                with self.assertRaises(PermissionError):
                    self.a.call('memory_search', {})
                with self.gui.store.transaction() as db:
                    db.execute('UPDATE slack_bindings SET revoked=0')
                    db.execute("UPDATE slack_channels SET generation=0,eligible=1 WHERE channel_id='CA'")

    def test_default_scopes_origin_metadata_and_hidden_id_mutations(self):
        shared = self.a.call('memory_upsert', {'content': 'shared record'})
        private = self.private.call('memory_upsert', {'content': 'private record'})
        self.assertEqual(shared['scope'], 'shared')
        self.assertEqual(private['scope'], 'personal')
        self.assertEqual(shared['metadata']['conversation_id'], 'slack:TA:CA:0')
        self.assertEqual([m['id'] for m in self.a.call('memory_search', {})], [shared['id']])
        for name, args in [
            ('memory_upsert', {'memory_id': private['id'], 'content': 'stolen', 'scope': 'shared'}),
            ('memory_upsert', {'content': 'derived leak', 'derived_from': [private['id']]}),
            ('memory_forget', {'memory_id': private['id']}),
            ('session_checkpoint', {'summary': 'leak', 'memory_ids': [private['id']]})]:
            with self.subTest(name=name), self.assertRaises(PermissionError):
                self.a.call(name, args)
        with self.assertRaises(PermissionError):
            self.private.call('memory_upsert', {'memory_id': private['id'], 'content': 'widen', 'scope': 'shared'})
        self.assertEqual(self.a.call('memory_search', {'scope': 'personal'}), [])

    def test_bootstrap_only_own_summary_and_granted_tasks(self):
        own = self.task()
        other = self.task(self.b)
        self.gui.call('session_checkpoint', {'summary': 'GUI PRIVATE SUMMARY'})
        self.b.call('session_checkpoint', {'summary': 'OTHER CHANNEL SUMMARY'})
        self.a.call('session_checkpoint', {'summary': 'OWN SUMMARY'})
        data = self.a.call('bootstrap', {})
        encoded = json.dumps(data)
        self.assertNotIn('GUI PRIVATE', encoded)
        self.assertNotIn('OTHER CHANNEL', encoded)
        self.assertEqual([t['id'] for t in data['tasks']], [own['id']])
        self.assertNotIn(other['id'], encoded)

    def test_task_methods_and_memory_references_require_grant(self):
        own = self.task()
        other = self.task(self.b)
        for name in ('task_status', 'task_wait', 'task_resume', 'task_checkpoint', 'task_cancel'):
            args = {'task_id': other['id']}
            if name == 'task_checkpoint':
                args['summary'] = 'wrong'
            with self.subTest(name=name), self.assertRaises(PermissionError):
                self.a.call(name, args)
        fact = self.gui.store.memory_upsert('other task', scope='task:' + other['id'])
        with self.assertRaises(PermissionError):
            self.a.call('task_checkpoint', {'task_id': own['id'], 'summary': 'test', 'memory_ids': [fact['id']]})
        self.assertEqual(self.a.call('task_status', {'task_id': own['id']})['id'], own['id'])

    def test_grant_atomic_before_claim_and_resume_preserves_origin(self):
        task = self.task(idempotency_key='same-key')
        with self.gui.store.connect() as db:
            grant = dict(db.execute('SELECT * FROM slack_task_grants WHERE task_id=?', (task['id'],)).fetchone())
        self.assertEqual(grant['input_id'], self.input_a)
        self.assertEqual(grant['reply_thread'], '0.99')
        self.assertEqual(self.task(idempotency_key='same-key')['id'], task['id'])
        other = self.task(self.b, idempotency_key='same-key')
        self.assertNotEqual(other['id'], task['id'])
        self.a.call('task_cancel', {'task_id': task['id']})
        self.a.call('task_resume', {'task_id': task['id']})
        self.assertEqual(task_access(self.gui.store, task['id']), grant)
        claimed = self.a.manager._claim()
        self.assertEqual(task_access(self.gui.store, claimed[0]['id']), grant)

    def test_shared_confidant_role_intersects_origin_and_own_task(self):
        task = self.task()
        worker, claimed = self.worker(self.a, task)
        other = self.task(self.b)
        for scope in ['shared', 'personal', 'tasks', 'friend:confidant', 'task:' + task['id'], 'task:' + other['id']]:
            self.gui.store.memory_upsert(scope, scope=scope)
        self.assertEqual({r['scope'] for r in worker.call('memory_search', {})}, {'shared', 'task:' + task['id']})
        hidden = self.gui.store.memory_upsert('private', scope='personal')
        with self.assertRaises(PermissionError):
            worker.call('task_checkpoint', {'task_id': task['id'], 'summary': 'x', 'memory_ids': [hidden['id']]})
        self.state.update_channel('TA', 'CA', generation=1)
        with self.assertRaises(PermissionError):
            worker.call('memory_search', {})

    def test_durable_task_grant_survives_gateway_stdio_token_rotation(self):
        task = self.task()
        with self.gui.store.transaction() as db:
            db.execute('UPDATE slack_bindings SET revoked=1')
        self.assertEqual(task_access(self.gui.store, task['id'])['conversation_id'], 'slack:TA:CA:0')
        with self.assertRaises(PermissionError):
            self.a.call('task_status', {'task_id': task['id']})

    def test_private_confidant_gets_personal_but_researcher_does_not(self):
        self.gui.store.memory_upsert('private', scope='personal')
        task = self.task(self.private)
        worker, _ = self.worker(self.private, task)
        self.assertIn('personal', {r['scope'] for r in worker.call('memory_search', {})})
        task = self.private.call('task_start', {'friend_id': 'researcher', 'request': 'research'})
        worker, _ = self.worker(self.private, task)
        self.assertNotIn('personal', {r['scope'] for r in worker.call('memory_search', {})})

    def test_shared_shell_rejected_and_researcher_shell_removed(self):
        with patch('laowu.slack_permissions.probe', return_value=False):
            with self.assertRaisesRegex(PermissionError, '隔离探针未通过'):
                self.a.call('task_start', {'friend_id': 'engineer', 'request': 'shell'})
        task = self.a.call('task_start', {'friend_id': 'researcher', 'request': 'web'})
        claimed = self.a.manager._claim()
        self.assertEqual(claimed[0]['id'], task['id'])
        self.assertNotIn('read', claimed[1]['tools'])
        self.assertNotIn('shell', claimed[1]['tools'])

    def test_stop_only_current_input_synchronous_tasks(self):
        sync = self.task()
        with patch('laowu.background.daemon_status', return_value={'running': True}):
            asynchronous = self.task(background=True)
        record = self.state.enqueue('TA', 'CA', '2.00', 'next')
        self.state.update_input(self.input_a, status='completed')
        self.state.update_input(record['id'], status='running')
        next_task = self.task()
        cancel_input_tasks(self.gui.store, 'slack:TA:CA:0', self.input_a)
        self.assertEqual(task_record(self.gui.store, sync['id'])['status'], 'cancelled')
        self.assertEqual(task_record(self.gui.store, asynchronous['id'])['status'], 'queued')
        self.assertEqual(task_record(self.gui.store, next_task['id'])['status'], 'queued')

    def test_notifications_filtered_and_latest_forbidden(self):
        a = self.a.call('event_emit', {'message': 'A', 'event_key': 'same'})
        b = self.b.call('event_emit', {'message': 'B', 'event_key': 'same'})
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual([r['id'] for r in self.a.call('notification_list', {})], [a['id']])
        with self.assertRaises(PermissionError):
            self.a.call('notification_retry', {'notification_id': b['id']})
        with self.assertRaises(PermissionError):
            self.a.call('event_emit', {'message': 'bad', 'event_key': 'bad', 'route': 'latest'})

    def test_schedule_operations_filter_existing_and_propagate_task_grants(self):
        from laowu.scheduling import ScheduleBook
        from datetime import timedelta
        spec = dict(id='schedule_a', name='调查', friend_id='researcher', request='research',
                    workspace=str(self.root), trigger={'type': 'interval', 'seconds': 10})
        result = self.a.call('schedule_upsert', {'spec': spec})
        self.assertEqual(result['schedule']['id'], spec['id'])
        self.assertEqual([s['id'] for s in self.a.call('schedule_list', {})], [spec['id']])
        self.assertEqual(self.b.call('schedule_list', {}), [])
        with self.assertRaises(PermissionError):
            self.b.call('schedule_upsert', {'spec': spec})
        with self.assertRaises(PermissionError):
            self.a.call('schedule_enable', {'schedule_id': 'other', 'enabled': True})
        with self.assertRaises(PermissionError):
            self.a.call('schedule_delete', {'schedule_id': 'other'})
        self.a.call('schedule_enable', {'schedule_id': spec['id'], 'enabled': False})
        self.a.call('schedule_enable', {'schedule_id': spec['id'], 'enabled': True})
        book = ScheduleBook(self.gui.store)
        from laowu.scheduling import _now
        book.due(_now() + timedelta(seconds=11))
        occurrence = book.pending()[0]
        manager = Manager(self.gui.store, background=True)
        self.addCleanup(manager.close)
        task = manager.submit(occurrence['friend_id'], occurrence['request'], occurrence['workspace'],
                              'schedule:' + occurrence['id'], detached=True, origin_thread=None)
        grant = task_access(self.gui.store, task['id'])
        self.assertEqual(grant['conversation_id'], 'slack:TA:CA:0')
        with self.gui.store.connect() as db:
            self.assertIsNone(db.execute('SELECT origin_thread FROM async_tasks WHERE task_id=?', (task['id'],)).fetchone()[0])
        self.a.call('schedule_delete', {'schedule_id': spec['id']})

    def test_schedule_publication_failure_does_not_leave_a_hijacking_grant(self):
        spec = dict(id='schedule_failed', name='test', friend_id='confidant', request='test',
                    workspace=str(self.root), trigger={'type': 'interval', 'seconds': 10})
        with patch('laowu.scheduling.ScheduleBook.upsert', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.a.call('schedule_upsert', {'spec': spec})
        with self.gui.store.connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM slack_schedule_grants WHERE schedule_id=?', (spec['id'],)).fetchone())

    def test_forget_revokes_threads_queues_and_task_material(self):
        task = self.task()
        fact = self.gui.store.memory_upsert('forget me', scope='shared')
        self.state.bind_thread('TA', 'CA', 'OLD_PRIVATE_CODEX_THREAD')
        self.state.queue_output(self.input_a, 'private queued reply')
        result = self.a.call('memory_forget', {'memory_id': fact['id']})
        self.assertEqual(result['deleted_count'], 1)
        with self.assertRaises(PermissionError):
            self.a.call('bootstrap', {})
        self.assertIsNone(self.state.channel('TA', 'CA')['thread_id'])
        self.assertEqual(self.state.inbox(), [])
        self.assertEqual(self.state.outputs(), [])
        self.assertEqual(task_record(self.gui.store, task['id'])['request'], '')
        # Metadata migration remains readable; forgetting did not delete its version.
        SlackState(self.gui.store)

    def test_backup_excludes_binding_tokens_restore_invalidates_slack_context(self):
        task = self.task()
        spec = dict(id='schedule_restore', name='private schedule', friend_id='confidant', request='PRIVATE SCHEDULE CANARY',
                    workspace=str(self.root), trigger={'type': 'interval', 'seconds': 10})
        self.a.call('schedule_upsert', {'spec': spec})
        snapshot = self.gui.store.backup()
        with zipfile.ZipFile(snapshot['path']) as archive:
            self.assertFalse(any('binding-' in n for n in archive.namelist()))
            database = self.root / 'snapshot.sqlite3'
            database.write_bytes(archive.read('state.sqlite3'))
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM slack_bindings').fetchone()[0], 0)
        for service in (self.a, self.b, self.private, self.gui):
            service.close()
        self.gui.store.restore(snapshot['path'])
        self.assertTrue(self.state.inbox())
        self.assertEqual(task_record(self.gui.store, task['id'])['status'], 'interrupted')
        self.assertEqual(task_record(self.gui.store, task['id'])['request'], 'test')
        self.assertEqual(self.state.channel('TA', 'CA')['generation'], 1)
        with self.gui.store.connect() as db:
            row = db.execute('SELECT * FROM schedules WHERE id=?', (spec['id'],)).fetchone()
            self.assertEqual(row['enabled'], 0)
            self.assertEqual(json.loads(row['spec_json'])['request'], 'PRIVATE SCHEDULE CANARY')
            self.assertEqual(db.execute('SELECT count(*) FROM schedule_occurrences').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
