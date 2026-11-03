"""Exercise real scheduling/guardians with local fake Codex JSONL only.

The only model seam patched is prepare_worker. Guardians, pipes, process groups,
SQLite state, reconciliation and JSON handoffs all run as in normal operation.
"""
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from laowu.adapters import AdapterError, WorkerLaunch
from laowu.common import process_alive
from laowu.registry import Registry
from laowu.runtime import Manager, cancel_task, checkpoint, reconcile, task_record
from laowu.store import Store


FAKE_CLI = r'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path

control, run_id, wait_mode, output_mode, child_mode = sys.argv[1:]
control = Path(control)
control.mkdir(parents=True, exist_ok=True)
prompt = sys.stdin.read()
child = None
if child_mode == "child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
started = control / (run_id + ".started.json")
temporary = control / (run_id + ".tmp")
temporary.write_text(json.dumps({"pid": os.getpid(), "prompt": prompt,
                                "slack_env_names": [k for k in os.environ if k.upper().startswith(("SLACK_", "LAOWU_SLACK_"))],
                                "descendant_pid": child.pid if child else None}), encoding="utf-8")
temporary.replace(started)
print(json.dumps({"type": "thread.started", "thread_id": "fake_" + run_id}), flush=True)
deadline = time.monotonic() + 30
while wait_mode == "hold" and not (control / (run_id + ".release")).exists():
    if time.monotonic() > deadline:
        sys.exit(9)
    time.sleep(0.025)
private_error = "Authorization: Bearer SYNTHETIC_PRIVATE_VALUE https://private.invalid/path PRIVATE_CHAT"
if output_mode in ("stream_failure", "structured_failure", "error_only", "recovered", "terminal_then_completed"):
    print(json.dumps({"type": "error", "message": "stream disconnected before completion " + private_error}), flush=True)
if output_mode in ("stream_failure", "structured_failure", "unknown_failure", "terminal_then_completed"):
    error = {"message": private_error}
    if output_mode == "structured_failure":
        error["codexErrorInfo"] = {"HttpConnectionFailed": {"httpStatusCode": 401, "private": private_error}}
    print(json.dumps({"type": "turn.failed", "error": error}), flush=True)
    if output_mode != "terminal_then_completed":
        sys.exit(1)
if output_mode in ("stderr_only", "unknown_stderr"):
    text = "Read-only file system (os error 30) " if output_mode == "stderr_only" else ""
    sys.stderr.write(text + private_error)  # No newline: classifier must still see EOF output.
    sys.exit(7)
if output_mode == "error_only":
    sys.exit(0)  # Error event still fails, even if the CLI exits zero.
result = {"result": "已完成测试任务", "artifacts": [str(started)],
          "validation": ["本地假 CLI 事件"], "unfinished": [], "memory_candidates": []}
if output_mode == "invalid":
    result["validation"] = [123]
elif output_mode == "unfinished":
    result["unfinished"] = ["缺少验收所需的依赖，尚未完成任务"]
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "phase": "final",
                  "text": json.dumps(result, ensure_ascii=False)}}, ensure_ascii=False), flush=True)
if output_mode == "invalid_after_valid":
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "最终未完成，且没有合法交接"}}, ensure_ascii=False), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
'''


PARENT_PROCESS = r'''
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from laowu.adapters import WorkerLaunch
from laowu.runtime import Manager
from laowu.store import Store

def fake(root, friend, client, run_id, prompt, workspace, worker_token=""):
    return WorkerLaunch([sys.executable, str(root / "fake_cli.py"), str(root / "fake"),
                         run_id, "hold", "valid", "child"], workspace, {}, prompt)

with patch("laowu.adapters.prepare_worker", fake):
    background = len(sys.argv) > 2 and sys.argv[2] == 'background'
    manager = Manager(Store(root), background=background)
    task = manager.submit("engineer", "等待父进程被强制结束", root / "工作甲",
                          detached=background, origin_thread='origin' if background else None)
    (root / "parent.json").write_text(json.dumps({"session_id": manager.id,
                                                 "task_id": task["id"]}), encoding="utf-8")
    while True:
        time.sleep(0.1)
'''


@unittest.skipUnless(sys.platform.startswith("linux"), "运行器使用 Linux 进程身份与进程组")
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="老五运行验收-")
        self.root = Path(self.temporary.name) / "中文项目"
        self.root.mkdir()
        source = Path(__file__).resolve().parents[1]
        for name in ("laowu", "scripts", "friends", ".agents"):
            shutil.copytree(source / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.assertTrue((self.root / "scripts/laowu.py").is_file(), "需有真实 guardian CLI 入口")
        self.control = self.root / "fake"
        self.control.mkdir()
        (self.root / "fake_cli.py").write_text(FAKE_CLI, encoding="utf-8")
        self.workspaces = [self.root / name for name in ("工作甲", "工作乙", "工作丙")]
        for directory in self.workspaces:
            directory.mkdir()
        self.store = Store(self.root)
        Registry(self.store).sync()
        self.managers = []
        self.parents = []
        self.wait_mode = "hold"
        self.output_mode = "valid"
        self.launches = {}
        self.launch_lock = threading.Lock()
        self.launch_patch = patch("laowu.adapters.prepare_worker", self.fake_launch)
        self.launch_patch.start()

    def tearDown(self):
        # Closing the owner is the ordinary cleanup path. Forced cleanup below is
        # only a last resort so a failed assertion cannot leave a fake process.
        for manager in reversed(self.managers):
            manager.close()
        for parent in self.parents:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            if parent.stderr is not None:
                parent.stderr.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = self.runs()
            if all(not process_alive(r[pid], r[identity]) for r in rows
                   for pid, identity in (("child_pid", "child_identity"),
                                         ("supervisor_pid", "supervisor_identity"))):
                break
            time.sleep(0.025)
        for row in self.runs():
            for pid, identity in (("child_pid", "child_identity"),
                                  ("supervisor_pid", "supervisor_identity")):
                if process_alive(row[pid], row[identity]):
                    try:
                        os.killpg(row[pid], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.launch_patch.stop()
        self.temporary.cleanup()

    def fake_launch(self, root, friend, client, run_id, prompt, workspace, worker_token=""):
        with self.launch_lock:
            self.launches[run_id] = {"profile": json.loads(json.dumps(friend)), "prompt": prompt}
        return WorkerLaunch([sys.executable, str(root / "fake_cli.py"), str(self.control), run_id,
                             self.wait_mode, self.output_mode, "none"], workspace, {}, prompt)

    def manager(self):
        manager = Manager(Store(self.root))
        self.managers.append(manager)
        return manager

    def poll(self, predicate, message="状态未在期限内达到", timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.025)
        self.fail(f"{message}; runs={self.runs()}")

    def runs(self, task_id=None):
        with self.store.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone()
            if not exists:
                return []
            if task_id:
                rows = db.execute("SELECT * FROM runs WHERE task_id=? ORDER BY started_at,rowid", (task_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM runs ORDER BY started_at,rowid").fetchall()
        return [dict(row) for row in rows]

    def started(self, task_id, run_number=1):
        def ready():
            rows = self.runs(task_id)
            if len(rows) < run_number:
                return None
            row = rows[run_number - 1]
            return row if row["status"] == "running" and row["child_identity"] and (
                self.control / (row["id"] + ".started.json")).exists() else None
        return self.poll(ready, f"任务 {task_id} 的第 {run_number} 次运行未启动")

    def release(self, run):
        (self.control / (run["id"] + ".release")).touch()

    def finished(self, manager, task_id, status="completed"):
        result = manager.wait(task_id, timeout=10)
        self.assertEqual(result["status"], status, result)
        return result

    def assert_dead(self, run):
        self.poll(lambda: not process_alive(run["child_pid"], run["child_identity"]), "朋友进程未停止")

    def test_queued_task_completes_with_validated_json_handoff(self):
        manager = self.manager()
        task = manager.submit("engineer", "完成一个假模型任务", self.workspaces[0])
        run = self.started(task["id"])
        self.release(run)
        completed = self.finished(manager, task["id"])
        result = completed["result"]
        self.assertEqual(set(result), {"result", "artifacts", "validation", "unfinished", "memory_candidates"})
        self.assertEqual(result["result"], "已完成测试任务")
        self.assertTrue(Path(result["artifacts"][0]).is_file())
        self.assertEqual(self.runs(task["id"])[0]["status"], "completed")
        self.assertEqual(self.runs(task["id"])[0]["cli_session_id"], "fake_" + run["id"])
        self.assert_dead(run)

    def test_earlier_json_cannot_mask_invalid_final_message(self):
        self.output_mode = "invalid_after_valid"
        self.wait_mode = "immediate"
        manager = self.manager()
        task = manager.submit("engineer", "最终交接必须有效", self.workspaces[0])
        finished = self.finished(manager, task["id"], "failed")
        self.assertEqual(finished["error"], "invalid_handoff")
        self.assert_dead(self.runs(task["id"])[0])

    def test_client_failures_keep_only_safe_diagnostics_after_cleanup(self):
        self.wait_mode = "immediate"
        manager = self.manager()
        cases = (("stream_failure", "client_stream", "json_event", 1),
                 ("structured_failure", "client_authentication", "json_event", 1),
                 ("unknown_failure", "client_error", "json_event", 1),
                 ("stderr_only", "client_filesystem_read_only", "stderr", 7),
                 ("unknown_stderr", "cli_exit_7", None, 7),
                 ("error_only", "client_stream", "json_event", 0),
                 ("terminal_then_completed", "client_stream", "json_event", 0))
        for mode, code, source, exit_code in cases:
            with self.subTest(mode=mode):
                self.output_mode = mode
                task = manager.submit("engineer", "诊断假客户端故障", self.workspaces[0])
                finished = self.finished(manager, task["id"], "failed")
                run = finished["runs"][-1]
                self.assertEqual(finished["error"], code)
                self.assertEqual(run["error"], code)
                self.assertEqual(run["diagnostics"]["exit_code"], exit_code)
                if source:
                    self.assertEqual(run["diagnostics"]["last_error"]["source"], source)
                if mode == "structured_failure":
                    self.assertEqual(run["diagnostics"]["last_error"]["http_status"], 401)
                self.poll(lambda: not (self.root / ".laowu/runtime" / run["id"]).exists())
                self.assert_dead(self.runs(task["id"])[0])
        # Search the actual database/WAL bytes, not just the returned object.
        for path in self.store.private.glob("state.sqlite3*"):
            data = path.read_bytes()
            for marker in (b"SYNTHETIC_PRIVATE_VALUE", b"private.invalid", b"PRIVATE_CHAT"):
                self.assertNotIn(marker, data)

    def test_recoverable_error_followed_by_completed_turn_does_not_fail(self):
        self.wait_mode, self.output_mode = "immediate", "recovered"
        manager = self.manager()
        task = manager.submit("engineer", "重连后成功", self.workspaces[0])
        finished = self.finished(manager, task["id"])
        self.assertIsNone(finished["error"])
        self.assertEqual(finished["runs"][0]["diagnostics"]["error_events"], 1)
        self.assertTrue(finished["runs"][0]["diagnostics"]["turn_completed"])

    def test_preflight_failure_is_visible_without_child_or_raw_error(self):
        manager = self.manager()
        diagnostic = {"source": "config_validation", "exit_code": 1,
                      "category": "filesystem_read_only", "basis": "message_pattern"}
        with patch("laowu.adapters.prepare_worker", side_effect=AdapterError(
                "config_validation_failed", "安全的错误摘要", diagnostic)):
            task = manager.submit("engineer", "配置预检", self.workspaces[0])
            finished = self.finished(manager, task["id"], "failed")
        self.assertEqual(finished["error"], "config_validation_failed")
        self.assertEqual(finished["runs"][0]["diagnostics"], diagnostic)
        self.assertIsNone(self.runs(task["id"])[0]["child_pid"])

    def test_writes_serialize_and_global_capacity_is_two(self):
        manager = self.manager()
        tasks = [manager.submit("engineer", f"并行任务 {index}", workspace)
                 for index, workspace in enumerate((self.workspaces[0], self.workspaces[0],
                                                     self.workspaces[1], self.workspaces[2]))]

        def active():
            rows = [row for row in self.runs() if row["status"] in {"starting", "running"}]
            self.assertLessEqual(len(rows), 2)
            self.assertEqual(len({row["workspace"] for row in rows}), len(rows), "同目录写入重叠")
            return rows

        initial = self.poll(lambda: active() if len(active()) == 2 else None, "两个独立目录的任务没有并行")
        first = next(row for row in initial if row["workspace"] == str(self.workspaces[0]))
        same_directory = [task for task in tasks[:2] if task["id"] != first["task_id"]][0]
        self.assertEqual(task_record(self.store, same_directory["id"])["status"], "queued")
        self.release(first)
        second = self.started(same_directory["id"])
        self.assertEqual(task_record(self.store, first["task_id"])["status"], "completed")
        self.assertNotEqual(second["id"], first["id"])

        def complete_all():
            for row in active():
                self.release(row)
            return all(task_record(self.store, task["id"])["status"] == "completed" for task in tasks)

        self.poll(complete_all, "队列没有全部完成")
        self.assertEqual(len(self.runs()), 4)

    def test_idempotent_retry_does_not_duplicate_task_or_run(self):
        first, second = self.manager(), self.manager()
        task = first.submit("engineer", "同一项任务", self.workspaces[0], "stable-request")
        retry = second.submit("engineer", "同一项任务", self.workspaces[0], "stable-request")
        self.assertEqual(task["id"], retry["id"])
        self.assertEqual(retry["session_id"], first.id)
        with self.assertRaises(ValueError):
            second.submit("engineer", "另一项任务", self.workspaces[0], "stable-request")
        run = self.started(task["id"])
        self.assertEqual(first.submit("engineer", "同一项任务", self.workspaces[0], "stable-request")["id"], task["id"])
        self.release(run)
        self.finished(first, task["id"])
        self.assertEqual(len(self.runs(task["id"])), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_closing_one_manager_preserves_another_session(self):
        first, second = self.manager(), self.manager()
        own = first.submit("engineer", "第一个会话的任务", self.workspaces[0])
        other = second.submit("engineer", "第二个会话的任务", self.workspaces[1])
        own_run, other_run = self.started(own["id"]), self.started(other["id"])
        first.close()
        self.assertEqual(task_record(self.store, own["id"])["status"], "interrupted")
        self.assert_dead(own_run)
        reconcile(self.store)
        self.assertEqual(task_record(self.store, other["id"])["status"], "running")
        self.assertTrue(process_alive(other_run["child_pid"], other_run["child_identity"]))
        self.release(other_run)
        self.finished(second, other["id"])

    def test_checkpoint_survives_close_and_resume_in_new_manager(self):
        first = self.manager()
        task = first.submit("engineer", "需要分阶段完成", self.workspaces[0])
        old_run = self.started(task["id"])
        artifact = self.workspaces[0] / "阶段产物.txt"
        artifact.write_text("已经完成的第一步", encoding="utf-8")
        saved = checkpoint(self.store, task["id"], "第一步已完成；下一步核对产物", artifacts=[str(artifact)])
        first.close()
        self.assert_dead(old_run)
        self.assertEqual(task_record(self.store, task["id"])["checkpoint"], saved)
        second = self.manager()
        second.resume(task["id"])
        new_run = self.started(task["id"], run_number=2)
        with self.launch_lock:
            restored_prompt = self.launches[new_run["id"]]["prompt"]
        self.assertIn(saved["summary"], restored_prompt)
        self.assertIn(str(artifact), restored_prompt)
        self.assertEqual(artifact.read_text(encoding="utf-8"), "已经完成的第一步")
        self.assertEqual(new_run["session_id"], second.id)
        self.assertNotEqual(new_run["id"], old_run["id"])
        self.release(new_run)
        self.finished(second, task["id"])

    def test_sigkill_owner_closes_lease_and_stops_worker_group(self):
        parent_script = self.root / "fake_parent.py"
        parent_script.write_text(PARENT_PROCESS, encoding="utf-8")
        parent = subprocess.Popen([sys.executable, str(parent_script), str(self.root)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, start_new_session=True)
        self.parents.append(parent)
        self.poll(lambda: (self.root / "parent.json").exists(), "独立父进程未提交任务")
        metadata = json.loads((self.root / "parent.json").read_text(encoding="utf-8"))
        run = self.started(metadata["task_id"])
        from laowu.common import process_identity
        started = json.loads((self.control / (run["id"] + ".started.json")).read_text(encoding="utf-8"))
        descendant_pid = started["descendant_pid"]
        descendant_identity = process_identity(descendant_pid)
        self.assertTrue(process_alive(descendant_pid, descendant_identity))
        parent.kill()
        parent.wait(timeout=5)
        self.assertEqual(parent.returncode, -signal.SIGKILL)
        self.assert_dead(run)
        self.poll(lambda: not process_alive(descendant_pid, descendant_identity), "朋友后代进程仍在运行")
        self.poll(lambda: task_record(self.store, metadata["task_id"])["status"] == "interrupted")
        reconcile(self.store)
        with self.store.connect() as db:
            status = db.execute("SELECT status FROM sessions WHERE id=?", (metadata["session_id"],)).fetchone()[0]
        self.assertEqual(status, "interrupted")

    def test_detached_task_keeps_running_after_origin_session_closes(self):
        origin = self.manager()
        daemon = Manager(self.store, background=True)
        self.managers.append(daemon)
        task = origin.submit('engineer', '后台继续', self.workspaces[0], detached=True, origin_thread='origin')
        run = self.started(task['id'])
        self.assertEqual(run['session_id'], daemon.id)
        checkpoint(self.store, task['id'], '已提交的后台检查点')
        origin.close()
        self.assertEqual(task_record(self.store, task['id'])['status'], 'running')
        self.assertTrue(process_alive(run['child_pid'], run['child_identity']))
        self.release(run)
        self.finished(daemon, task['id'])

    def test_sigkill_background_owner_recovers_only_after_old_group_stops(self):
        from laowu.daemon import recover_interrupted
        parent_script = self.root / 'background_parent.py'
        parent_script.write_text(PARENT_PROCESS, encoding='utf-8')
        parent = subprocess.Popen([sys.executable, str(parent_script), str(self.root), 'background'],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, start_new_session=True)
        self.parents.append(parent)
        self.poll(lambda: (self.root / 'parent.json').exists(), '后台进程未提交任务')
        task_id = json.loads((self.root / 'parent.json').read_text())['task_id']
        old = self.started(task_id)
        checkpoint(self.store, task_id, '强制结束前已提交的进度')
        parent.kill(); parent.wait(timeout=5)
        self.assert_dead(old)
        self.poll(lambda: task_record(self.store, task_id)['status'] == 'interrupted')
        reconcile(self.store)
        second = Manager(self.store, background=True)
        self.managers.append(second)
        recover_interrupted(self.store)
        new = self.started(task_id, 2)
        prompt = json.loads((self.control / (new['id'] + '.started.json')).read_text())['prompt']
        self.assertIn('强制结束前已提交的进度', prompt)
        self.release(new)
        self.finished(second, task_id)
        self.assertEqual(len(self.runs(task_id)), 2)

    def test_daemon_restart_recovers_detached_checkpoint_without_replaying_completed(self):
        from laowu.daemon import recover_interrupted
        origin = self.manager()
        first = Manager(self.store, background=True)
        self.managers.append(first)
        task = origin.submit('engineer', '重启续做', self.workspaces[0], detached=True, origin_thread='origin')
        old = self.started(task['id'])
        checkpoint(self.store, task['id'], '第一步已完成，不要重复外部操作')
        first.close()
        self.assert_dead(old)
        second = Manager(self.store, background=True)
        self.managers.append(second)
        recover_interrupted(self.store)
        new = self.started(task['id'], 2)
        prompt = json.loads((self.control / (new['id'] + '.started.json')).read_text())['prompt']
        self.assertIn('第一步已完成', prompt)
        self.release(new)
        self.finished(second, task['id'])
        recover_interrupted(self.store)
        self.assertEqual(task_record(self.store, task['id'])['status'], 'completed')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT origin_thread FROM async_tasks WHERE task_id=?', (task['id'],)).fetchone()[0], 'origin')

    def test_missing_cli_fails_without_claiming_success(self):
        manager = self.manager()
        with patch("laowu.adapters.prepare_worker", side_effect=AdapterError("client_missing", "假 CLI 缺失")):
            task = manager.submit("engineer", "客户端不可用", self.workspaces[0])
            result = self.finished(manager, task["id"], "failed")
        self.assertEqual(result["error"], "client_missing")
        self.assertIsNone(result["result"])
        self.assertIsNone(self.runs(task["id"])[0]["child_pid"])

    def test_slack_background_grant_is_enforced_and_generation_stops_guardian(self):
        from laowu.access import task_access
        from laowu.slack_state import SlackState
        state = SlackState(self.store)
        state.update_channel('TA', 'CA', eligible=1)
        state.issue_binding('TA', 'CA')
        origin = self.manager()
        background = Manager(self.store, background=True)
        self.managers.append(background)
        context = dict(conversation_id='slack:TA:CA:0', audience='shared', reply_thread='1.00', input_id='input-test')
        with patch.dict(os.environ, {'SLACK_BOT_TOKEN': 'fake-slack-secret', 'LAOWU_SLACK_TOKEN_FILE': '/must-not-inherit'}):
            task = origin.submit('researcher', '无shell后台调查', self.workspaces[0], detached=True, access_context=context)
            run = self.started(task['id'])
        self.assertEqual(task_access(self.store, task['id'])['conversation_id'], context['conversation_id'])
        self.assertNotIn('read', json.loads(run['spec_json'])['tools'])
        started = json.loads((self.control / (run['id'] + '.started.json')).read_text())
        self.assertEqual(started['slack_env_names'], [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT origin_thread FROM async_tasks WHERE task_id=?', (task['id'],)).fetchone()[0], context['conversation_id'])
        with self.store.transaction() as db:
            db.execute('UPDATE slack_channels SET generation=generation+1')
        self.finished(background, task['id'], 'interrupted')
        self.assert_dead(run)

    def test_scheduler_failure_keeps_diagnostic_and_closes_existing_lease(self):
        manager = self.manager()
        task = manager.submit('engineer', '调度器失败后清理既有本地假进程', self.workspaces[0])
        run = self.started(task['id'])
        # Real SQLite error metadata; arbitrary error text must never be stored.
        failure = sqlite3.OperationalError('database is locked PRIVATE_SCHEDULER_TEXT')
        failure.sqlite_errorcode = 5
        with patch.object(manager, '_claim', side_effect=failure):
            self.assertTrue(manager.closed.wait(timeout=5))
            manager.thread.join(timeout=5)
        self.assertFalse(manager.thread.is_alive())
        with self.store.connect() as db:
            session = db.execute('SELECT * FROM sessions WHERE id=?', (manager.id,)).fetchone()
            self.assertEqual(session['status'], 'interrupted')
            diagnostic = json.loads(session['diagnostics'])
            self.assertEqual(diagnostic['last_error'], {'source': 'scheduler', 'category': 'database_busy',
                                                       'basis': 'structured', 'sqlite_code': 5})
            self.assertNotIn('PRIVATE_SCHEDULER_TEXT', ' '.join(db.iterdump()))
        finished = task_record(self.store, task['id'])
        self.assertEqual((finished['status'], finished['error']), ('interrupted', 'scheduler_failed'))
        self.assertEqual(finished['runs'][0]['error'], 'scheduler_failed')
        manager.close()  # Cleanup must not erase the failure's status or diagnostics.
        self.assert_dead(run)
        self.assertEqual(manager._children, {})

    def test_scheduler_storage_outage_still_stops_child_and_can_persist_after_recovery(self):
        manager = self.manager()
        task = manager.submit('engineer', '数据库不可写时也须断开本地假进程租约', self.workspaces[0])
        run = self.started(task['id'])
        with patch.object(manager.store, 'transaction', side_effect=OSError(30, 'PRIVATE_STORAGE_ERROR')), \
                patch('threading.excepthook') as unhandled:
            self.assertTrue(manager.closed.wait(timeout=5))
            manager.thread.join(timeout=5)
        unhandled.assert_not_called()
        self.assert_dead(run)
        self.assertEqual(manager._children, {})
        manager.close()  # Retry writing the safe RAM diagnostic when storage is writable.
        with self.store.connect() as db:
            session = db.execute('SELECT status,diagnostics FROM sessions WHERE id=?', (manager.id,)).fetchone()
            self.assertEqual(session['status'], 'interrupted')
            self.assertEqual(json.loads(session['diagnostics'])['last_error'], {
                'source': 'scheduler', 'category': 'filesystem_read_only', 'basis': 'structured', 'os_errno': 30})
            self.assertNotIn('PRIVATE_STORAGE_ERROR', ' '.join(db.iterdump()))

    def test_cancelled_claim_cannot_launch_or_recreate_context(self):
        # Freeze automatic scheduling to put cancellation exactly between claim
        # and launch, without trying to win a timing race against the thread.
        with patch.object(Manager, "_schedule", lambda manager: None):
            manager = self.manager()
        task = manager.submit("engineer", "排队后在启动前取消", self.workspaces[0])
        claimed = manager._claim()
        self.assertIsNotNone(claimed)
        cancel_task(self.store, task["id"])
        with patch("laowu.adapters.prepare_worker") as prepare, patch("laowu.runtime.subprocess.Popen") as launch:
            manager._launch(claimed)
        prepare.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(task_record(self.store, task["id"])["status"], "cancelled")
        self.assertFalse((self.root / ".laowu/runtime" / claimed[2]).exists())

    def test_close_during_launch_leaves_no_live_worker_or_owned_pipe(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_prepare(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("测试启动屏障超时")
            return self.fake_launch(*args, **kwargs)

        manager = self.manager()
        with warnings.catch_warnings(record=True) as observed, patch("laowu.adapters.prepare_worker", blocked_prepare):
            warnings.simplefilter("always", ResourceWarning)
            task = manager.submit("engineer", "准备启动期间关闭会话", self.workspaces[0])
            self.assertTrue(entered.wait(timeout=5))
            closer = threading.Thread(target=manager.close, daemon=True)
            closer.start()
            try:
                self.assertTrue(manager.closed.wait(timeout=5))
            finally:
                release.set()
            closer.join(timeout=10)
        self.assertFalse(closer.is_alive(), "关闭流程在启动竞争中阻塞")
        self.assertFalse([warning for warning in observed if issubclass(warning.category, ResourceWarning)],
                         "关闭与启动竞争留下未回收的 subprocess")
        self.assertEqual(task_record(self.store, task["id"])["status"], "interrupted")
        self.assertEqual(manager._children, {}, "关闭后仍持有启动实例与 lease")
        self.poll(lambda: all(not process_alive(row[pid], row[identity])
                              for row in self.runs(task["id"])
                              for pid, identity in (("child_pid", "child_identity"),
                                                    ("supervisor_pid", "supervisor_identity"))),
                  "关闭与启动竞争后有进程仍存活")

    def test_invalid_handoff_fails_even_when_cli_exits_zero(self):
        self.output_mode = "invalid"
        manager = self.manager()
        task = manager.submit("engineer", "返回字段类型错误的交接", self.workspaces[0])
        run = self.started(task["id"])
        self.release(run)
        result = self.finished(manager, task["id"], "failed")
        self.assertEqual(result["error"], "invalid_handoff")
        self.assertIsNone(result["result"])

    def test_unfinished_handoff_is_blocked_and_can_resume_after_fix(self):
        self.output_mode = "unfinished"
        manager = self.manager()
        task = manager.submit("engineer", "尚有未完成事项不能算完成", self.workspaces[0])
        first_run = self.started(task["id"])
        self.release(first_run)
        result = self.finished(manager, task["id"], "blocked")
        self.assertEqual(result["error"], "unfinished_work")
        self.assertEqual(result["result"]["unfinished"], ["缺少验收所需的依赖，尚未完成任务"])
        self.assertEqual(self.runs(task["id"])[0]["status"], "blocked")
        cli = subprocess.run([sys.executable, str(self.root / "scripts/laowu.py"), "--root", str(self.root),
                              "tasks", "show", task["id"]], capture_output=True, text=True, timeout=10)
        self.assertEqual(cli.returncode, 1, cli.stdout + cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["status"], "blocked")
        self.output_mode = "valid"
        manager.resume(task["id"])
        second_run = self.started(task["id"], run_number=2)
        self.release(second_run)
        self.finished(manager, task["id"])

    def test_profile_update_keeps_running_version_and_next_run_uses_new_version(self):
        manager = self.manager()
        first = manager.submit("engineer", "使用原版本工作", self.workspaces[0])
        first_run = self.started(first["id"])
        old_profile = json.loads(first_run["spec_json"])
        update = manager.registry.get("engineer")
        update["instructions"] += "\n新增规则：交接时说明兼容性。"
        installed = manager.registry.ensure(update)
        self.assertGreater(installed["version"], first_run["friend_version"])
        second = manager.submit("engineer", "使用新版本工作", self.workspaces[0])
        persisted = self.runs(first["id"])[0]
        self.assertEqual(json.loads(persisted["spec_json"]), old_profile)
        self.assertEqual(persisted["friend_version"], first_run["friend_version"])
        self.release(first_run)
        self.finished(manager, first["id"])
        second_run = self.started(second["id"])
        self.assertEqual(second_run["friend_version"], installed["version"])
        self.assertIn("新增规则", json.loads(second_run["spec_json"])["instructions"])
        with self.launch_lock:
            self.assertNotIn("新增规则", self.launches[first_run["id"]]["profile"]["instructions"])
        self.release(second_run)
        self.finished(manager, second["id"])


if __name__ == "__main__":
    unittest.main()
