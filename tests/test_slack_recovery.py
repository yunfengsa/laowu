"""Cross-layer recovery contracts: restart/restore are distinct from forgetting."""
import json
import tempfile
import unittest
from pathlib import Path

from laowu.access import SlackAccess
from laowu.background import Outbox
from laowu.common import now
from laowu.scheduling import ScheduleBook
from laowu.slack_notifications import route_slack_notifications
from laowu.slack_state import SlackState, conversation_id
from laowu.store import Store


class SlackRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='老五恢复测试-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / '原工作空间')
        self.state = SlackState(self.store)
        self.state.set_config({'team_id': 'TA', 'owner_user_id': 'UA', 'enabled': True})
        self.state.update_channel('TA', 'DA', personal=1, last_activity=10, summary='保留频道摘要')
        self.state.runtime(status='connected')
        self.origin = conversation_id(self.state.channel('TA', 'DA'))
        self.input = self.state.enqueue('TA', 'DA', '10.000001', '用户任务', reply_ts='9.000001')
        self.old_token = self.state.issue_binding('TA', 'DA')
        with self.store.transaction() as db:
            db.execute("INSERT INTO sessions(id,client,host_session_id,status,opened_at,updated_at) VALUES('source','codex',?,'closed',?,?)", (self.origin, now(), now()))

    def task(self, ident, *, asynchronous=False, status='queued'):
        with self.store.transaction() as db:
            db.execute('INSERT INTO tasks(id,session_id,friend_id,client,request,workspace,status,checkpoint,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, 'source', 'confidant', 'codex', '需要保留的任务请求', str(self.store.root), status,
                        json.dumps({'summary': '已经完成第一步', 'artifacts': ['中文产物']}), ident, now(), now()))
            db.execute('INSERT INTO slack_task_grants VALUES(?,?,?,?,?)', (ident, self.origin, 'personal', self.input['reply_ts'], self.input['id']))
            if asynchronous:
                db.execute('INSERT INTO async_tasks(task_id,origin_thread,context) VALUES(?,?,?)', (ident, self.origin, 'normal'))
        return ident

    def schedule(self):
        ScheduleBook(self.store)
        spec = {'id': 'saved_schedule', 'name': '保留计划', 'friend_id': 'confidant',
                'request': '每天做一项检查', 'workspace': str(self.store.root),
                'trigger': {'type': 'interval', 'seconds': 600}, 'timezone': 'Asia/Shanghai',
                'enabled': True, 'context': 'normal'}
        with self.store.transaction() as db:
            db.execute('INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?)',
                       ('saved_schedule', json.dumps(spec), 1, 1, now(), now(), None, now(), now()))
            db.execute('INSERT INTO slack_schedule_grants VALUES(?,?,?)', ('saved_schedule', self.origin, 'personal'))
        return spec

    def test_reset_preserves_async_reply_migrates_grants_and_cancels_sync(self):
        asynchronous = self.task('async_task', asynchronous=True)
        synchronous = self.task('sync_task')
        self.schedule()
        channel = self.state.reset('TA', 'DA')
        following = conversation_id(channel)
        self.assertEqual(following, 'slack:TA:DA:1')
        with self.store.connect() as db:
            async_task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (asynchronous,)).fetchone())
            sync_task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (synchronous,)).fetchone())
            grant = dict(db.execute('SELECT * FROM slack_task_grants WHERE task_id=?', (asynchronous,)).fetchone())
            async_origin = db.execute('SELECT origin_thread FROM async_tasks WHERE task_id=?', (asynchronous,)).fetchone()[0]
            schedule_origin = db.execute('SELECT conversation_id FROM slack_schedule_grants').fetchone()[0]
        self.assertEqual(async_task['status'], 'queued')
        self.assertEqual(async_task['request'], '需要保留的任务请求')
        self.assertEqual(sync_task['status'], 'cancelled')
        self.assertEqual(grant['conversation_id'], following)
        self.assertEqual(grant['reply_thread'], '9.000001')
        self.assertEqual(async_origin, self.origin)
        self.assertEqual(schedule_origin, following)
        with self.assertRaises(PermissionError):
            SlackAccess(self.store, self.old_token)

    def test_reset_rearms_queued_notice_once_with_original_reply(self):
        ident = self.task('async_complete', asynchronous=True, status='completed')
        box = Outbox(self.store)
        notice = box.publish('任务已完成', event_key='reset-queued-notice', origin_thread=self.origin, task_id=ident)
        self.assertEqual(route_slack_notifications(self.store), 1)
        first = next(row for row in self.state.inbox() if row['origin_notice'] == notice['id'])
        self.assertEqual(first['reply_ts'], '9.000001')
        self.state.reset('TA', 'DA')
        self.assertEqual(box.get(notice['id'])['status'], 'pending')
        self.assertEqual(route_slack_notifications(self.store), 1)
        second = next(row for row in self.state.inbox() if row['origin_notice'] == notice['id'])
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(second['generation'], 1)
        self.assertEqual(second['status'], 'pending')
        self.assertEqual(second['reply_ts'], '9.000001')
        self.assertEqual(route_slack_notifications(self.store), 0)
        self.assertEqual(sum(row['origin_notice'] == notice['id'] for row in self.state.inbox()), 1)

    def test_backup_restore_retains_task_summary_schedule_and_revokes_identity(self):
        ident = self.task('saved_task', asynchronous=True)
        spec = self.schedule()
        self.store.memory_upsert('保留共享偏好', scope='shared')
        snapshot = self.store.backup()
        restored = Store(self.root / '恢复后的中文空间')
        restored.restore(snapshot['path'])
        state = SlackState(restored)
        channel = state.channel('TA', 'DA')
        self.assertEqual(channel['summary'], '保留频道摘要')
        self.assertEqual(channel['generation'], 1)
        self.assertEqual(channel['eligible'], 0)
        self.assertIsNone(channel['thread_id'])
        self.assertFalse(state.config()['enabled'])
        with restored.connect() as db:
            task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone())
            schedule = dict(db.execute('SELECT * FROM schedules WHERE id=?', ('saved_schedule',)).fetchone())
            self.assertEqual(db.execute("SELECT value FROM background_meta WHERE key='paused'").fetchone()[0], '1')
            self.assertTrue(all(row[0] for row in db.execute('SELECT revoked FROM slack_bindings')))
            self.assertEqual(db.execute('SELECT conversation_id FROM slack_task_grants').fetchone()[0], 'slack:TA:DA:1')
            self.assertEqual(db.execute('SELECT conversation_id FROM slack_schedule_grants').fetchone()[0], 'slack:TA:DA:1')
        self.assertEqual(task['request'], '需要保留的任务请求')
        self.assertEqual(json.loads(task['checkpoint'])['summary'], '已经完成第一步')
        self.assertEqual(task['status'], 'interrupted')
        self.assertEqual(json.loads(schedule['spec_json'])['request'], spec['request'])
        self.assertEqual(schedule['enabled'], 0)
        self.assertIsNone(schedule['deleted_at'])
        self.assertEqual(restored.memory_search('保留共享偏好')[0]['content'], '保留共享偏好')

    def test_forget_scrubs_derived_material_instead_of_restore_behavior(self):
        ident = self.task('forgotten_task', asynchronous=True, status='completed')
        self.schedule()
        memory = self.store.memory_upsert('需要遗忘的记忆', scope='shared')
        self.store.memory_forget(memory['id'])
        with self.store.connect() as db:
            task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone())
            self.assertEqual(task['request'], '')
            self.assertEqual(task['checkpoint'], '{}')
            self.assertEqual(db.execute('SELECT summary FROM slack_channels').fetchone()[0], '')
            self.assertEqual(db.execute('SELECT count(*) FROM slack_task_grants').fetchone()[0], 0)
            schedule = dict(db.execute('SELECT * FROM schedules').fetchone())
            self.assertEqual(schedule['spec_json'], '{}')
            self.assertIsNotNone(schedule['deleted_at'])


if __name__ == '__main__':
    unittest.main()
