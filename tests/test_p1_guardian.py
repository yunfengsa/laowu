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
tool_program='import signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path('+repr(str(directory/'tool.ready'))+').touch();time.sleep(30)'
tool=subprocess.Popen([sys.executable,'-c',tool_program])
while not (directory/'tool.ready').exists():time.sleep(0.005)
(directory/'ready.json').write_text(json.dumps({'cli':os.getpid(),'tool':tool.pid}))
print(json.dumps({'type':'thread.started','thread_id':'fake'}),flush=True)
if mode=='exit':
    result=dict(result='ok',artifacts=[],validation=[],unfinished=[],memory_candidates=[])
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','phase':'final','text':json.dumps(result)}}),flush=True)
    print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)
else:
    time.sleep(30)
'''


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process identity and pidfds')
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

    def test_missing_pidfd_capability_refuses_cli_before_fork(self):
        reader,writer=os.pipe()
        os.write(writer,b'1')
        os.close(writer)
        marker=self.root/'must-not-run'
        program='''import json,sys
from unittest.mock import patch
from laowu.common import run_process_group
with patch('laowu.common.os.pidfd_open',side_effect=PermissionError()),patch('laowu.common.os.fork',side_effect=AssertionError('must not fork')) as fork:
    code=run_process_group(int(sys.argv[1]),sys.argv[2],sys.argv[3:])
    assert not fork.called
    print(json.dumps({'code':code}))
'''
        try:
            process=subprocess.run([sys.executable,'-c',program,str(reader),str(self.root/'group.json'),sys.executable,'-c',f'open({str(marker)!r},"w").close()'],pass_fds=(reader,),start_new_session=True,capture_output=True,text=True,timeout=5)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertEqual(json.loads(process.stdout),{'code':1})
            self.assertFalse(marker.exists())
        finally:
            os.close(reader)

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
        with patch('laowu.common.group_members',side_effect=lambda pgid:next(states,{})),patch('laowu.common.process_identity',return_value=None),patch.object(Path,'read_text',read),patch('laowu.common.signal_process',side_effect=lambda *args:calls.append(args)),patch('laowu.common.time.sleep'):
            self.assertTrue(stop_process_group(10,'boot:anchor'))
        self.assertTrue(any(pid==12 for pid,identity,signum in calls))
