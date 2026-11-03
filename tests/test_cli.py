import tempfile
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from laowu.cli import parser, execute, doctor
from laowu.runtime import initialize_runtime
from laowu.store import Store


class CliTests(unittest.TestCase):
    def test_chinese_project_entry_uses_same_venv_with_ascii_executable(self):
        source = Path(__file__).resolve().parents[1]
        venv = source / '.venv'
        if not (venv / 'bin/python').exists():
            self.skipTest('此启动回归需要已安装项目虚拟环境')
        with tempfile.TemporaryDirectory(prefix='老五启动-') as folder:
            root = Path(folder)
            (root / 'scripts').mkdir()
            (root / 'laowu').mkdir()
            (root / '.venv').symlink_to(venv, target_is_directory=True)
            shutil.copy2(source / 'scripts/laowu.py', root / 'scripts/laowu.py')
            (root / 'laowu/__init__.py').touch()
            (root / 'laowu/cli.py').write_text(
                'import sys,json\ndef main():\n print(json.dumps([sys.executable,sys.prefix]));return 0\n')
            result=subprocess.run([sys.executable,str(root/'scripts/laowu.py')],capture_output=True,text=True,check=True)
            executable, prefix=json.loads(result.stdout)
            self.assertTrue(executable.isascii())
            self.assertEqual(Path(prefix).resolve(),venv.resolve())

    def test_chat_passes_codex_args_and_exit_code(self):
        with tempfile.TemporaryDirectory(prefix='老五命令-') as folder:
            root=Path(folder)
            args=parser().parse_args(['chat','--','resume','thread-id'])
            with patch('laowu.session_proxy.run_chat',return_value=7) as run:
                with self.assertRaises(SystemExit) as result:
                    execute(args,root)
                self.assertEqual(result.exception.code,7)
                self.assertEqual(run.call_args.args[0].root,root)
                self.assertEqual(run.call_args.args[1],['resume','thread-id'])

    def test_doctor_without_cli_args_does_not_call_model(self):
        with tempfile.TemporaryDirectory(prefix='老五诊断-') as folder:
            with patch('laowu.adapters.detect_clients',return_value={}):
                result=doctor(Path(folder))
            self.assertEqual(result['database_integrity'],'ok')
            self.assertFalse(result['background']['running'])

    def test_diagnose_migrates_old_run_schema_without_reading_task_text_or_starting_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            initialize_runtime(store)
            with store.transaction() as db:
                db.execute("INSERT INTO tasks(id,session_id,friend_id,client,request,workspace,status,error,request_hash,created_at,updated_at) "
                           "VALUES('task_test','s','engineer','codex','PRIVATE_REQUEST','/tmp','failed','client_error','h','t','t')")
                db.execute("INSERT INTO runs(id,task_id,session_id,friend_id,friend_version,client,status,can_write,workspace,spec_json,token_hash,started_at,updated_at,error) "
                           "VALUES('run_test','task_test','s','engineer',1,'codex','failed',1,'/tmp','PRIVATE_PROFILE','PRIVATE_TOKEN','t','t','client_error')")
                db.execute("ALTER TABLE runs DROP COLUMN diagnostics")
                db.execute("ALTER TABLE sessions DROP COLUMN diagnostics")
            args = parser().parse_args(['tasks', 'diagnose', 'task_test'])
            with patch('laowu.cli.Manager') as manager, patch('laowu.cli.reconcile') as reconcile:
                result = execute(args, root)
                again = execute(args, root)  # Migration is idempotent and retains old evidence.
            manager.assert_not_called()
            reconcile.assert_not_called()
            self.assertEqual(result, again)
            self.assertEqual(result['error'], 'client_error')
            self.assertEqual(result['runs'][0]['diagnostics'], {})
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_diagnose_shows_scheduler_failure_without_session_summary_or_host(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            initialize_runtime(store)
            safe = {'version': 1, 'last_error': {'source': 'scheduler', 'category': 'database_busy',
                                               'basis': 'structured', 'sqlite_code': 5}}
            with store.transaction() as db:
                db.execute("INSERT INTO sessions(id,client,host_session_id,status,summary,opened_at,updated_at,diagnostics) "
                           "VALUES('s','codex','PRIVATE_HOST','interrupted','PRIVATE_SUMMARY','t','t',?)", (json.dumps(safe),))
                db.execute("INSERT INTO tasks(id,session_id,friend_id,client,request,workspace,status,error,request_hash,created_at,updated_at) "
                           "VALUES('task_test','s','engineer','codex','PRIVATE_REQUEST','/tmp','interrupted','scheduler_failed','h','t','t')")
            result = execute(parser().parse_args(['tasks', 'diagnose', 'task_test']), root)
            self.assertEqual(result['sessions'], [{'id': 's', 'status': 'interrupted', 'diagnostics': safe}])
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_doctor_reports_unknown_when_socket_metadata_is_denied(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('laowu.adapters.detect_clients', return_value={}), \
                    patch('laowu.codex_bridge.socket_path') as path:
                path.return_value.exists.side_effect = PermissionError(13, 'PRIVATE_DIAGNOSTIC')
                result = doctor(Path(folder))
            self.assertIsNone(result['codex_socket_exists'])
            self.assertEqual(result['codex_socket_error'], 'permission_denied')
            self.assertNotIn('PRIVATE_DIAGNOSTIC', json.dumps(result))

if __name__=='__main__':unittest.main()
