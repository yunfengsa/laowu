"""Independent review regressions; all state and injected failures use temporary roots."""
import unittest
from test_phase1 import PhaseFixture
from laowu.runtime import task_record

class ReviewCases(PhaseFixture):
    def memory_ref(self,text):
        item=self.store.memory_upsert(text,scope='shared')
        shown=self.ctx.present('memory_search',{},[item])
        return item,next(self.refs(shown))

    def test_quotes_and_backslashes_are_readable(self):
        for symbol in ('"','\\'):
            item,ref=self.memory_ref(symbol*20000)
            self.assertEqual(self.read_all(self.ctx,ref),item['content'])

    def test_request_ref_survives_heartbeat(self):
        t=self.task('稳定请求'*6000)
        self.run_task(t)
        shown=self.ctx.present('task_status',{'task_id':t['id']},task_record(self.store,t['id']))
        ref=shown['data']['request']['ref']
        with self.store.transaction() as db:db.execute("UPDATE runs SET updated_at='2099-01-01T00:00:00+00:00' WHERE task_id=?",(t['id'],))
        self.assertEqual(self.read_all(self.ctx,ref),t['request'])

    def test_pending_forget_intent_blocks_existing_ref(self):
        item,ref=self.memory_ref('删除意图验证'*5000)
        self.store._write_ledger({item['id']},pending=True)
        with self.assertRaises((ValueError,PermissionError,RuntimeError)):self.ctx.read(ref)

    def test_accepted_requires_real_evidence(self):
        t=self.task();self.finish(t)
        with self.assertRaises(ValueError):
            self.svc.call('task_review',dict(task_id=t['id'],run_id='run_one',decision='accepted',summary=' ',evidence=[]))

    def test_closed_main_cannot_review(self):
        t=self.task();self.finish(t);self.svc.close()
        with self.assertRaises((ValueError,PermissionError,RuntimeError)):
            self.svc.call('task_review',dict(task_id=t['id'],run_id='run_one',decision='accepted',summary='已核对',evidence=['验证证据']))

    def test_upgrade_repairs_missing_epoch(self):
        from laowu.migrations import upgrade
        with self.store.connect() as db:
            db.execute("DELETE FROM memory_meta WHERE key='context_epoch'")
            upgrade(db)
            self.assertIsNotNone(db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone())

    def test_new_outage_after_success_gets_new_budget(self):
        from laowu.retries import ReadRetry,RetryPending
        retry=ReadRetry(self.store,lambda:self.clock[0])
        def fail():raise ConnectionError()
        with self.assertRaises(RetryPending):retry.call('poll','bridge.state',fail)
        self.clock[0]+=2
        self.assertEqual(retry.call('poll','bridge.state',lambda:'ok'),'ok')
        self.clock[0]+=400
        with self.assertRaises(RetryPending):retry.call('poll','bridge.state',fail)

    def test_typed_network_failures_retry_only_on_read_path(self):
        from laowu.retries import ReadRetry,RetryPending
        from laowu.slack_client import SlackAPIError
        from laowu.slack_codex import SlackCodexError
        errors=[SlackAPIError('transport_uncertain',uncertain=True),SlackCodexError('connection_lost','连接已断开')]
        for i,error in enumerate(errors):
            def fail(error=error):raise error
            with self.assertRaises(RetryPending):ReadRetry(self.store,lambda:self.clock[0]).call('transport'+str(i),'slack.receipt',fail)


import json
import os
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from unittest.mock import patch
from laowu.common import now
from laowu.context import Context, body_size
from laowu.retries import ReadRetry, RetryPending, RetrySuspended
from laowu.store import Store


def network_failure():
    raise ConnectionError()


class ReviewBoundaryTests(PhaseFixture):
    def test_blank_evidence_and_inactive_database_session_rejected(self):
        t=self.task(); self.finish(t)
        args=dict(task_id=t['id'],run_id='run_one',decision='accepted',summary='核对',evidence=['证据'])
        for changes in ({'summary':'\t\n'},{'evidence':[]},{'evidence':[' ']},{'evidence':['有效','\t']}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.svc.call('task_review',args|changes)
        with self.store.transaction() as db:
            db.execute("UPDATE sessions SET status='closed' WHERE id=?",(self.svc.manager.id,))
        with self.assertRaises(ValueError):self.svc.call('task_review',args)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM task_reviews').fetchone()[0],0)
        # Local maintenance is explicit and need not own an MCP session.
        from laowu.acceptance import review
        self.assertEqual(review(self.store,**args)['state'],'accepted')

    def test_forget_cleanup_rollback_revokes_memory_task_and_summary_refs(self):
        item=self.store.memory_upsert('须遗忘'*5000,scope='shared')
        mref=next(self.refs(self.ctx.present('memory_search',{},[item])))
        t=self.task('派生请求'*5000); self.finish(t)
        record=task_record(self.store,t['id'])
        tref=next(self.refs(self.ctx.present('task_status',{'task_id':t['id']},record)))
        with self.store.transaction() as db:
            db.execute('UPDATE sessions SET summary=? WHERE id=?',('派生摘要'*5000,self.svc.manager.id))
        summary=self.ctx.object('session',self.svc.manager.id)
        sref=next(self.refs(self.ctx.present('bootstrap',{},dict(recent_summaries=[summary]))))
        with self.store.connect() as db:epoch=db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone()[0]
        with patch.object(self.store,'_clear_runtime_files',side_effect=OSError('injected cleanup failure')):
            with self.assertRaises(OSError):self.store.memory_forget(item['id'])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone()[0],epoch)
            self.assertIsNotNone(db.execute('SELECT id FROM memory_records WHERE id=?',(item['id'],)).fetchone())
        for ref in (mref,tref,sref):
            with self.assertRaises(PermissionError):self.ctx.read(ref)
        for kind,ident in (('memory',item['id']),('task',t['id']),('session',self.svc.manager.id)):
            with self.assertRaises(PermissionError):self.ctx.object(kind,ident)
        with self.assertRaises(PermissionError):self.ctx.present('memory_search',{},[item])
        # Even a non-pending tombstone must block a surviving DB row.
        self.store._write_ledger({item['id']},pending=False)
        with self.assertRaises(PermissionError):self.ctx.object('memory',item['id'])

    def test_upgrade_epoch_atomic_failure_and_both_entry_points(self):
        from laowu import migrations
        from laowu.cli import parser,execute
        original=migrations._repair
        def fail(db,**kwargs):
            original(db,**kwargs)
            raise RuntimeError('injected migration failure')
        for version in ('1','2'):
            with self.store.connect() as db:
                db.execute("UPDATE memory_meta SET value=? WHERE key='schema_version'",(version,))
                db.execute("DELETE FROM memory_meta WHERE key='context_epoch'")
                with patch.object(migrations,'_repair',fail),self.assertRaises(RuntimeError):migrations.upgrade(db)
                self.assertEqual(db.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()[0],version)
                self.assertIsNone(db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone())
            Store(self.root)
            with self.store.connect() as db:
                epoch=db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone()[0]
                self.assertTrue(epoch)
                db.execute("DELETE FROM memory_meta WHERE key='context_epoch'")
            self.assertEqual(execute(parser().parse_args(['migrate']),self.root)['integrity'],'ok')
            with self.store.connect() as db:
                epoch=db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone()[0]
                migrations.upgrade(db)
                self.assertEqual(db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone()[0],epoch)


class RetryRecoveryTests(PhaseFixture):
    def test_first_read_after_healthy_gap_crash_keeps_new_reservation_window(self):
        retry=ReadRetry(self.store,lambda:self.clock[0])
        self.assertEqual(retry.call('gap','bridge.state',lambda:'ok'),'ok')
        self.clock[0]+=400
        # BaseException simulates abrupt death, bypassing normal failure handling.
        with self.assertRaises(SystemExit):retry.call('gap','bridge.state',lambda:(_ for _ in ()).throw(SystemExit()))
        with self.store.transaction() as db:
            row=db.execute('SELECT started,state FROM read_retries').fetchone()
            self.assertEqual(tuple(row),(1400,'reading'))
            db.execute("UPDATE read_retries SET owner_pid=999999,owner_identity='gone'")
        with self.assertRaises(RetryPending):ReadRetry(Store(self.root),lambda:self.clock[0]).call('gap','bridge.state',network_failure)
        row=retry.list()[0]
        self.assertEqual((row['started'],row['attempts'],row['total_attempts']),(1400,1,1))

    def test_success_closes_window_preserves_totals_and_concurrent_read_cannot_reset(self):
        retry=ReadRetry(self.store,lambda:self.clock[0])
        with self.assertRaises(RetryPending):retry.call('same','bridge.state',network_failure)
        self.clock[0]+=2
        entered=threading.Event();release=threading.Event()
        from concurrent.futures import ThreadPoolExecutor
        def blocked():entered.set();release.wait(5);return 'ok'
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(retry.call,'same','bridge.state',blocked);self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(RetryPending):ReadRetry(Store(self.root),lambda:self.clock[0]).call('same','bridge.state',lambda:self.fail('duplicate read'))
            finally:release.set()
            self.assertEqual(future.result(),'ok')
        row=retry.list()[0]
        self.assertEqual((row['state'],row['attempts'],row['total_attempts'],row['category']),('ready',0,1,None))
        self.clock[0]+=400
        with self.assertRaises(RetryPending):retry.call('same','bridge.state',network_failure)
        self.assertEqual(retry.list()[0]['started'],self.clock[0])

    def test_crashed_read_exhaustion_commits_and_explicit_recovery_retains_total(self):
        with self.store.transaction() as db:
            db.execute("INSERT INTO read_retries(id,operation,started,attempts,total_attempts,next_attempt,state,owner_pid,owner_identity) VALUES('crash','bridge.state',1000,3,7,1000,'reading',999999,'gone')")
        retry=ReadRetry(self.store,lambda:self.clock[0])
        with self.assertRaises(RetrySuspended):retry.call('crash','bridge.state',lambda:self.fail('exhausted'))
        self.assertEqual(retry.list()[0]['state'],'suspended')
        restarted=ReadRetry(Store(self.root),lambda:self.clock[0])
        with self.assertRaises(RetrySuspended):restarted.call('crash','bridge.state',lambda:self.fail('restart reset'))
        self.clock[0]+=400
        row=restarted.retry('crash')
        self.assertEqual((row['started'],row['attempts'],row['total_attempts'],row['resumptions']),(1400,0,7,1))
        self.assertEqual(restarted.call('crash','bridge.state',lambda:'ok'),'ok')
        self.assertEqual(restarted.list()[0]['total_attempts'],7)

    def test_read_only_cli_wakes_native_uncertain_without_sending(self):
        from laowu.background import Outbox
        from laowu.cli import parser,execute
        event=Outbox(self.store).publish('合成通知',event_key='recover-native')
        with self.store.transaction() as db:
            db.execute("UPDATE notifications SET status='uncertain',target_thread='native',next_attempt=9999999999 WHERE id=?",(event['id'],))
        retry=ReadRetry(self.store,lambda:self.clock[0]);key=event['id']+':receipt'
        with self.assertRaises(RetrySuspended):retry.call(key,'bridge.receipt',lambda:(_ for _ in ()).throw(PermissionError()))
        with patch.dict(os.environ,{'LAOWU_ROLE':'main','LAOWU_MANAGED_WORKER':'0'}):
            self.assertEqual(len(execute(parser().parse_args(['reads','list']),self.root)),1)
            execute(parser().parse_args(['reads','retry',key]),self.root)
        live=Outbox(self.store).get(event['id'])
        self.assertEqual((live['status'],live['next_attempt']),('uncertain',0))
        from laowu.daemon import Dispatcher
        class Bridge:
            sends=0;receipts=0
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def loaded(self):return []
            def receipt(self,*args):self.receipts+=1;return ('delivered','turn-original')
            def send(self,*args):self.sends+=1;raise AssertionError('must not send')
        bridge=Bridge();dispatcher=Dispatcher(self.store,bridge_factory=lambda:bridge)
        dispatcher.tick()
        self.assertEqual((bridge.sends,bridge.receipts),(0,1))
        self.assertEqual(Outbox(self.store).get(event['id'])['status'],'delivered')

    def test_legacy_notification_retry_cannot_demote_unknown_submission(self):
        from laowu.background import Outbox
        box=Outbox(self.store);event=box.publish('合成未知提交',event_key='unknown-native')
        with self.store.transaction() as db:
            db.execute("UPDATE notifications SET status='uncertain',target_thread=NULL,turn_id=NULL WHERE id=?",(event['id'],))
        self.assertEqual(box.retry(event['id'])['status'],'uncertain')


class SlackReviewFixture(PhaseFixture):
    def setUp(self):
        super().setUp()
        from laowu.slack_state import SlackState
        from test_slack_gateway import FakeAPI,FakeSession
        self.state=SlackState(self.store)
        self.state.set_config(dict(team_id='TONE',owner_user_id='UOWNER',bot_user_id='UBOT',enabled=True))
        self.state.update_channel('TONE','DONE',personal=1,last_activity=99999999)
        self.source=self.state.enqueue('TONE','DONE','10.1','合成输入',context='normal',reply_ts='10.0')
        self.api=FakeAPI();FakeSession.created=[];FakeSession.gate=None
        self.session_type=FakeSession

    def gateway(self):
        from laowu.slack_gateway import SlackGateway
        g=SlackGateway(self.state,api=self.api,session_factory=self.session_type,clock=lambda:self.clock[0])
        self.addCleanup(g.close)
        return g


class SlackReadRecoveryTests(SlackReviewFixture):
    def test_pending_output_recovery_allows_first_send_once(self):
        g=self.gateway();out=self.state.queue_output(self.source['id'],'从未提交的输出')
        with patch.object(self.api,'info',side_effect=ConnectionError()):
            for delta in (0,2,10,30):self.clock[0]+=delta;g.deliver_outputs()
        self.assertEqual(self.state.outputs()[0]['status'],'blocked')
        self.assertEqual(len(self.api.posts),0)
        g.close();self.state=type(self.state)(Store(self.root));g=self.gateway()
        g.reads.retry(out['id']+':qualify:info')
        self.assertEqual(self.state.outputs()[0]['status'],'pending')
        g.deliver_outputs();g.deliver_outputs()
        self.assertEqual(len(self.api.posts),1)
        self.assertEqual(self.state.outputs()[0]['status'],'sent')

    def test_blocked_output_recovery_after_restart_reconciles_without_post(self):
        g=self.gateway();out=self.state.queue_output(self.source['id'],'合成输出')
        self.state.update_output(out['id'],status='uncertain')
        with patch.object(self.api,'reconcile',side_effect=ConnectionError()):
            for delta in (0,2,10,30):self.clock[0]+=delta;g.deliver_outputs()
        self.assertEqual(self.state.outputs()[0]['status'],'blocked')
        g.close();self.state=type(self.state)(Store(self.root));g=self.gateway()
        g.reads.retry(out['id']+':reconcile')
        self.assertEqual(self.state.outputs()[0]['status'],'uncertain')
        with patch.object(self.api,'reconcile',return_value='receipt-original') as read:
            g.deliver_outputs()
        self.assertEqual(read.call_count,1);self.assertEqual(len(self.api.posts),0)
        self.assertEqual(self.state.outputs()[0]['status'],'sent')

    def test_input_lost_submission_no_turn_id_recovery_uses_durable_uncertainty(self):
        g=self.gateway()
        self.state.update_input(self.source['id'],status='processing')
        def lost_submission(session, *args, **kwargs):
            with session.turn_admission():
                raise ConnectionError()
        with patch.object(self.session_type,'start_turn',autospec=True,side_effect=lost_submission) as start:
            g._execute(dict(self.source))
        self.assertEqual(start.call_count,1)
        record=self.state.inbox()[0]
        self.assertEqual(record['status'],'uncertain');self.assertIsNone(record['turn_id'])
        with patch.object(self.session_type,'history_receipt',side_effect=ConnectionError()):
            for delta in (0,2,10,30):
                self.clock[0]+=delta
                record=self.state.inbox()[0]
                self.state.update_input(record['id'],status='processing')
                g._execute(dict(record,_reconcile=True))
        self.assertEqual(self.state.inbox()[0]['status'],'blocked')
        g.close();self.state=type(self.state)(Store(self.root));g=self.gateway()
        g.reads.retry(self.source['id']+':receipt')
        record=self.state.inbox()[0];self.assertEqual(record['status'],'uncertain')
        self.state.update_input(record['id'],status='processing')
        with patch.object(self.session_type,'start_turn',side_effect=AssertionError('duplicate action')) as start:
            g._execute(record)
        self.assertEqual(start.call_count,0)
        self.assertEqual(self.state.inbox()[0]['status'],'completed')

    def test_pre_submission_qualification_can_resume_but_missing_proof_stays_uncertain(self):
        g=self.gateway();self.state.update_input(self.source['id'],status='processing')
        with patch.object(self.api,'info',side_effect=ConnectionError()):
            for delta in (0,2,10,30):
                self.clock[0]+=delta;g._execute(dict(self.source))
        self.assertEqual(self.state.inbox()[0]['status'],'blocked')
        key=self.source['id']+':qualify:info'
        g.reads.retry(key)
        self.assertEqual(self.state.inbox()[0]['status'],'pending')
        with self.store.transaction() as db:
            db.execute("UPDATE read_retries SET state='suspended' WHERE id=?",(key,))
            db.execute("UPDATE slack_inbox SET status='blocked' WHERE id=?",(self.source['id'],))
            db.execute('DELETE FROM read_sources')
        g.reads.retry(key)
        self.assertEqual(self.state.inbox()[0]['status'],'uncertain')

    def test_monitor_and_metric_busy_errors_do_not_change_turn_or_leak_active(self):
        g=self.gateway();self.state.update_input(self.source['id'],status='processing')
        with patch.object(g.monitor,'start',side_effect=sqlite3.OperationalError('database is locked')), \
             patch.object(g.monitor,'observe',side_effect=RuntimeError('monitor failure')), \
             patch('laowu.metrics.begin',side_effect=sqlite3.OperationalError('database is locked')), \
             patch('laowu.metrics.finish',side_effect=RuntimeError('metric failure')):
            g._execute(dict(self.source))
        self.assertEqual(self.state.inbox()[0]['status'],'completed')
        self.assertEqual(g.active,{})
        self.assertTrue(g.wake.is_set())
        self.assertEqual(len(self.session_type.created[0].starts),1)

    def test_discover_uncertain_input_and_notification_without_prior_instance(self):
        from laowu.monitoring import Monitor
        notice=self.state.enqueue('TONE','DONE','notice-1','合成通知',origin_notice='notice-original')
        with self.store.transaction() as db:
            db.execute("UPDATE slack_inbox SET status='uncertain',updated_at=?",(datetime.fromtimestamp(1000,timezone.utc).isoformat(),))
        self.clock[0]+=601
        mon=Monitor(self.store,lambda:self.clock[0]);self.assertEqual(len(mon.scan()),2)
        self.assertEqual(mon.scan(),[])
        self.assertEqual({r['input_id'] for r in self.state.outputs()},{self.source['id'],notice['id']})


class MonitorPolicyTests(PhaseFixture):
    def test_generic_tool_has_new_30_minute_window_aliases_and_dedup(self):
        from laowu.monitoring import Monitor,event_metadata
        task=self.task();run=self.run_task(task);mon=Monitor(self.store,lambda:self.clock[0]);mon.start(run,'worker',task_id=task['id'])
        self.clock[0]+=1000
        event=event_metadata(dict(type='item.started',item=dict(id='cmd',type='command_execution')))
        mon.observe(run,**event)
        self.assertEqual(mon.scan(),[])
        self.clock[0]+=1799;mon.observe(run,**event)
        self.assertEqual(mon.scan(),[])
        self.clock[0]+=1;self.assertEqual(len(mon.scan()),1)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM monitor_tools').fetchone()[0],1)
        meta=event_metadata(dict(type='item/started',params=dict(item=dict(id='img',type='mcpToolCall',tool='mcp__image_gen__imagegen'))))
        self.assertEqual(meta['phase'],'long_tool')
        mon.observe(run,**meta)
        mon.observe(run,phase='approval_wait');self.assertEqual(mon.scan(),[])
        self.clock[0]+=600;mon.scan()
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT type FROM monitor_alerts WHERE cleared IS NULL').fetchone()[0],'approval_wait')

    def test_numeric_policy_validation_live_thresholds_and_backup_restore(self):
        from laowu.policy import settings,DEFAULTS
        from laowu.features import settings as flags
        from laowu.monitoring import Monitor
        path=self.root/'config/runtime-policy.json'
        self.assertEqual(settings(self.store),DEFAULTS)
        self.assertTrue(all(type(v) is bool for v in flags(self.store).values()))
        for value in ({'response_bytes':True},{'reference_ttl_seconds':0},{'monitor_scan_seconds':float('nan')},{'read_extra_attempts':2},{'read_delays_seconds':[2,30,10]},{'unknown':2},{'page_bytes':100000}):
            path.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(ValueError):settings(self.store)
        path.write_text(json.dumps(dict(response_bytes=8192,page_bytes=2048,reference_ttl_seconds=5,working_seconds=12)))
        mem=self.store.memory_upsert('自定义预算'*4000,scope='shared')
        shown=self.ctx.present('memory_search',{},[mem]);self.assertLessEqual(body_size(shown),8192)
        ref=next(self.refs(shown));self.assertLessEqual(body_size(self.ctx.read(ref)),8192)
        task=self.task();run=self.run_task(task);mon=Monitor(self.store,lambda:self.clock[0]);mon.start(run,'worker',task_id=task['id'])
        self.clock[0]+=13
        with self.assertRaises(PermissionError):self.ctx.read(ref)
        self.assertEqual(len(mon.scan()),1)
        self.svc.close()
        snapshot=self.store.backup()['path']
        path.write_text('{}');self.store.restore(snapshot)
        self.assertEqual(settings(self.store)['working_seconds'],12)
        # A pre-policy snapshot must restore defaults, not keep a later policy.
        path.unlink();(self.root/'config/features.json').unlink()
        old=self.store.backup()['path']
        path.write_text('{"working_seconds":13}');self.configure()
        self.store.restore(old)
        self.assertEqual(settings(self.store),DEFAULTS)
        self.assertFalse(any(flags(self.store).values()))

    def test_configured_retry_attempts_delays_and_deadline(self):
        (self.root/'config/runtime-policy.json').write_text(json.dumps(dict(read_extra_attempts=1,read_delays_seconds=[5],read_window_seconds=6)))
        retry=ReadRetry(self.store,lambda:self.clock[0])
        with self.assertRaises(RetryPending):retry.call('custom','bridge.state',network_failure)
        self.assertEqual(retry.list()[0]['next_attempt'],1005)
        self.clock[0]+=5
        with self.assertRaises(RetrySuspended):retry.call('custom','bridge.state',network_failure)
        self.assertEqual(retry.list()[0]['total_attempts'],1)

    def test_worker_metrics_and_monitor_failures_leave_no_process_fds_or_runtime(self):
        from laowu.worker import run
        task=self.task();ident=self.run_task(task)
        folder=self.store.private/'runtime'/ident;folder.mkdir(parents=True)
        message=json.dumps(dict(type='item.completed',item=dict(type='agent_message',text=json.dumps(self.result()))))
        program='import sys;sys.stdin.read();print('+repr(message)+')'
        (folder/'launch.json').write_text(json.dumps(dict(argv=[sys.executable,'-c',program],env={},cwd=str(self.root),stdin='synthetic')))
        readfd,writefd=os.pipe()
        handlers={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
        import subprocess
        processes=[];popen=subprocess.Popen
        def spawn(*args,**kwargs):
            process=popen(*args,**kwargs);processes.append(process);return process
        try:
            with patch('laowu.monitoring.Monitor.scan',side_effect=sqlite3.OperationalError('database is locked')), \
                 patch('laowu.monitoring.Monitor.observe',side_effect=RuntimeError('monitor unavailable')), \
                 patch('laowu.metrics.sample',side_effect=RuntimeError('metric unavailable')), \
                 patch('laowu.worker.subprocess.Popen',spawn):
                self.assertEqual(run(self.store,ident,readfd),0)
            self.assertEqual(task_record(self.store,task['id'])['status'],'completed')
            self.assertFalse(folder.exists())
            # SQLite may reuse the numeric fd after cleanup; assert the lease
            # pipe itself has no reader, rather than inspecting a recycled fd.
            with self.assertRaises(BrokenPipeError):os.write(writefd,b'x')
            for process in processes:
                self.assertIsNotNone(process.poll())
                self.assertTrue(all(stream.closed for stream in (process.stdin,process.stdout,process.stderr)))
        finally:
            os.close(writefd)
            for sig,handler in handlers.items():signal.signal(sig,handler)
