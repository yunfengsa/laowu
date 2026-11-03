import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from laowu.slack_state import SlackState, conversation_id
from laowu.store import Store
from laowu.cli import parser, execute


class SlackStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='老五Slack-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.state = SlackState(self.store)
        self.state.set_config({'team_id': 'T01', 'owner_user_id': 'U01'})

    def test_channel_identity_is_persistent_and_threads_do_not_split_it(self):
        a = self.state.channel('T01', 'C01', True)
        self.state.engage('T01', 'C01', '1.0001')
        self.state.engage('T01', 'C01', '2.0002')
        b = SlackState(Store(self.temp.name)).channel('T01', 'C01')
        self.assertEqual(conversation_id(a), conversation_id(b))
        self.assertTrue(self.state.engaged('T01', 'C01', '1.0001'))
        self.assertFalse(self.state.engaged('T01', 'C02', '1.0001'))

    def test_dual_events_and_retries_are_one_durable_input(self):
        first = self.state.enqueue('T01', 'C01', '1.01', '中文消息', event_id='Ev1')
        twice = self.state.enqueue('T01', 'C01', '1.01', '中文消息', event_id='Ev2')
        again = self.state.enqueue('T01', 'C01', '1.01', '中文消息', event_id='Ev1')
        self.assertEqual(first['id'], twice['id'])
        self.assertTrue(again['duplicate'])
        self.assertEqual(len(SlackState(Store(self.temp.name)).inbox()), 1)

    def test_restart_quarantines_work_and_never_requeues_unknown_send(self):
        item = self.state.enqueue('T01', 'C01', '1.1', '任务')
        self.state.update_input(item['id'], status='processing', turn_id='turn1')
        out = self.state.queue_output(item['id'], '结果', key='result')
        self.state.update_output(out['id'], status='sending')
        self.state.recover()
        self.assertEqual(self.state.inbox()[0]['status'], 'uncertain')
        self.assertEqual(self.state.outputs()[0]['status'], 'uncertain')
        self.assertEqual(self.state.inbox()[0]['turn_id'], 'turn1')

    def test_output_idempotency_rejects_changed_body(self):
        item = self.state.enqueue('T01', 'C01', '1', '你好')
        out = self.state.queue_output(item['id'], '结果', key='p1')
        self.assertEqual(out['id'], self.state.queue_output(item['id'], '结果', key='p1')['id'])
        with self.assertRaises(ValueError):
            self.state.queue_output(item['id'], '另一内容', key='p1')

    def test_reset_revokes_principal_and_clears_unsent_private_payload(self):
        self.state.update_channel('T01', 'C01', personal=1, thread_id='native1', summary='私密摘要')
        token = self.state.issue_binding('T01', 'C01')
        self.assertEqual(token.stat().st_mode & 0o777, 0o600)
        item = self.state.enqueue('T01', 'C01', '1', '私密输入')
        self.state.queue_output(item['id'], '私密输出')
        changed = self.state.reset('T01', 'C01')
        self.assertEqual(changed['generation'], 1)
        self.assertIsNone(changed['thread_id'])
        self.assertEqual(changed['summary'], '')
        self.assertEqual(self.state.outputs()[0]['body'], '')
        self.assertEqual(self.state.inbox()[0]['status'], 'cancelled')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT revoked FROM slack_bindings').fetchone()[0], 1)

    def test_secrets_never_in_database_backup_or_status(self):
        creds = {'app_token': 'xapp-test-secret', 'bot_token': 'xoxb-test-secret'}
        self.state.save_credentials(creds)
        self.assertEqual(self.state.credentials(), creds)
        snapshot = self.store.backup()
        with zipfile.ZipFile(snapshot['path']) as archive:
            self.assertFalse(any('credentials' in n for n in archive.namelist()))
            for name in archive.namelist():
                self.assertNotIn(b'xoxb-test-secret', archive.read(name))
        self.assertNotIn('xoxb', json.dumps(self.state.status()))
        os.chmod(self.state.directory / 'credentials.json', 0o644)
        with self.assertRaises(PermissionError):
            self.state.credentials()

    def test_cli_personal_is_request_only_and_does_not_grant_reads(self):
        args = parser().parse_args(['slack', 'channels', 'C01', '--mode', 'direct', '--personal', 'yes'])
        result = execute(args, self.store.root)
        self.assertEqual(result['mode'], 'direct')
        self.assertEqual(result['private_approved'], 1)
        self.assertEqual(result['personal'], 0)
        self.assertEqual(execute(parser().parse_args(['slack', 'doctor']), self.store.root)['credentials'], 'missing')
        with self.assertRaises(FileNotFoundError):
            execute(parser().parse_args(['slack', 'enable']), self.store.root)

    def test_identity_cannot_be_replaced_and_secret_files_no_symlinks(self):
        with self.assertRaises(ValueError):
            self.state.set_config({'team_id': 'T02'})
        target = self.store.root / 'elsewhere'
        target.write_text('{}')
        (self.state.directory / 'credentials.json').symlink_to(target)
        with self.assertRaises(ValueError):
            self.state.credentials()


if __name__ == '__main__':
    unittest.main()
