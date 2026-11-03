"""Deterministic phase-one acceptance. Temporary roots; no model/service/network."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from laowu.common import now
from laowu.context import Context, body_size, dumps
from laowu.features import DEFAULTS
from laowu.runtime import Manager, finish_run, task_record, checkpoint
from laowu.service import Service
from laowu.store import Store


class PhaseFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='phase1-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'config').mkdir()
        self.flags=DEFAULTS | {k:True for k in DEFAULTS}
        self.configure()
        self.schedule=patch.object(Manager,'_schedule',return_value=None)
        self.schedule.start()
        self.addCleanup(self.schedule.stop)
        self.svc=Service(self.root)
        self.addCleanup(self.svc.close)
        self.store=self.svc.store
        self.svc.registry.ensure(dict(id='engineer',name='工程师',description='代码',instructions='工程测试',skills=[],tools=['memory','checkpoint'],can_write=False))
        self.clock=[1000.]
        self.ctx=Context(self.svc,clock=lambda:self.clock[0])

    def configure(self,**values):
        self.flags.update(values)
        (self.root/'config/features.json').write_text(json.dumps(self.flags))

    def task(self,request='限定任务',key=None):
        return self.svc.call('task_start',dict(friend_id='engineer',request=request,**({'idempotency_key':key} if key else {})))

    def run_task(self,task,run_id='run_one'):
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='running' WHERE id=?",(task['id'],))
            db.execute("INSERT INTO runs(id,task_id,session_id,friend_id,friend_version,client,status,can_write,workspace,spec_json,token_hash,started_at,updated_at) VALUES(?,?,?,'engineer',1,'codex','running',0,?,'{}',?,?,?)",
                       (run_id,task['id'],self.svc.manager.id,str(self.root),hashlib.sha256(b'test-token').hexdigest(),now(),now()))
        return run_id

    def result(self,text='完整交接',**extra):
        return dict(result=text,artifacts=['/tmp/artifact'],validation=['fake检查通过'],unfinished=[],memory_candidates=[],**extra)

    def finish(self,task,run_id='run_one',result=None,status='completed'):
        self.run_task(task,run_id)
        finish_run(self.store,run_id,status,result or self.result())
        return task_record(self.store,task['id'])

    def refs(self,value):
        if isinstance(value,dict):
            if 'ref' in value and value.get('complete') is False:
                yield value['ref']
            else:
                for child in value.values(): yield from self.refs(child)
        elif isinstance(value,list):
            for child in value: yield from self.refs(child)

    def read_all(self,ctx,ref):
        cursor=None; pieces=[]
        while True:
            page=ctx.read(ref,cursor)
            self.assertLessEqual(body_size(page),16384)
            self.assertLessEqual(len(dumps(page['data']).encode()),8192)
            pieces.append(page['data'])
            cursor=page['next_cursor']
            if not cursor: break
        return json.loads(''.join(pieces))


class BudgetTests(PhaseFixture):
    def test_chinese_nested_result_full_resume_and_wire_bytes(self):
        t=self.task('中文请求'*6000)
        payload=self.result('中文结果🌟\\"\n'*12000)
        payload['memory_candidates']=[{'type':'fact','nested':[['嵌套'*80]*6]*5} for _ in range(15)]
        payload['unfinished']=['重要未完成事项'*600]
        record=self.finish(t,result=payload,status='blocked')
        shown=self.ctx.present('task_status',{'task_id':t['id']},record)
        self.assertLessEqual(body_size(shown),16384)
        self.assertEqual(shown['version'],1)
        self.assertIn('unfinished',dumps(shown))
        refs=list(self.refs(shown))
        self.assertGreater(len(refs),0)
        recovered=[self.read_all(self.ctx,r) for r in refs]
        self.assertIn(record['request'],recovered)
        self.assertIn(payload['result'],recovered)
        resumed=self.svc.manager.resume(t['id'])
        self.assertEqual(resumed['request'],record['request'])
        self.assertIsNone(resumed['result'])
        with self.assertRaises(ValueError): self.ctx.read(refs[0])

    def test_bootstrap_large_lists_budget_and_complete_list(self):
        for i in range(35):
            self.svc.call('memory_upsert',dict(content=('中文记忆'+str(i))*3000,scope='shared'))
        for i in range(25): self.task(str(i)+'请求'*100)
        full=self.svc.call('bootstrap',{})
        shown=self.ctx.present('bootstrap',{},full)
        self.assertLessEqual(body_size(shown),32768)
        self.assertEqual(len(full['tasks']),25)
        ref=shown['data']['memories']['ref']
        self.assertEqual(self.read_all(self.ctx,ref),full['memories'])
        self.assertEqual(self.read_all(self.ctx,shown['data']['tasks']['ref']),full['tasks'])

    def test_search_revision_expiry_and_arbitrary_ref_rejected(self):
        mem=self.svc.call('memory_upsert',dict(content='长记忆'*20000,scope='shared'))
        found=self.svc.call('memory_search',{})
        shown=self.ctx.present('memory_search',{},found)
        ref=next(self.refs(shown))
        self.assertEqual(self.read_all(self.ctx,ref),found[0]['content'])
        page=self.ctx.read(ref)
        self.clock[0]+=901
        with self.assertRaises(PermissionError): self.ctx.read(ref,page['next_cursor'])
        self.clock[0]-=901
        self.svc.call('memory_upsert',dict(memory_id=mem['id'],content='修订',scope='shared'))
        with self.assertRaises(ValueError): self.ctx.read(ref)
        with self.assertRaises(PermissionError): self.ctx.read('/etc/passwd')

    def test_worker_scope_and_token_revalidated_every_page(self):
        t=self.task();run=self.run_task(t)
        worker=Service(self.root,worker_run=run,worker_token='test-token')
        self.addCleanup(worker.close)
        self.svc.call('memory_upsert',dict(content='工程共享'*6000,scope='shared'))
        self.svc.call('memory_upsert',dict(content='个人'*2000,scope='personal'))
        ctx=Context(worker,clock=lambda:self.clock[0])
        data=worker.call('memory_search',{})
        self.assertEqual({v['scope'] for v in data},{'shared'})
        ref=next(self.refs(ctx.present('memory_search',{},data)))
        self.assertTrue(ctx.read(ref)['data'])
        with self.assertRaises(PermissionError): self.ctx.read(ref)
        with self.assertRaises(PermissionError): worker.call('task_review',dict(task_id=t['id']))
        with self.store.transaction() as db: db.execute("UPDATE runs SET token_hash='revoked' WHERE id=?",(run,))
        with self.assertRaises(ValueError): ctx.read(ref)

    def test_forget_restore_epoch_and_no_cached_bodies(self):
        memory=self.svc.call('memory_upsert',dict(content='需要遗忘'*12000,scope='shared'))
        data=self.svc.call('memory_search',{})
        ref=next(self.refs(self.ctx.present('memory_search',{},data)))
        snapshot=self.store.backup()['path']
        copy=self.root/'old.zip';copy.write_bytes(Path(snapshot).read_bytes())
        with self.store.connect() as db:
            desc=db.execute('SELECT descriptor FROM context_refs').fetchone()[0]
            self.assertNotIn('需要遗忘',desc)
        self.svc.close()
        self.store.memory_forget(memory['id'])
        with self.assertRaises(PermissionError): self.ctx.read(ref)
        self.store.restore(copy)
        self.assertEqual(self.store.memory_search(),[])
        with self.assertRaises(PermissionError): self.ctx.read(ref)

    def test_start_wait_checkpoint_bypasses_are_budgeted(self):
        t=self.task('请求'*15000)
        for tool in ('task_start','task_status','task_wait'):
            value=t if tool=='task_start' else self.svc.call(tool,dict(task_id=t['id'],**({'timeout':0} if tool=='task_wait' else {})))
            shown=self.ctx.present(tool,{'task_id':t['id']},value)
            self.assertLessEqual(body_size(shown),16384)
            self.assertEqual(self.read_all(self.ctx,next(self.refs(shown))),t['request'])
        value=self.svc.call('task_checkpoint',dict(task_id=t['id'],summary='检查点'*2600,artifacts=['/tmp/产物'*100]*100))
        shown=self.ctx.present('task_checkpoint',{'task_id':t['id']},value)
        self.assertLessEqual(body_size(shown),16384)
        self.assertEqual(self.read_all(self.ctx,shown['data']['artifacts']['ref']),value['artifacts'])


class AcceptanceTests(PhaseFixture):
    def review(self,t,decision='accepted',run='run_one'):
        return self.svc.call('task_review',dict(task_id=t['id'],run_id=run,decision=decision,summary='已核对',evidence=['fake测试']))

    def test_legacy_old_resume_idempotency_and_new_registration(self):
        self.configure(task_acceptance=False)
        old=self.task(key='old');self.assertEqual(old['acceptance']['state'],'legacy')
        self.configure(task_acceptance=True)
        self.assertEqual(self.task(key='old')['acceptance']['state'],'legacy')
        self.finish(old,status='blocked')
        self.assertEqual(self.svc.manager.resume(old['id'])['acceptance']['state'],'legacy')
        with self.assertRaises(ValueError): self.review(old)
        self.assertEqual(self.task()['acceptance']['state'],'not_ready')

    def test_pending_needs_work_resume_history_and_late_result(self):
        t=self.task(); record=self.finish(t)
        self.assertEqual(record['status'],'completed')
        self.assertEqual(record['acceptance']['state'],'pending')
        self.assertIn(t['id'],[x['id'] for x in self.svc.call('bootstrap',{})['tasks']])
        self.review(t,'needs_work')
        resumed=self.svc.manager.resume(t['id'])
        self.assertIsNone(resumed['result'])
        self.assertEqual(resumed['acceptance']['state'],'not_ready')
        self.assertEqual(len(resumed['acceptance']['history']),1)
        self.run_task(t,'run_two')
        self.assertFalse(finish_run(self.store,'run_one','completed',self.result('旧结果')))
        with self.assertRaises(ValueError): self.review(t)
        finish_run(self.store,'run_two','completed',self.result('新结果'))
        self.review(t,run='run_two')
        self.assertNotIn(t['id'],[x['id'] for x in self.svc.call('bootstrap',{})['tasks']])
        self.assertEqual(task_record(self.store,t['id'])['result']['result'],'新结果')

    def test_idempotent_conflict_and_concurrent_review(self):
        t=self.task();self.finish(t)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.review(t),range(2)))
        self.assertEqual(len(results[0]['history']),1)
        with self.assertRaises(ValueError): self.review(t,'needs_work')

    def test_failed_without_handoff_not_ready_running_denied(self):
        t=self.task();run=self.run_task(t)
        with self.assertRaises(ValueError): self.review(t)
        finish_run(self.store,run,'failed',None,'invalid_handoff')
        self.assertEqual(task_record(self.store,t['id'])['acceptance']['state'],'not_ready')
        with self.assertRaises(ValueError): self.review(t)

    def test_result_mutation_denies_stale_version(self):
        t=self.task();self.finish(t)
        with self.store.transaction() as db: db.execute('UPDATE tasks SET result=? WHERE id=?',(json.dumps(self.result('changed')),t['id']))
        with self.assertRaises(ValueError): self.review(t)

    def test_forgetting_scrubs_review_and_monitor_data(self):
        t=self.task();self.finish(t);self.review(t)
        m=self.store.memory_upsert('待忘',scope='shared')
        self.store.memory_forget(m['id'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM task_reviews').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM context_refs').fetchone()[0],0)


class MonitorTests(PhaseFixture):
    def setUp(self):
        super().setUp()
        from laowu.monitoring import Monitor
        from laowu.background import initialize_background
        initialize_background(self.store)
        self.monitor=Monitor(self.store,lambda:self.clock[0])
        self.t=self.task();self.run=self.run_task(self.t)
        self.monitor.start(self.run,'worker',task_id=self.t['id'],origin='native-thread',context='root')

    def test_stream_heartbeat_not_progress_once_and_clear(self):
        self.clock[0]+=601
        self.monitor.observe(self.run,heartbeat=True,client_event=True)
        self.assertEqual(len(self.monitor.scan()),1)
        self.assertEqual(self.monitor.scan(),[])
        with self.store.connect() as db:
            n=db.execute('SELECT * FROM notifications').fetchone()
            self.assertEqual(n['origin_thread'],'native-thread');self.assertEqual(n['context'],'root')
            self.assertEqual(db.execute('SELECT progress FROM monitor_instances').fetchone()[0],1000)
        self.monitor.observe(self.run,progress_key='tool-id-one',phase='working')
        self.monitor.scan()
        with self.store.connect() as db: self.assertIsNotNone(db.execute('SELECT cleared FROM monitor_alerts').fetchone()[0])
        self.clock[0]+=601
        self.monitor.observe(self.run,progress_key='tool-id-one')
        self.assertEqual(len(self.monitor.scan()),1)

    def test_long_tool_waiting_cancellation_and_reboot_dedup(self):
        self.monitor.observe(self.run,phase='long_tool')
        self.clock[0]+=601
        self.assertEqual(self.monitor.scan(),[])
        self.clock[0]+=1200
        self.assertEqual(len(self.monitor.scan()),1)
        from laowu.monitoring import Monitor
        self.assertEqual(Monitor(self.store,lambda:self.clock[0]).scan(),[])
        self.monitor.observe(self.run,phase='approval_wait')
        self.assertEqual(self.monitor.scan(),[])
        self.clock[0]+=601
        self.assertEqual(len(self.monitor.scan()),1)
        self.svc.call('task_cancel',dict(task_id=self.t['id']))
        self.monitor.scan()
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM monitor_alerts WHERE cleared IS NULL').fetchone()[0],0)

    def test_checkpoint_only_content_changes_advance(self):
        with patch('laowu.monitoring.time.time',return_value=1100): checkpoint(self.store,self.t['id'],'检查内容')
        with patch('laowu.monitoring.time.time',return_value=1300): checkpoint(self.store,self.t['id'],'检查内容')
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT progress FROM monitor_instances').fetchone()[0],1100)
        with patch('laowu.monitoring.time.time',return_value=1400): checkpoint(self.store,self.t['id'],'新的检查内容')
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT progress FROM monitor_instances').fetchone()[0],1400)

    def test_sanitized_event_parser_and_nullable_usage(self):
        from laowu.monitoring import event_metadata
        from laowu.metrics import sample,aggregate,usage
        event=event_metadata({'type':'item.completed','item':{'id':'abc','type':'command_execution','aggregated_output':'秘密日志'}})
        self.assertNotIn('秘密日志',json.dumps(event))
        self.assertTrue(event['progress_key'])
        self.assertEqual(usage({'inputTokens':7,'outputTokens':2,'reasoning':'secret'}),{'input_tokens':7,'output_tokens':2})
        sample(self.store,self.run,'worker',input_tokens=7)
        total=aggregate(self.store)['samples'][0]
        self.assertEqual(total['input_tokens'],7)
        self.assertIsNone(total['output_tokens'])
        self.assertIsNone(total['total_tokens'])


class RetryTests(PhaseFixture):
    def test_delays_three_extra_attempts_restart_does_not_reset(self):
        from laowu.retries import ReadRetry,RetryPending,RetrySuspended
        calls=[]
        def fail(): calls.append(1);raise TimeoutError()
        for delta in (0,2,10):
            self.clock[0]+=delta
            with self.assertRaises(RetryPending): ReadRetry(self.store,lambda:self.clock[0]).call('stable','slack.receipt',fail)
            with self.assertRaises(RetryPending): ReadRetry(self.store,lambda:self.clock[0]).call('stable','slack.receipt',fail)
        self.clock[0]+=30
        with self.assertRaises(RetrySuspended): ReadRetry(self.store,lambda:self.clock[0]).call('stable','slack.receipt',fail)
        self.clock[0]+=1000
        with self.assertRaises(RetrySuspended): ReadRetry(self.store,lambda:self.clock[0]).call('stable','slack.receipt',fail)
        self.assertEqual(len(calls),4)
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT attempts FROM read_retries').fetchone()[0],3)

    def test_retry_after_over_budget_permissions_unknown_and_write_denied(self):
        from laowu.retries import ReadRetry,RetrySuspended
        from laowu.slack_client import SlackAPIError
        retry=ReadRetry(self.store,lambda:self.clock[0])
        for i,exc in enumerate((SlackAPIError('ratelimited',retry_after=301),PermissionError(),RuntimeError(),SlackAPIError('missing_scope'),SlackAPIError('quota_exceeded'))):
            def fail(exc=exc): raise exc
            with self.assertRaises(RetrySuspended): retry.call(str(i),'slack.receipt',fail)
        with self.assertRaises(PermissionError): retry.call('write','turn/start',lambda:None)

    def test_remaining_budget_is_shared_by_paginated_transport_reads(self):
        from laowu.retries import ReadRetry,RetryPending,RetrySuspended,remaining
        retry=ReadRetry(self.store,lambda:self.clock[0])
        with self.assertRaises(RetryPending):retry.call('pages','slack.receipt',lambda:(_ for _ in ()).throw(TimeoutError()))
        self.clock[0]+=2
        observed=[]
        def paginated():
            self.clock[0]=1299
            observed.append(remaining(30))
            self.clock[0]=1300
            remaining(30)
            self.fail('超预算不能继续下一页')
        with self.assertRaises(RetrySuspended):retry.call('pages','slack.receipt',paginated)
        self.assertEqual(observed,[1])
        self.assertEqual(remaining(30),30)

    def test_transient_success_and_five_minute_deadline(self):
        from laowu.retries import ReadRetry,RetryPending,RetrySuspended
        retry=ReadRetry(self.store,lambda:self.clock[0])
        with self.assertRaises(RetryPending): retry.call('ok','bridge.state',lambda:(_ for _ in ()).throw(ConnectionError()))
        self.clock[0]+=2
        self.assertEqual(retry.call('ok','bridge.state',lambda:'idle'),'idle')
        with self.assertRaises(RetryPending): retry.call('deadline','bridge.state',lambda:(_ for _ in ()).throw(ConnectionError()))
        self.clock[0]+=301
        with self.assertRaises(RetrySuspended): retry.call('deadline','bridge.state',lambda:'must not run')


class MigrationTests(PhaseFixture):
    def test_idempotent_upgrade_and_future_refused(self):
        with self.store.connect() as db: db.execute("UPDATE memory_meta SET value='1' WHERE key='schema_version'")
        Store(self.root);Store(self.root)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()[0],'2')
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            db.execute("UPDATE memory_meta SET value='99' WHERE key='schema_version'")
        with self.assertRaises(ValueError): Store(self.root)
        # avoid cleanup using a future schema
        with self.store.connect() as db: db.execute("UPDATE memory_meta SET value='2' WHERE key='schema_version'")

    def test_old_backup_upgraded_in_temporary_copy_and_roundtrip(self):
        self.svc.close()
        self.store.memory_upsert('旧版内容',scope='shared')
        path=Path(self.store.backup()['path'])
        with zipfile.ZipFile(path) as z: files={n:z.read(n) for n in z.namelist()}
        database=self.root/'old.sqlite';database.write_bytes(files['state.sqlite3'])
        with sqlite3.connect(database) as db:
            db.execute("UPDATE memory_meta SET value='1' WHERE key='schema_version'")
            for table in ('context_cursors','context_refs','task_reviews','task_acceptance','monitor_instances','monitor_tools','monitor_progress','monitor_alerts','read_retries','metric_samples','metric_timings','execution_routes','slack_result_versions'):
                db.execute('DROP TABLE '+table)
            db.execute("DELETE FROM memory_meta WHERE key='context_epoch'")
        files['state.sqlite3']=database.read_bytes()
        manifest=json.loads(files['manifest.json']);manifest['schema_version']=1
        manifest['files']['state.sqlite3']=hashlib.sha256(files['state.sqlite3']).hexdigest()
        files.pop('assets/config/features.json',None)
        manifest['files'].pop('assets/config/features.json',None)
        files['manifest.json']=json.dumps(manifest).encode()
        old=self.root/'old.zip'
        with zipfile.ZipFile(old,'w') as z:
            for n,data in files.items():z.writestr(n,data)
        self.store.restore(old)
        self.assertEqual(self.store.memory_search()[0]['content'],'旧版内容')
        new=self.store.backup()['path'];self.store.restore(new)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()[0],'2')
            self.assertEqual(db.execute('SELECT count(*) FROM monitor_instances').fetchone()[0],0)


class SlackBoundaryTests(PhaseFixture):
    def setUp(self):
        super().setUp()
        from laowu.slack_state import SlackState
        self.state=SlackState(self.store)
        self.state.set_config(dict(team_id='T01',owner_user_id='U01',bot_user_id='B01',enabled=True))
        self.state.update_channel('T01','D01',personal=1,last_activity=99999999)
        self.source=self.state.enqueue('T01','D01','10.1','synthetic input',context='root',reply_ts='10.0')
        self.state.runtime(status='connected')
        self.slack=Service(self.root,slack_token_file=self.state.issue_binding('T01','D01'))
        self.addCleanup(self.slack.close)
        self.sc=Context(self.slack,clock=lambda:self.clock[0])

    def test_result_grant_does_not_leak_request_checkpoint_or_authorize_review(self):
        from laowu.background import Outbox
        from laowu.slack_notifications import route_slack_notifications
        t=self.task('不能给结果受众的请求')
        self.svc.call('task_checkpoint',dict(task_id=t['id'],summary='不能给结果受众的检查点'))
        self.finish(t,result=self.result('公开结果'*9000))
        box=Outbox(self.store)
        event=box.publish('任务完成',event_key='task-result',task_id=t['id'])
        route_slack_notifications(self.store)
        value=self.slack.call('task_status',dict(task_id=t['id']))
        self.assertNotIn('request',value);self.assertNotIn('checkpoint',value)
        ref=next(self.refs(self.sc.present('task_status',{'task_id':t['id']},value)))
        self.assertEqual(self.read_all(self.sc,ref),'公开结果'*9000)
        with self.assertRaises(PermissionError): self.slack.call('task_review',dict(task_id=t['id'],run_id='run_one',decision='accepted',summary='越权',evidence=[]))
        self.svc.call('task_review',dict(task_id=t['id'],run_id='run_one',decision='needs_work',summary='返工',evidence=['交接仍有未完成事项']))
        self.svc.manager.resume(t['id'])
        with self.assertRaises(PermissionError): self.sc.read(ref)
        with self.assertRaises(PermissionError): self.slack.call('task_status',dict(task_id=t['id']))

    def test_channel_generation_scope_and_revocation_rechecked(self):
        mem=self.slack.call('memory_upsert',dict(content='私密内容'*8000,scope='personal'))
        data=self.slack.call('memory_search',{})
        ref=next(self.refs(self.sc.present('memory_search',{},data)))
        self.assertTrue(self.sc.read(ref)['data'])
        self.state.update_channel('T01','D02',personal=1)
        other=Service(self.root,slack_token_file=self.state.issue_binding('T01','D02'))
        self.addCleanup(other.close)
        with self.assertRaises(PermissionError): Context(other).read(ref)
        self.state.reset('T01','D01')
        with self.assertRaises(PermissionError): self.sc.read(ref)

    def test_scoped_review_requires_owned_latest_run(self):
        t=self.task()
        with self.store.transaction() as db:
            db.execute('INSERT INTO slack_task_grants VALUES(?,?,?,?,?)',(t['id'],'slack:T01:D01:0','personal','10.0',self.source['id']))
        self.finish(t)
        got=self.slack.call('task_review',dict(task_id=t['id'],run_id='run_one',decision='accepted',summary='范围内验收',evidence=['fake']))
        self.assertEqual(got['state'],'accepted')

    def test_stuck_slack_turn_alert_direct_outbox_context_and_notification_turn(self):
        from laowu.monitoring import Monitor
        self.state.update_input(self.source['id'],status='processing')
        mon=Monitor(self.store,lambda:self.clock[0])
        mon.start('slack:'+self.source['id'],'slack',input_id=self.source['id'],origin='slack:T01:D01:0',context='root',audience='personal',generation=0)
        self.clock[0]+=601
        self.assertEqual(len(mon.scan()),1)
        self.assertEqual(mon.scan(),[])
        self.assertEqual(len(self.state.inbox()),1)
        output=self.state.outputs()[0]
        self.assertEqual(output['input_id'],self.source['id'])
        self.assertTrue(output['body'].startswith('<root>'))
        self.assertEqual(self.state.inbox()[0]['reply_ts'],'10.0')
        # A notification model turn is observed by the same execution path.
        notice=self.state.enqueue('T01','D01','notice-x','synthetic notice',origin_notice='notice_x',context='normal')
        self.state.update_input(notice['id'],status='processing')
        mon.start('slack:'+notice['id'],'slack',input_id=notice['id'],origin='slack:T01:D01:0',audience='personal',generation=0)
        self.clock[0]+=601
        mon.scan()
        self.assertEqual(len(self.state.outputs()),2)

    def test_slack_checkpoint_progress_and_late_turn_does_not_advance_current(self):
        from laowu.monitoring import Monitor
        from laowu.slack_gateway import SlackGateway
        self.state.update_channel('T01','D01',thread_id='thread-current')
        self.state.update_input(self.source['id'],status='processing',turn_id='turn-current')
        mon=Monitor(self.store,lambda:self.clock[0])
        ident='slack:'+self.source['id']
        mon.start(ident,'slack',input_id=self.source['id'],origin='slack:T01:D01:0',generation=0)
        with patch('laowu.service.time.time',return_value=1100): self.slack.call('session_checkpoint',dict(summary='新摘要'))
        with patch('laowu.service.time.time',return_value=1300): self.slack.call('session_checkpoint',dict(summary='新摘要'))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT progress FROM monitor_instances WHERE id=?',(ident,)).fetchone()[0],1100)
        g=SlackGateway(self.state,clock=lambda:self.clock[0]);self.addCleanup(g.close)
        g.active[('T01','D01')]=dict(self.source,turn_id='turn-current')
        self.clock[0]=1500
        g._notification(('T01','D01'),dict(method='item/completed',params=dict(threadId='thread-current',turnId='turn-old',item=dict(id='x',type='commandExecution'))))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT progress FROM monitor_instances WHERE id=?',(ident,)).fetchone()[0],1100)
        g._notification(('T01','D01'),dict(method='item/completed',params=dict(threadId='thread-current',turnId='turn-current',item=dict(id='x',type='commandExecution'))))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT progress FROM monitor_instances WHERE id=?',(ident,)).fetchone()[0],1500)

    def test_lost_post_response_reconciles_stable_id_without_duplicate_action(self):
        from laowu.slack_gateway import SlackGateway
        from laowu.slack_client import SlackAPIError
        class API:
            posts=0; reads=0
            def info(self,c): return dict(is_im=True,user='U01')
            def post(self,*a,**k): self.posts+=1;raise SlackAPIError('transport_uncertain',uncertain=True)
            def reconcile(self,*a,**k): self.reads+=1;return 'received.1'
        api=API();g=SlackGateway(self.state,api=api,clock=lambda:self.clock[0]);self.addCleanup(g.close)
        self.state.queue_output(self.source['id'],'synthetic output')
        g.deliver_outputs();self.clock[0]+=2;g.deliver_outputs();g.deliver_outputs()
        self.assertEqual((api.posts,api.reads),(1,1))
        self.assertEqual(self.state.outputs()[0]['status'],'sent')

    def test_missing_receipt_retry_restart_budget_and_no_resend(self):
        from laowu.slack_gateway import SlackGateway
        class API:
            posts=0; reads=0
            def info(self,c): return dict(is_im=True,user='U01')
            def reconcile(self,*a,**k): self.reads+=1;return None
            def post(self,*a,**k): self.posts+=1;return {'ts':'must-not-send'}
        api=API();out=self.state.queue_output(self.source['id'],'synthetic output')
        self.state.update_output(out['id'],status='uncertain')
        for delta in (0,2,10,30,600):
            self.clock[0]+=delta
            gateway=SlackGateway(self.state,api=api,clock=lambda:self.clock[0])
            try: gateway.deliver_outputs()
            finally: gateway.close()
        self.assertEqual(api.posts,0);self.assertEqual(api.reads,4)
        self.assertEqual(self.state.outputs()[0]['status'],'blocked')

class ProtocolAndReplayTests(PhaseFixture):
    def test_fresh_stdio_advertises_tools_and_serves_budgeted_continuation(self):
        import subprocess,sys
        self.store.memory_upsert('stdio中文资料'*9000,scope='shared')
        self.svc.close()
        process=subprocess.Popen([sys.executable,'-c','import sys;from laowu.mcp import serve;serve(sys.argv[1])',str(self.root)],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            def rpc(ident,method,params):
                process.stdin.write(json.dumps(dict(jsonrpc='2.0',id=ident,method=method,params=params))+'\n');process.stdin.flush()
                return json.loads(process.stdout.readline())['result']
            listing=rpc(1,'tools/list',{})
            names={v['name'] for v in listing['tools']}
            self.assertTrue({'context_read','task_review'}<=names)
            shown=rpc(2,'tools/call',{'name':'memory_search','arguments':{}})
            self.assertFalse(shown['isError'])
            self.assertLessEqual(len(dumps(shown).encode()),16384)
            value=json.loads(shown['content'][0]['text'])
            ref=next(self.refs(value))
            page=rpc(3,'tools/call',{'name':'context_read','arguments':{'ref':ref}})
            self.assertFalse(page['isError'])
            self.assertLessEqual(len(dumps(page).encode()),16384)
            self.assertIsNotNone(json.loads(page['content'][0]['text'])['next_cursor'])
        finally:
            process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close();process.stderr.close()
        self.assertEqual(process.returncode,0)

    def test_escaped_page_and_heartbeat_change_keep_full_handoff_reference(self):
        t=self.task('\\"\n'*9000);run=self.run_task(t)
        original=task_record(self.store,t['id'])
        ref=next(self.refs(self.ctx.present('task_status',{'task_id':t['id']},original)))
        first=self.ctx.read(ref)
        with self.store.transaction() as db:
            db.execute('UPDATE runs SET updated_at=?,event_count=200 WHERE id=?',(now(),run))
        self.assertEqual(self.read_all(self.ctx,ref),original['request'])
        self.assertLessEqual(body_size(first),16384)

    def test_multiple_tools_long_phase_and_stream_events(self):
        from laowu.monitoring import Monitor,event_metadata
        t=self.task();run=self.run_task(t)
        mon=Monitor(self.store,lambda:self.clock[0]);mon.start(run,'worker',task_id=t['id'])
        mon.observe(run,**event_metadata({'type':'item.started','item':{'type':'mcp_tool_call','id':'image','tool':'imagegen'}}))
        mon.observe(run,**event_metadata({'type':'item.started','item':{'type':'command_execution','id':'shell'}}))
        mon.observe(run,**event_metadata({'type':'item.completed','item':{'type':'command_execution','id':'shell'}}))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT phase FROM monitor_instances').fetchone()[0],'long_tool')
        self.clock[0]+=601;mon.scan()
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM monitor_alerts').fetchone()[0],0)
        mon.observe(run,phase='user_wait');self.clock[0]+=601;mon.scan()
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT type FROM monitor_alerts WHERE cleared IS NULL').fetchone()[0],'user_wait')

    def test_monitor_not_blocked_by_paused_dispatch_or_advisory_io_lock(self):
        import threading
        from laowu.monitoring import Monitor
        from laowu.background import initialize_background
        initialize_background(self.store)
        t=self.task();run=self.run_task(t)
        mon=Monitor(self.store,lambda:self.clock[0]);mon.start(run,'worker',task_id=t['id'],origin='native')
        with self.store.transaction() as db:db.execute("INSERT OR REPLACE INTO background_meta VALUES('paused','1')")
        self.clock[0]+=601
        acquired=threading.Event();release=threading.Event()
        def io_lock():
            with self.store._lock():acquired.set();release.wait(5)
        thread=threading.Thread(target=io_lock);thread.start();acquired.wait(2)
        try:self.assertEqual(len(mon.scan()),1)
        finally:release.set();thread.join(2)

    def test_retry_reservation_survives_crash_and_healthy_reads_do_not_expire(self):
        from laowu.retries import ReadRetry,RetryPending
        retry=ReadRetry(self.store,lambda:self.clock[0])
        with self.store.transaction() as db:
            db.execute("INSERT INTO read_retries(id,operation,started,next_attempt,state,owner_pid,owner_identity) VALUES('crash','bridge.state',1000,1000,'reading',999999,'gone')")
        self.assertEqual(retry.call('crash','bridge.state',lambda:'idle'),'idle')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT attempts,total_attempts FROM read_retries').fetchone()[:],(0,1))
        with self.assertRaises(RetryPending):retry.call('healthy','bridge.state',lambda:(_ for _ in ()).throw(ConnectionError()))
        self.clock[0]+=2
        self.assertEqual(retry.call('healthy','bridge.state',lambda:'idle'),'idle')
        self.clock[0]+=1000
        self.assertEqual(retry.call('healthy','bridge.state',lambda:'idle'),'idle')

    def test_metrics_independent_flag_and_diagnose_has_no_review_bodies(self):
        from laowu.runtime import task_diagnostics
        from laowu.metrics import aggregate
        self.configure(mcp_budget=False,monitoring=False)
        value=self.svc.call('memory_search',{})
        self.ctx.present('memory_search',{},value)
        self.assertGreater(aggregate(self.store)['samples'][0]['mcp_bytes'],0)
        t=self.task();self.finish(t)
        self.svc.call('task_review',dict(task_id=t['id'],run_id='run_one',decision='accepted',summary='私密验收摘要',evidence=['私密证据']))
        diag=json.dumps(task_diagnostics(self.store,t['id']),ensure_ascii=False)
        self.assertNotIn('私密',diag)
        self.assertIn('accepted',diag)


if __name__=='__main__': unittest.main()
