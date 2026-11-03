"""Real runner and private Python fake CLI; no model, Slack, or network calls."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from laowu.adapters import WorkerLaunch
from laowu.common import group_members, process_alive, process_identity, signal_process, stop_process_group
from laowu.runtime import Manager, cancel_task, cleanup_runs, reconcile, task_record
from laowu.store import Store


FAKE = '''import json,os,signal,subprocess,sys,time
from pathlib import Path
directory=Path(sys.argv[1]); mode=sys.argv[2]
sys.stdin.read()
handler='lambda *a:(Path('+repr(str(directory/'term.received'))+').touch(),sys.exit(0))' if mode=='term_handler' else 'signal.SIG_IGN'
tool_program='import signal,time,sys;from pathlib import Path;signal.signal(signal.SIGTERM,'+handler+');Path('+repr(str(directory/'tool.ready'))+').touch();time.sleep(30)'
tool=subprocess.Popen([sys.executable,'-c',tool_program])
while not (directory/'tool.ready').exists():time.sleep(0.005)
(directory/'ready.json').write_text(json.dumps({'cli':os.getpid(),'tool':tool.pid}))
print(json.dumps({'type':'thread.started','thread_id':'fake'}),flush=True)
if mode in ('exit','big_exit','exit7'):
    result=dict(result='x'*100000 if mode=='big_exit' else 'ok',artifacts=[],validation=[],unfinished=[],memory_candidates=[])
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','phase':'final','text':json.dumps(result)}}),flush=True)
    print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)
    if mode=='exit7':sys.exit(7)
else:
    time.sleep(30)
'''


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process identity and waitid')
class GuardianTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='laowu-p1-guardian-')
        self.root = Path(self.temporary.name)
        source = Path(__file__).resolve().parents[1]
        for name in ('laowu','scripts','friends','.agents'):
            shutil.copytree(source/name,self.root/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        self.fake = self.root/'fake.py'
        self.fake.write_text(FAKE)
        self.store = Store(self.root)
        self.mode = 'hold'
        self.managers = []
        self.live = {}
        self.patcher = patch('laowu.adapters.prepare_worker',self.prepare)
        self.patcher.start()

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        for pid,identity in self.live.items():
            signal_process(pid,identity,signal.SIGKILL)
        self.patcher.stop()
        self.temporary.cleanup()

    def prepare(self,root,friend,client,run_id,prompt,workspace,worker_token=''):
        return WorkerLaunch([sys.executable,str(self.fake),str(self.root),self.mode],workspace,{},prompt)

    def until(self,condition):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            value=condition()
            if value:
                return value
            time.sleep(0.025)
        self.fail('fake runner condition timed out')

    def start(self,background=False):
        manager=Manager(self.store,background=background)
        self.managers.append(manager)
        task=manager.submit('engineer','local fake CLI only',self.root,detached=background)
        self.until(lambda:(self.root/'ready.json').exists())
        metadata=json.loads((self.root/'ready.json').read_text())
        for pid in metadata.values():
            identity=process_identity(pid)
            if identity:
                self.live[pid]=identity
        with self.store.connect() as db:
            run=dict(db.execute('SELECT * FROM runs WHERE task_id=?',(task['id'],)).fetchone())
        return manager,task,run,metadata

    @contextmanager
    def without_pidfds(self, *, missing=False, dynamic=False, waitid_denied=False):
        # This hook runs in the actual guardian, anchor, CLI and tool processes.
        # Keep any pre-existing safety sitecustomize (e.g. network/model guard).
        hook=self.root/'capability-hook'
        hook.mkdir()
        marker=self.root/'deny-pidfd'
        if not dynamic:
            marker.touch()
        (hook/'sitecustomize.py').write_text('''import errno,os,runpy,signal,sys
from pathlib import Path
for entry in sys.path:
    candidate=Path(entry)/'sitecustomize.py'
    if candidate.is_file() and candidate.resolve()!=Path(__file__).resolve():
        runpy.run_path(str(candidate),run_name='_inherited_safety_hook')
        break
marker=Path('''+repr(str(marker))+''')
original_open=getattr(os,'pidfd_open',None)
original_send=getattr(signal,'pidfd_send_signal',None)
def denied(fn,*args,**kwargs):
    if marker.exists() or fn is None:raise OSError(errno.ENOSYS,'synthetic old Linux')
    return fn(*args,**kwargs)
os.pidfd_open=lambda *a,**kw:denied(original_open,*a,**kw)
signal.pidfd_send_signal=lambda *a,**kw:denied(original_send,*a,**kw)
'''+("del os.pidfd_open\ndel signal.pidfd_send_signal\n" if missing else '')
            +("def no_waitid(*a,**kw):raise PermissionError('synthetic waitid denial')\nos.waitid=no_waitid\n" if waitid_denied else ''))
        original_open=os.pidfd_open
        original_send=signal.pidfd_send_signal
        def denied(fn,*args,**kwargs):
            if marker.exists():
                raise OSError('synthetic old Linux')
            return fn(*args,**kwargs)
        path=str(hook)+os.pathsep+os.environ.get('PYTHONPATH','')
        with patch.dict(os.environ,{'PYTHONPATH':path}), \
                patch('laowu.common.os.pidfd_open',side_effect=lambda *a,**kw:denied(original_open,*a,**kw)), \
                patch('laowu.common.signal.pidfd_send_signal',side_effect=lambda *a,**kw:denied(original_send,*a,**kw)):
            if waitid_denied:
                with patch('laowu.worker.os.waitid',side_effect=PermissionError('synthetic waitid denial')):
                    yield marker
            else:
                yield marker

    def assert_tools_dead(self):
        self.until(lambda:all(not process_alive(pid,identity) for pid,identity in self.live.items()))

    def test_guardian_sigkill_stops_cli_and_sigterm_ignoring_tool(self):
        manager,task,run,_=self.start()
        os.kill(run['supervisor_pid'],signal.SIGKILL)
        self.until(lambda:task_record(self.store,task['id'])['status']=='interrupted')
        self.assert_tools_dead()
        manager.close()
        with self.store.connect() as db:
            self.assertFalse(db.execute('SELECT 1 FROM run_cleanup WHERE run_id=?',(run['id'],)).fetchone())

    def test_anchor_sigkill_is_detected_without_waiting_for_pipe_eof(self):
        _,task,run,_=self.start()
        os.kill(run['child_pid'],signal.SIGKILL)
        self.until(lambda:task_record(self.store,task['id'])['status']=='failed')
        self.assert_tools_dead()

    def test_cli_leader_exit_does_not_leave_its_long_tool(self):
        self.mode='exit'
        manager,task,_,_=self.start()
        self.assertEqual(manager.wait(task['id'],8)['status'],'completed')
        self.assert_tools_dead()

    def test_cancel_and_close_stop_the_whole_group(self):
        manager,task,_,_=self.start()
        cancel_task(self.store,task['id'])
        manager.close()
        self.assert_tools_dead()

    def test_restart_reconciles_persisted_group_without_manager_handles(self):
        manager,task,run,_=self.start()
        os.kill(run['supervisor_pid'],signal.SIGKILL)
        # Independent manager/store uses persisted anchor + manifest, not _children.
        reconcile(Store(self.root))
        self.assert_tools_dead()
        self.assertEqual(task_record(self.store,task['id'])['status'],'interrupted')

    def test_starting_dead_supervisor_does_not_keep_a_slot(self):
        with patch.object(Manager,'_schedule',lambda manager:None):
            manager=Manager(self.store)
        self.managers.append(manager)
        task=manager.submit('engineer','synthetic pre-launch failure',self.root)
        claim=manager._claim()
        with self.store.transaction() as db:
            db.execute('UPDATE runs SET supervisor_pid=99999999,supervisor_identity=? WHERE id=?',('gone',claim[2]))
        reconcile(self.store)
        self.assertEqual(task_record(self.store,task['id'])['status'],'interrupted')

    def test_outer_gate_eof_prevents_cli_exec_before_registration(self):
        from laowu import common
        reader,writer=os.pipe()
        marker=self.root/'must-not-run'
        process=subprocess.Popen([sys.executable,str(Path(common.__file__)),str(reader),str(self.root/'group.json'),sys.executable,'-c',f'open({str(marker)!r},"w").close()'],pass_fds=(reader,),start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        os.close(reader)
        os.close(writer)
        process.wait(timeout=5)
        self.assertFalse(marker.exists())

    def test_worker_registration_failure_never_executes_the_cli(self):
        from laowu.worker import run
        with patch.object(Manager,'_schedule',lambda manager:None):
            manager=Manager(self.store)
        self.managers.append(manager)
        task=manager.submit('engineer','synthetic registration failure',self.root)
        claim=manager._claim()
        folder=self.store.private/'runtime'/claim[2]
        folder.mkdir(parents=True)
        (folder/'launch.json').write_text(json.dumps(dict(argv=[sys.executable,str(self.fake),str(self.root),'hold'],env={},cwd=str(self.root),stdin='test')))
        reader,writer=os.pipe()
        popen=subprocess.Popen
        transaction=self.store.transaction
        fail_next=[False]
        processes=[]
        def spawn(*args,**kwargs):
            process=popen(*args,**kwargs)
            processes.append(process)
            fail_next[0]=True
            return process
        def transact(*args,**kwargs):
            if fail_next[0]:
                fail_next[0]=False
                raise OSError('synthetic register failure')
            return transaction(*args,**kwargs)
        handlers={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
        try:
            with patch('laowu.worker.subprocess.Popen',spawn),patch.object(self.store,'transaction',transact):
                self.assertEqual(run(self.store,claim[2],reader),1)
            self.assertFalse((self.root/'ready.json').exists())
            self.assertTrue(all(p.poll() is not None for p in processes))
        finally:
            os.close(writer)
            for sig,handler in handlers.items():
                signal.signal(sig,handler)

    def test_runtime_cleanup_upgrade_backs_up_once(self):
        from laowu.runtime import initialize_runtime
        initialize_runtime(self.store)
        with self.store.transaction() as db:
            db.execute('DROP TABLE run_cleanup')
        with patch.object(self.store,'_backup_locked',wraps=self.store._backup_locked) as backup:
            initialize_runtime(self.store)
            initialize_runtime(self.store)
        self.assertEqual(backup.call_count,1)

    def test_uncertain_cleanup_preserves_writer_exclusion_and_is_visible(self):
        with patch.object(Manager,'_schedule',lambda manager:None):
            manager=Manager(self.store)
        self.managers.append(manager)
        task=manager.submit('engineer','synthetic old writer',self.root)
        claim=manager._claim()
        with self.store.transaction() as db:
            db.execute("UPDATE runs SET status='interrupted',child_pid=99999999,child_identity='gone' WHERE id=?",(claim[2],))
            db.execute('INSERT INTO run_cleanup VALUES(?)',(claim[2],))
            db.execute("UPDATE tasks SET status='interrupted' WHERE id=?",(task['id'],))
        with patch('laowu.common.stop_process_group',return_value=False):
            cleanup_runs(self.store)
        self.assertTrue(task_record(self.store,task['id'])['runs'][0]['cleanup_pending'])
        manager.submit('engineer','new writer must wait',self.root)
        self.assertIsNone(manager._claim())
        cleanup_runs(self.store)
        self.assertIsNotNone(manager._claim())

    def test_pid_reuse_is_never_signalled(self):
        with patch('laowu.common.os.pidfd_open',return_value=123),patch('laowu.common.os.close'),patch('laowu.common.process_alive',return_value=False),patch('laowu.common.signal.pidfd_send_signal') as send:
            self.assertFalse(signal_process(900,'old',signal.SIGKILL))
        send.assert_not_called()

    def test_unavailable_pidfd_keeps_cleanup_pending(self):
        with patch('laowu.common.os.pidfd_open',side_effect=PermissionError()):
            self.assertFalse(signal_process(os.getpid(),process_identity(os.getpid()),signal.SIGTERM))

    def test_unreadable_process_table_keeps_cleanup_pending_without_signals(self):
        with patch('laowu.common.group_members',side_effect=PermissionError()), \
                patch('laowu.common.signal_process') as send:
            self.assertFalse(stop_process_group(99999999,'unknown'))
        send.assert_not_called()
        with patch('laowu.common.os.pidfd_open',return_value=123),patch('laowu.common.os.close'),patch('laowu.common.signal.pidfd_send_signal',side_effect=PermissionError()):
            self.assertFalse(signal_process(os.getpid(),process_identity(os.getpid()),signal.SIGTERM))

    def test_no_pidfd_cli_exit_cleans_sigterm_ignoring_tool_and_keeps_success(self):
        self.mode='exit'
        with self.without_pidfds():
            manager,task,_,_=self.start()
            self.assertEqual(manager.wait(task['id'],8)['status'],'completed')
            self.assert_tools_dead()

    def test_no_pidfd_large_handoff_is_fully_drained(self):
        self.mode='big_exit'
        with self.without_pidfds(missing=True):
            manager,task,_,_=self.start()
            result=manager.wait(task['id'],8)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(len(result['result']['result']),100000)
            self.assert_tools_dead()

    def test_no_pidfd_preserves_nonzero_cli_exit(self):
        self.mode='exit7'
        with self.without_pidfds():
            manager,task,_,_=self.start()
            result=manager.wait(task['id'],8)
            self.assertEqual(result['error'],'cli_exit_7')
            self.assertEqual(result['runs'][0]['diagnostics']['exit_code'],7)
            self.assert_tools_dead()

    def test_no_pidfd_guardian_sigkill_stops_group(self):
        with self.without_pidfds():
            manager,task,run,_=self.start()
            os.kill(run['supervisor_pid'],signal.SIGKILL)
            self.until(lambda:task_record(self.store,task['id'])['status']=='interrupted')
            self.assert_tools_dead()

    def test_no_pidfd_anchor_sigkill_uses_unreaped_child_pin(self):
        with self.without_pidfds():
            manager,task,run,_=self.start()
            os.kill(run['child_pid'],signal.SIGKILL)
            result=manager.wait(task['id'],8)
            self.assertEqual(result['status'],'failed')
            self.assertNotEqual(result['runs'][0]['diagnostics']['exit_code'],0)
            self.assert_tools_dead()

    def test_no_pidfd_cancel_stops_group(self):
        with self.without_pidfds():
            manager,task,_,_=self.start()
            cancel_task(self.store,task['id'])
            self.assert_tools_dead()
            manager.close()

    def test_no_pidfd_close_stops_group(self):
        with self.without_pidfds():
            manager,_,_,_=self.start()
            manager.close()
            self.assert_tools_dead()

    def test_pidfds_disappearing_during_work_still_stop_group(self):
        with self.without_pidfds(dynamic=True) as marker:
            manager,task,_,_=self.start()
            marker.touch()
            cancel_task(self.store,task['id'])
            self.assert_tools_dead()

    def test_no_pidfd_graceful_sigterm_handler_runs_before_hard_stop(self):
        self.mode='term_handler'
        with self.without_pidfds():
            manager,task,_,_=self.start()
            cancel_task(self.store,task['id'])
            self.assert_tools_dead()
            self.assertTrue((self.root/'term.received').exists())

    def test_waitid_denied_pidfd_backend_still_completes(self):
        self.mode='exit'
        with self.without_pidfds(dynamic=True,waitid_denied=True):
            manager,task,_,_=self.start()
            self.assertEqual(manager.wait(task['id'],8)['status'],'completed')
            self.assert_tools_dead()

    def test_both_pin_backends_denied_never_executes_cli(self):
        with self.without_pidfds(waitid_denied=True):
            manager=Manager(self.store)
            self.managers.append(manager)
            task=manager.submit('engineer','pin capabilities unavailable',self.root)
            self.assertEqual(manager.wait(task['id'],8)['status'],'failed')
            self.assertFalse((self.root/'ready.json').exists())

    def owned_anchor(self):
        process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
        process._laowu_parent=os.getpid()
        process._laowu_reaped=False
        process._laowu_identity=process_identity(process.pid,include_zombie=True)
        process._laowu_manifest=self.root/'missing-group.json'
        return process

    def test_wrong_identity_or_foreign_parent_never_uses_numeric_fallback(self):
        from laowu.worker import stop_group
        process=self.owned_anchor()
        try:
            identity=process._laowu_identity
            for changes in ({'_laowu_identity':'reused'}, {'_laowu_parent':99999999}):
                process._laowu_identity=identity
                process._laowu_parent=os.getpid()
                for key,value in changes.items():setattr(process,key,value)
                with patch('laowu.worker.os.killpg') as send:
                    self.assertFalse(stop_group(process))
                send.assert_not_called()
        finally:
            process.kill()
            process.wait(timeout=5)

    def test_reaped_child_or_nondefault_sigchld_refuses_numeric_fallback(self):
        from laowu.worker import stop_group
        process=self.owned_anchor()
        try:
            with patch('laowu.worker.signal.getsignal',return_value=signal.SIG_IGN),patch('laowu.worker.os.killpg') as send:
                self.assertFalse(stop_group(process))
            send.assert_not_called()
            process.kill()
            process.wait(timeout=5)
            with patch('laowu.worker.os.killpg') as send:
                self.assertFalse(stop_group(process))
            send.assert_not_called()
        finally:
            if process.poll() is None:process.kill()
            process.wait(timeout=5)

    def test_exit_manifest_requires_matching_identity_and_verified_cleanup(self):
        from laowu.worker import cli_exit_code
        path=self.root/'exit.json'
        process=SimpleNamespace(pid=123,_laowu_identity='identity',_laowu_manifest=path,_laowu_clean=True)
        valid=dict(anchor_pid=123,anchor_identity='identity',self_cleanup=True,cli_exit_code=0)
        for changes in ({'anchor_pid':999},{'anchor_identity':'reused'},{'cli_exit_code':True},{'self_cleanup':False}):
            path.write_text(json.dumps({**valid,**changes}))
            self.assertEqual(cli_exit_code(process,-signal.SIGKILL),-signal.SIGKILL)
        path.write_text(json.dumps(valid))
        self.assertEqual(cli_exit_code(process,-signal.SIGKILL),0)
        process._laowu_clean=False
        self.assertEqual(cli_exit_code(process,-signal.SIGKILL),-signal.SIGKILL)
        path.write_text('bad json')
        process._laowu_clean=True
        self.assertEqual(cli_exit_code(process,-signal.SIGKILL),-signal.SIGKILL)

    def test_signal_success_with_surviving_members_stays_pending(self):
        from laowu.worker import stop_group
        process=SimpleNamespace(pid=123,_laowu_identity='identity',_laowu_manifest=self.root/'missing',wait=unittest.mock.Mock())
        with patch('laowu.worker.anchor_status',return_value=-9),patch('laowu.common.stop_process_group',return_value=False),patch('laowu.worker.os.killpg'),patch('laowu.common.group_members',return_value={456:'alive'}),patch('laowu.worker.time.sleep'),patch('laowu.worker.time.monotonic',side_effect=[0,2]):
            self.assertFalse(stop_group(process))
        process.wait.assert_not_called()

    def test_group_snapshot_uses_one_stat_for_group_and_identity(self):
        # A second stat read could refer to a reused PID in another group.
        entry=Path('/proc/123')
        fields=['S','1','777','777']+['0']*15+['456']
        def read(path,*unused,**kwargs):
            return 'boot' if str(path).endswith('boot_id') else '123 (synthetic) '+' '.join(fields)
        with patch.object(Path,'iterdir',return_value=iter([entry])),patch.object(Path,'read_text',read),patch('laowu.common.process_identity',side_effect=AssertionError('second stat read')):
            self.assertEqual(group_members(777),{123:'boot:456'})

    def test_verified_tool_remains_a_witness_after_leaders_exit(self):
        calls=[]
        fields=['S','1','10','10']+['0']*15
        def read(path,*args,**kwargs):
            if str(path).endswith('boot_id'):
                return 'boot'
            identity='anchor' if not calls else 'tool'
            return '12 (synthetic) '+' '.join([*fields,identity])
        states=iter([{10:'boot:anchor',11:'boot:cli',12:'boot:tool'},{12:'boot:tool'},{12:'boot:tool'},{}])
        with patch('laowu.common.group_members',side_effect=lambda pgid:next(states,{})),patch('laowu.common.process_identity',return_value=None),patch.object(Path,'read_text',read),patch('laowu.common.signal_process',side_effect=lambda *args:(calls.append(args) or True)),patch('laowu.common.time.sleep'):
            self.assertTrue(stop_process_group(10,'boot:anchor'))
        self.assertTrue(any(pid==12 for pid,identity,signum in calls))
