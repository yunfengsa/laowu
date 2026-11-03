"""Routing, durable delivery and lifecycle checks. Never call a real model."""
import json
import fcntl
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from laowu.background import Outbox, Presence, initialize_background
from laowu.common import now, process_identity
from laowu.daemon import Dispatcher
from laowu.registry import Registry
from laowu.runtime import Manager, initialize_runtime, reconcile, task_record
from laowu.scheduling import ScheduleBook
from laowu.service import Service
from laowu.store import Store


class FakeBridge:
    def __init__(self):
        self.states = {}
        self.sent = []
        self.receipts = {}
        self.fail_send = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def loaded(self):
        return set(self.states)

    def state(self, thread):
        return self.states[thread], {}

    def send(self, event):
        self.sent.append(dict(event))
        if self.fail_send:
            raise TimeoutError('ambiguous')
        return 'turn-' + event['id']

    def receipt(self, event):
        return self.receipts.get(event['id'], ('submitted', 'turn-' + event['id']))


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='老五后台-')
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        initialize_runtime(self.store)
        self.box = Outbox(self.store)
        self.presence = Presence(self.store)
        self.bridge = FakeBridge()
        self.dispatcher = Dispatcher(self.store, lambda: self.bridge)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self, name, *, connected_at=None):
        self.presence.connect('connection-' + name, name, mode='lease', ttl=30,
                              connected_at=connected_at)
        self.bridge.states[name] = 'idle'

    def ready(self, event):
        with self.store.transaction() as db:
            db.execute('UPDATE notifications SET next_attempt=0 WHERE id=?', (event['id'],))

    def test_latest_route_waits_offline_survives_restart_then_delivers(self):
        event = self.box.publish('中文结果', event_key='once')
        self.dispatcher.tick()
        self.assertEqual(self.bridge.sent, [])
        self.connect('old', connected_at='2020-01-01T00:00:00+00:00')
        self.connect('new', connected_at='2020-01-02T00:00:00+00:00')
        Dispatcher(Store(self.root), lambda: self.bridge).tick()
        self.assertEqual(self.bridge.sent[0]['target_thread'], 'new')
        self.assertEqual(self.box.get(event['id'])['status'], 'submitted')
        self.ready(event)
        self.bridge.receipts[event['id']] = ('delivered', 'confirmed-turn')
        self.dispatcher.tick()
        self.assertEqual(self.box.get(event['id'])['status'], 'delivered')
        self.dispatcher.tick()
        self.assertEqual(len(self.bridge.sent), 1)

    def test_origin_never_moves_to_other_online_thread(self):
        event = self.box.publish('专属结果', event_key='origin', origin_thread='origin')
        self.connect('other')
        self.dispatcher.tick()
        self.assertEqual(self.bridge.sent, [])
        self.connect('origin')
        self.dispatcher.tick()
        self.assertEqual(self.bridge.sent[0]['target_thread'], 'origin')
        self.assertEqual(self.box.get(event['id'])['origin_thread'], 'origin')

    def test_closed_lease_overrides_lingering_native_connection(self):
        self.presence.connect('native', 'one')
        self.connect('one')
        self.presence.disconnect('connection-one')
        self.assertEqual(self.presence.online({'one'}), [])
        self.connect('two')
        self.assertEqual(self.presence.online({'one', 'two'}), ['two'])

    def test_heartbeat_does_not_change_recency(self):
        # Explicit times avoid ties from the production millisecond clock.
        self.connect('old', connected_at='2020-01-01T00:00:00+00:00')
        self.connect('new', connected_at='2020-01-02T00:00:00+00:00')
        self.presence.connect('connection-old', 'old', mode='lease', ttl=30)
        self.assertEqual(self.presence.online({'old', 'new'}), ['new', 'old'])
        self.presence.disconnect('connection-old')
        self.presence.connect('connection-old', 'old', mode='lease', ttl=30)
        self.assertEqual(self.presence.online({'old', 'new'}), ['old', 'new'])

    def test_fresh_native_connection_can_reopen_after_lease_disconnect(self):
        self.connect('one')
        self.presence.disconnect('connection-one')
        future = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.presence.connect('native-reopened', 'one', connected_at=future)
        self.assertEqual(self.presence.online({'one'}), ['one'])

    def test_lock_timeout_does_not_stall_connection_shutdown(self):
        descriptor = os.open(self.store.private / 'store.lock', os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                with self.store.transaction(timeout=0.04):
                    self.fail('锁仍被占用')
            self.assertLess(time.monotonic() - start, 0.5)
        finally:
            os.close(descriptor)

    def test_busy_thread_keeps_event_local_until_idle(self):
        self.connect('one')
        self.bridge.states['one'] = 'active'
        event = self.box.publish('完成', event_key='busy', context='root')
        self.dispatcher.tick()
        self.assertEqual(self.box.get(event['id'])['status'], 'pending')
        self.bridge.states['one'] = 'idle'
        self.ready(event)
        self.dispatcher.tick()
        self.assertEqual(self.bridge.sent[0]['context'], 'root')

    def test_ambiguous_send_is_reconciled_never_blindly_replayed(self):
        self.connect('one')
        event = self.box.publish('完成', event_key='uncertain')
        self.bridge.fail_send = True
        self.dispatcher.tick()
        self.assertEqual(self.box.get(event['id'])['status'], 'uncertain')
        self.ready(event)
        self.bridge.receipts[event['id']] = ('uncertain', None)
        self.dispatcher.tick()
        self.assertEqual(len(self.bridge.sent), 1)
        self.ready(event)
        self.bridge.receipts[event['id']] = ('delivered', 'found-in-history')
        self.dispatcher.tick()
        self.assertEqual(self.box.get(event['id'])['status'], 'delivered')
        self.assertEqual(len(self.bridge.sent), 1)

    def test_duplicate_event_and_different_payload(self):
        first = self.box.publish('事件', event_key='same')
        self.assertEqual(first['id'], Outbox(Store(self.root)).publish('事件', event_key='same')['id'])
        with self.assertRaises(ValueError):
            self.box.publish('其他事件', event_key='same')

    def test_forget_clears_events_and_schedules(self):
        Registry(self.store).ensure({'id': 'engineer', 'name': '测试', 'description': '测试',
                                    'instructions': '测试', 'skills': [], 'tools': ['memory'], 'can_write': False})
        book = ScheduleBook(self.store)
        book.upsert({'id': 'plan', 'name': '旧内容', 'friend_id': 'engineer', 'request': '旧内容',
                     'workspace': str(self.root), 'trigger': {'type': 'interval', 'seconds': 60}})
        fact = self.store.memory_upsert('旧内容')
        self.box.publish('旧内容', event_key='old')
        self.store.memory_forget(fact['id'])
        self.assertEqual(book.list(), [])
        self.assertEqual(self.box.list()[0]['message'], '')
        self.assertEqual(self.box.list()[0]['status'], 'cancelled')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM background_meta WHERE key='paused'").fetchone()[0], '1')

    def test_restore_clears_presence_pauses_background_remaps_workspace(self):
        Registry(self.store).ensure({'id': 'engineer', 'name': '测试', 'description': '测试',
                                    'instructions': '测试', 'skills': [], 'tools': ['memory'], 'can_write': False})
        workspace = self.root / '中文目录'
        workspace.mkdir()
        ScheduleBook(self.store).upsert({'id': 'plan', 'name': '计划', 'friend_id': 'engineer', 'request': '测试',
                                       'workspace': str(workspace), 'trigger': {'type': 'interval', 'seconds': 60}})
        self.connect('one')
        event = self.box.publish('结果', event_key='restore')
        self.dispatcher.tick()
        backup = self.store.backup()
        with tempfile.TemporaryDirectory(prefix='恢复中文-') as target:
            restored = Store(target)
            restored.restore(backup['path'])
            self.assertEqual(Presence(restored).online({'one'}), [])
            self.assertEqual(Outbox(restored).get(event['id'])['status'], 'uncertain')
            self.assertEqual(ScheduleBook(restored).list()[0]['workspace'], str(Path(target) / '中文目录'))
            with restored.connect() as db:
                self.assertEqual(db.execute("SELECT value FROM background_meta WHERE key='paused'").fetchone()[0], '1')

    def test_detached_queue_survives_origin_close_and_reconcile(self):
        with patch.object(Manager, '_schedule', return_value=None):
            origin = Manager(self.store)
            Registry(self.store).ensure({'id': 'engineer', 'name': '测试', 'description': '测试',
                                        'instructions': '测试', 'skills': [], 'tools': ['memory'], 'can_write': False})
            task = origin.submit('engineer', '后台任务', detached=True, origin_thread='origin')
            origin.close()
            reconcile(self.store)
            self.assertEqual(task_record(self.store, task['id'])['status'], 'queued')
            worker = Manager(self.store, background=True)
            try:
                claimed = worker._claim()
                self.assertEqual(claimed[0]['id'], task['id'])
                with self.store.transaction() as db:
                    db.execute("UPDATE tasks SET status='completed',result='{}' WHERE id=?", (task['id'],))
                    db.execute("UPDATE runs SET status='completed' WHERE task_id=?", (task['id'],))
                self.box.collect_tasks()
                self.box.collect_tasks()
                self.assertEqual(len(self.box.list()), 1)
                self.assertEqual(self.box.list()[0]['origin_thread'], 'origin')
            finally:
                worker.close()

    def test_paused_background_rejects_stale_submission_after_forget(self):
        with patch.object(Manager, '_schedule', return_value=None):
            owner = Manager(self.store)
            try:
                Registry(self.store).ensure({'id': 'engineer', 'name': '测试', 'description': '测试',
                                            'instructions': '测试', 'skills': [], 'tools': ['memory'], 'can_write': False})
                fact = self.store.memory_upsert('需要遗忘的内容')
                self.store.memory_forget(fact['id'])
                with self.assertRaisesRegex(RuntimeError, '后台已暂停'):
                    owner.submit('engineer', '缓存中仍有的旧任务', detached=True)
                with self.store.connect() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
            finally:
                owner.close()

    def test_recovery_does_not_resume_interrupted_task_with_live_owner(self):
        from laowu.daemon import recover_interrupted
        with patch.object(Manager, '_schedule', return_value=None):
            owner = Manager(self.store, background=True)
            try:
                Registry(self.store).ensure({'id': 'engineer', 'name': '测试', 'description': '测试',
                                            'instructions': '测试', 'skills': [], 'tools': ['memory'], 'can_write': False})
                task = owner.submit('engineer', '显式中断', detached=True)
                with self.store.transaction() as db:
                    db.execute("UPDATE tasks SET status='interrupted',error='interrupted' WHERE id=?", (task['id'],))
                recover_interrupted(self.store)
                self.assertEqual(task_record(self.store, task['id'])['status'], 'interrupted')
            finally:
                owner.close()


if __name__ == '__main__':
    unittest.main()
