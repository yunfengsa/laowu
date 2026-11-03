"""Application security checks without invoking any model or real Codex run."""
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from laowu.common import now, process_identity
from laowu.runtime import Manager
from laowu.service import Service, validate_arguments


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # No scheduler work and no models. Exercise the same persisted state and
        # capability checks as the actual MCP transport.
        self.scheduler = patch.object(Manager, "_schedule", return_value=None)
        self.scheduler.start()
        self.service = Service(self.root)
        self.spec = {"id": "engineer", "name": "工程师", "description": "负责代码",
                     "instructions": "只处理当前任务", "skills": [],
                     "tools": ["memory", "checkpoint"], "can_write": False}
        self.service.call("friend_ensure", {"spec": self.spec})

    def tearDown(self):
        self.service.close()
        self.scheduler.stop()
        self.tmp.cleanup()

    def make_worker(self, task_id="task_a", run_id="run_a", token="test-run-token"):
        stamp = now()
        with self.service.store.transaction() as db:
            db.execute("""INSERT INTO tasks(id,session_id,friend_id,client,request,workspace,status,request_hash,created_at,updated_at)
                VALUES(?,?,'engineer','codex','test request',?,'running','hash',?,?)""",
                (task_id, self.service.manager.id, str(self.root), stamp, stamp))
            db.execute("""INSERT INTO runs(id,task_id,session_id,friend_id,friend_version,client,status,can_write,workspace,spec_json,token_hash,started_at,updated_at)
                VALUES(?,?,?,'engineer',1,'codex','running',0,?,'{}',?,?,?)""",
                (run_id, task_id, self.service.manager.id, str(self.root), hashlib.sha256(token.encode()).hexdigest(), stamp, stamp))
        return Service(self.root, worker_run=run_id, worker_token=token)

    def test_worker_actor_and_task_are_bound_to_token(self):
        worker = self.make_worker()
        for scope in ("personal", "shared", "task:task_a", "task:task_b", "friend:engineer", "friend:researcher"):
            self.service.call("memory_upsert", {"content": scope, "scope": scope})
        seen = worker.call("memory_search", {})
        self.assertEqual({m["scope"] for m in seen}, {"shared", "task:task_a", "friend:engineer"})
        self.assertEqual(worker.call("memory_search", {"scope": "personal"}), [])
        self.assertEqual(worker.call("memory_search", {"scope": "task:task_b"}), [])
        with self.assertRaises(ValueError):
            validate_arguments("memory_search", {"actor": "laowu"})
        with self.assertRaises(ValueError):
            validate_arguments("memory_search", {"task_id": "task_b"})
        with self.assertRaises((TypeError, ValueError, PermissionError)):
            worker.call("memory_search", {"actor": "laowu"})
        with self.assertRaises(ValueError):
            Service(self.root, worker_run="run_a", worker_token="wrong")

    def test_worker_cannot_delegate_write_memory_or_bootstrap(self):
        worker = self.make_worker()
        for name in ("task_start", "friend_ensure", "memory_upsert", "memory_forget", "bootstrap", "task_resume"):
            with self.subTest(name=name), self.assertRaises(PermissionError):
                worker.call(name, {})
        worker.call("task_checkpoint", {"task_id": "task_a", "summary": "已检查输入"})
        with self.assertRaises(PermissionError):
            worker.call("task_checkpoint", {"task_id": "task_b", "summary": "越权"})
        with self.service.store.transaction() as db:
            db.execute("UPDATE runs SET status='completed' WHERE id='run_a'")
        with self.assertRaises(ValueError):
            worker.call("memory_search", {})

    def test_secrets_cannot_be_saved_in_memory_or_summary(self):
        secret = "sk-" + "A1" * 20
        with self.assertRaises(ValueError):
            self.service.call("memory_upsert", {"content": secret})
        with self.assertRaises(ValueError):
            self.service.call("session_checkpoint", {"summary": secret})
        worker = self.make_worker()
        with self.assertRaises(ValueError):
            worker.call("task_checkpoint", {"task_id": "task_a", "summary": secret})

    def test_bootstrap_is_laowu_and_binds_host_session(self):
        self.service.call("memory_upsert", {"content": "希望中文沟通"})
        result = self.service.call("bootstrap", {"host_session_id": "codex-host-session"})
        self.assertEqual(result["actor"], "laowu")
        self.assertEqual(result["memories"][0]["content"], "希望中文沟通")
        self.assertEqual(result["friends"][0]["id"], "engineer")
        with self.service.store.connect() as db:
            self.assertEqual(db.execute("SELECT host_session_id FROM sessions WHERE id=?", (result["session_id"],)).fetchone()[0], "codex-host-session")

    def test_closed_manager_cannot_resume_or_submit_before_bootstrap(self):
        task = self.service.call('task_start', {'friend_id': 'engineer', 'request': '仅本地状态检查'})
        self.service.close()
        with self.service.store.connect() as db:
            before = dict(db.execute('SELECT * FROM tasks WHERE id=?', (task['id'],)).fetchone())
        for name, args in (
                ('task_resume', {'task_id': task['id']}),
                ('task_start', {'friend_id': 'engineer', 'request': '关闭后提交'})):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'bootstrap'):
                self.service.call(name, args)
        with self.service.store.connect() as db:
            after = dict(db.execute('SELECT * FROM tasks WHERE id=?', (task['id'],)).fetchone())
            self.assertEqual(after, before)
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)

    def test_database_closed_session_rejects_resume_before_scheduler_notices(self):
        task = self.service.call('task_start', {'friend_id': 'engineer', 'request': '关闭竞争'})
        self.service.call('task_cancel', {'task_id': task['id']})
        with self.service.store.transaction() as db:
            db.execute("UPDATE sessions SET status='closed' WHERE id=?", (self.service.manager.id,))
        self.assertFalse(self.service.manager.closed.is_set())
        with self.assertRaisesRegex(ValueError, 'bootstrap'):
            self.service.call('task_resume', {'task_id': task['id']})
        with self.service.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM tasks WHERE id=?', (task['id'],)).fetchone()[0], 'cancelled')

    def test_bootstrap_rebuilds_closed_manager_preserves_host_without_replaying_task(self):
        self.service.call('bootstrap', {'host_session_id': 'retained-host'})
        old = self.service.manager
        task = self.service.call('task_start', {'friend_id': 'engineer', 'request': '必须明确恢复'})
        self.service.close()
        recovered = self.service.call('bootstrap', {})
        self.assertNotEqual(recovered['session_id'], old.id)
        self.assertEqual(recovered['manager_recovery']['previous_status'], 'closed')
        again = self.service.call('bootstrap', {})
        self.assertEqual(again['session_id'], recovered['session_id'])
        with self.service.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM tasks WHERE id=?', (task['id'],)).fetchone()[0], 'interrupted')
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT host_session_id FROM sessions WHERE id=?', (recovered['session_id'],)).fetchone()[0], 'retained-host')
            self.assertEqual(db.execute('SELECT state FROM connections WHERE id=?', (old.id,)).fetchone()[0], 'closed')
        resumed = self.service.call('task_resume', {'task_id': task['id']})
        self.assertEqual((resumed['status'], resumed['session_id']), ('queued', recovered['session_id']))

    def test_bootstrap_reports_preserved_scheduler_diagnostic(self):
        old = self.service.manager
        old.failure_diagnostic = {'version': 1, 'last_error': {
            'source': 'scheduler', 'category': 'permission', 'basis': 'structured', 'os_errno': 13}}
        old.close(reason='scheduler_failed')
        recovered = self.service.call('bootstrap', {'host_session_id': 'new-host'})
        self.assertEqual(recovered['manager_recovery'], {
            'previous_session_id': old.id, 'previous_status': 'interrupted', 'diagnostics': old.failure_diagnostic})
        with self.service.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM sessions WHERE id=?', (old.id,)).fetchone()[0], 'interrupted')

    def test_parallel_bootstrap_creates_one_manager_and_no_tasks(self):
        self.service.manager.close()
        start = threading.Barrier(4)

        def recover():
            start.wait(timeout=5)
            return self.service.call('bootstrap', {})['session_id']

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: recover(), range(4)))
        self.assertEqual(len(set(results)), 1)
        with self.service.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sessions WHERE status='active'").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_failed_skill_validation_retains_previous_friend_version(self):
        before = self.service.registry.get("engineer")
        bad = dict(self.spec, skills=["not-installed"], instructions="replacement")
        with self.assertRaises(ValueError):
            self.service.call("friend_ensure", {"spec": bad})
        self.assertEqual(self.service.registry.get("engineer"), before)

    def test_failed_friend_file_write_retains_previous_version(self):
        before = self.service.registry.get("engineer")
        with patch("laowu.registry.atomic_write", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                self.service.call("friend_ensure", {"spec": dict(self.spec, instructions="new instructions")})
        self.assertEqual(self.service.registry.get("engineer"), before)

    def test_forget_fails_safely_while_cancelled_child_is_still_live(self):
        self.make_worker()
        fact = self.service.call("memory_upsert", {"content": "不能被运行任务写回的旧记忆"})
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with self.service.store.transaction() as db:
                db.execute("UPDATE runs SET child_pid=?,child_identity=? WHERE id='run_a'", (child.pid, process_identity(child.pid)))
            with patch("laowu.service.time.monotonic", side_effect=[0, 10]):
                with self.assertRaises(RuntimeError):
                    self.service.call("memory_forget", {"memory_id": fact["id"]})
            self.assertEqual(len(self.service.store.memory_search()), 1)
        finally:
            child.terminate()
            child.wait(timeout=5)
        result = self.service.call("memory_forget", {"memory_id": fact["id"]})
        self.assertEqual(result["deleted_count"], 1)

    def test_forgotten_task_cannot_be_resumed_with_empty_request(self):
        self.make_worker()
        fact = self.service.call("memory_upsert", {"content": "待遗忘"})
        self.service.call("memory_forget", {"memory_id": fact["id"]})
        with self.assertRaises(ValueError):
            self.service.call("task_resume", {"task_id": "task_a"})

    def test_background_task_requires_running_daemon_and_bound_origin(self):
        with self.assertRaises(RuntimeError):
            self.service.call('task_start', {'friend_id': 'engineer', 'request': '后台', 'background': True})
        with patch('laowu.background.daemon_status', return_value={'running': True}):
            with self.assertRaises(ValueError):
                self.service.call('task_start', {'friend_id': 'engineer', 'request': '后台', 'background': True})
            self.service.call('bootstrap', {'host_session_id': 'bound-origin'})
            task = self.service.call('task_start', {'friend_id': 'engineer', 'request': '后台', 'background': True, 'context': 'root'})
        with self.service.store.connect() as db:
            route = db.execute('SELECT * FROM async_tasks WHERE task_id=?', (task['id'],)).fetchone()
            self.assertEqual((route['origin_thread'], route['context']), ('bound-origin', 'root'))

    def test_worker_notification_cannot_choose_another_destination(self):
        from laowu.background import initialize_background
        initialize_background(self.service.store)
        worker = self.make_worker()
        with self.service.store.transaction() as db:
            db.execute("INSERT INTO async_tasks(task_id,origin_thread,context) VALUES('task_a','original','root')")
        event = worker.call('event_emit', {'message': '阶段完成', 'event_key': 'phase-one'})
        self.assertEqual(event['origin_thread'], 'original')
        self.assertEqual(event['context'], 'root')
        with self.assertRaises(PermissionError):
            worker.call('event_emit', {'message': '内容', 'event_key': 'phase-two', 'route': 'latest'})
        with self.assertRaises(PermissionError):
            worker.call('schedule_list', {})


if __name__ == "__main__":
    unittest.main()
