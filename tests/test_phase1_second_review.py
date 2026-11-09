"""Second review: real legacy layouts and observation/commit failure boundaries.

Set LAOWU_SCHEMA2_FIXTURE to an anonymous SQLite fixture to exercise its exact
layout via SQLite's read-only backup API. The checked-in schema is the portable
schema-only equivalent; all writes and injected failures stay in temporary roots.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from test_phase1 import PhaseFixture
from laowu import migrations
from laowu.features import DEFAULTS
from laowu.monitoring import Monitor
from laowu.retries import ReadRetry, RetryPending
from laowu.runtime import Manager, checkpoint, task_record
from laowu.service import Service
from laowu.store import Store


class Schema2LayoutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='phase1-second-layout-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / '.laowu/state.sqlite3'
        self.path.parent.mkdir()
        fixture = os.environ.get('LAOWU_SCHEMA2_FIXTURE')
        with contextlib.closing(sqlite3.connect(self.path)) as target:
            if fixture:
                source_path = Path(fixture)
                original = hashlib.sha256(source_path.read_bytes()).hexdigest()
                self.addCleanup(lambda: self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), original))
                with contextlib.closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
                    source.backup(target)
            else:
                target.executescript((Path(__file__).parent / 'fixtures/phase1_schema2.sql').read_text())
        with self.raw() as db:
            self.assertEqual([r[1] for r in db.execute('PRAGMA table_info(read_retries)')],
                             ['id','operation','started','attempts','next_attempt','state','category'])
            self.assertEqual([r[1] for r in db.execute('PRAGMA table_info(execution_routes)')], ['task_id','context'])
            db.execute("INSERT INTO tasks(id,session_id,friend_id,client,request,workspace,status,checkpoint,result,request_hash,created_at,updated_at) VALUES('legacy','old-session','engineer','codex','synthetic',?,'blocked','{\"summary\":\"old\"}','{\"result\":\"retained\"}','hash','2020-01-02T00:00:00+00:00','2020-01-03T00:00:00+00:00')", (str(self.root),))
            db.execute("INSERT INTO execution_routes VALUES('legacy','root')")
            db.execute("INSERT INTO execution_routes VALUES('no-task','normal')")
            db.execute("INSERT INTO read_retries VALUES('old-read','bridge.state',100,2,200,'waiting','network')")
            db.execute("INSERT INTO notifications(id,event_key,digest,context,topic,message,status,created_at,updated_at) VALUES('unknown','unknown','digest','normal','test','synthetic','uncertain','old','old')")
            db.execute("INSERT INTO memory_records VALUES('shared-record','synthetic fact','fact','shared','test','explicit','laowu',1,'old','old','{}')")
        schedule = patch.object(Manager, '_schedule', return_value=None)
        schedule.start()
        self.addCleanup(schedule.stop)

    @contextlib.contextmanager
    def raw(self, path=None):
        with contextlib.closing(sqlite3.connect(path or self.path, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            yield db

    def dump(self, path=None):
        with self.raw(path) as db:
            return list(db.iterdump())

    def backups(self):
        return sorted((self.root / '.laowu/backups').glob('before_migration_*.sqlite3'))

    def assert_layout(self, db):
        self.assertFalse(migrations.needs_upgrade(db))
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        for table in ('read_sources','monitor_tools','metric_timings'):
            self.assertIsNotNone(db.execute('SELECT name FROM sqlite_master WHERE name=?', (table,)).fetchone())
        self.assertEqual(db.execute("SELECT value FROM memory_meta WHERE key=?", (migrations.LAYOUT_KEY,)).fetchone()[0], str(migrations.LAYOUT_REVISION))

    def test_old_layout_starts_tasks_resumes_recovers_and_retries(self):
        service = Service(self.root)
        self.addCleanup(service.close)
        self.assertEqual({k: False for k in DEFAULTS}, DEFAULTS)
        service.registry.ensure(dict(id='engineer',name='test',description='test',instructions='synthetic',skills=[],tools=['checkpoint'],can_write=False))
        task = service.call('task_start', dict(friend_id='engineer', request='synthetic new task'))
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['acceptance']['state'], 'legacy')
        self.assertEqual(service.call('task_resume', dict(task_id='legacy'))['status'], 'queued')
        with service.store.transaction() as db:
            db.execute("UPDATE tasks SET status='interrupted',error='owner_lost' WHERE id='legacy'")
            db.execute("INSERT INTO async_tasks(task_id,context) VALUES('legacy','root')")
        from laowu.daemon import recover_interrupted
        recover_interrupted(service.store)
        self.assertEqual(task_record(service.store, 'legacy')['status'], 'queued')
        (self.root / 'config').mkdir(exist_ok=True)
        (self.root / 'config/features.json').write_text(json.dumps(DEFAULTS | {'read_retries': True}))
        retry = ReadRetry(service.store, lambda: 1000)
        self.assertEqual(retry.call('new-read','bridge.state',lambda:'ok'), 'ok')
        with self.assertRaises(RetryPending):
            retry.call('failed-read','bridge.state',lambda: (_ for _ in ()).throw(ConnectionError()))
        with self.raw() as db:
            self.assert_layout(db)
            self.assertEqual(db.execute("SELECT status FROM notifications WHERE id='unknown'").fetchone()[0], 'uncertain')

    def test_backed_up_repair_preserves_data_and_second_start_is_noop(self):
        before = self.dump()
        Store(self.root)
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.dump(backups[0]), before)
        with self.raw() as db:
            self.assert_layout(db)
            self.assertEqual(db.execute("SELECT queued_at FROM execution_routes WHERE task_id='legacy'").fetchone()[0], '2020-01-02T00:00:00+00:00')
            self.assertIsNone(db.execute("SELECT queued_at FROM execution_routes WHERE task_id='no-task'").fetchone()[0])
            self.assertEqual(tuple(db.execute("SELECT attempts,total_attempts,resumptions,owner_pid,owner_identity FROM read_retries WHERE id='old-read'").fetchone()), (2,2,0,None,None))
            self.assertEqual(db.execute('SELECT count(*) FROM task_acceptance').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT content FROM memory_records WHERE id='shared-record'").fetchone()[0], 'synthetic fact')
            self.assertEqual(db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone()[0], 'schema2-fixture')
        repaired = self.dump()
        Store(self.root)
        with self.raw() as db:
            migrations.upgrade(db)
            self.assertEqual(db.total_changes, 0)
        self.assertEqual(self.dump(), repaired)
        self.assertEqual(self.backups(), backups)

    def test_failed_repair_rolls_back_columns_tables_epoch_and_revision(self):
        with self.raw() as db:
            db.execute("DELETE FROM memory_meta WHERE key='context_epoch'")
        before = self.dump()
        repair = migrations._repair
        def fail(db, **kwargs):
            repair(db, **kwargs)
            raise RuntimeError('synthetic migration failure')
        with patch.object(migrations, '_repair', fail), self.assertRaises(RuntimeError):
            Store(self.root)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.dump(self.backups()[0]), before)
        Store(self.root)
        with self.raw() as db:
            self.assert_layout(db)
            self.assertTrue(db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone()[0])

    def test_startup_backup_includes_committed_wal(self):
        with self.raw() as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE tasks SET checkpoint='{\"summary\":\"committed WAL\"}' WHERE id='legacy'")
            self.assertGreater(Path(str(self.path)+'-wal').stat().st_size, 0)
            Store(self.root)
            with self.raw(self.backups()[0]) as backup:
                self.assertEqual(backup.execute("SELECT checkpoint FROM tasks WHERE id='legacy'").fetchone()[0], '{"summary":"committed WAL"}')
                self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_epoch_only_damage_and_marked_layout_damage_are_backed_up(self):
        Store(self.root)
        for damage in ("DELETE FROM memory_meta WHERE key='context_epoch'",
                       "UPDATE memory_meta SET value='  ' WHERE key='context_epoch'",
                       'DROP TABLE monitor_tools'):
            with self.subTest(damage=damage):
                with self.raw() as db:
                    db.execute(damage)
                before = self.dump()
                backups = set(self.backups())
                Store(self.root)
                new_backups = set(self.backups()) - backups
                self.assertEqual(len(new_backups), 1)
                self.assertEqual(self.dump(new_backups.pop()), before)
                with self.raw() as db:
                    self.assert_layout(db)
                Store(self.root)
                self.assertEqual(len(self.backups()), len(backups) + 1)

    def test_future_schema_extension_and_layout_are_rejected_before_writes(self):
        for table, key, value in (('memory_meta','schema_version','3'),
                                  ('memory_meta',migrations.LAYOUT_KEY,str(migrations.LAYOUT_REVISION + 1)),
                                  ('runtime_meta','schema_version','2'),
                                  ('background_meta','schema_version','2'),
                                  ('slack_meta','schema_version','2')):
            with self.subTest(table=table, key=key):
                with self.raw() as db:
                    old = db.execute('SELECT value FROM '+table+' WHERE key=?', (key,)).fetchone()
                    db.execute('INSERT OR REPLACE INTO '+table+' VALUES(?,?)', (key,value))
                before = self.dump()
                with self.assertRaises(ValueError):
                    Store(self.root)
                with self.raw() as db, self.assertRaises(ValueError):
                    migrations.upgrade(db)
                self.assertEqual(self.dump(), before)
                self.assertEqual(self.backups(), [])
                with self.raw() as db:
                    db.execute('DELETE FROM '+table+' WHERE key=?', (key,))
                    if old:
                        db.execute('INSERT INTO '+table+' VALUES(?,?)', (key,old[0]))

    def archive(self, version):
        with self.raw() as db:
            db.execute("UPDATE memory_meta SET value=? WHERE key='schema_version'", (str(version),))
        blobs = {'state.sqlite3': self.path.read_bytes(), 'forgotten.json': b'{"ids":[],"pending":false}'}
        manifest = dict(format='laowu-backup', version=1, schema_version=version,
                        source_root=str(self.root), files={k:hashlib.sha256(v).hexdigest() for k,v in blobs.items()})
        path = self.root / ('old-'+str(version)+'.zip')
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('manifest.json',json.dumps(manifest))
            for name, blob in blobs.items():
                z.writestr(name,blob)
        return path

    def test_schema1_and_old_schema2_snapshots_restore_and_future_layout_does_not(self):
        for version in (1,2):
            with self.subTest(version=version):
                archive = self.archive(version)
                destination = Store(self.root / ('restore-'+str(version)))
                self.assertTrue(destination.restore(archive)['restored'])
                with contextlib.closing(destination.connect()) as db:
                    self.assert_layout(db)
                    self.assertEqual(db.execute('SELECT count(*) FROM task_acceptance').fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT checkpoint FROM tasks WHERE id='legacy'").fetchone()[0], '{"summary":"old"}')
                    self.assertEqual(db.execute("SELECT status FROM notifications WHERE id='unknown'").fetchone()[0], 'uncertain')
        with self.raw() as db:
            db.execute('INSERT INTO memory_meta VALUES(?,?)', (migrations.LAYOUT_KEY,str(migrations.LAYOUT_REVISION + 1)))
        archive = self.archive(2)
        before = self.dump(destination.path)
        with self.assertRaises(ValueError):
            destination.restore(archive)
        self.assertEqual(self.dump(destination.path), before)


class TaskCheckpointIsolationTests(PhaseFixture):
    def setUp(self):
        super().setUp()
        self.task_row = self.task()
        self.run_task(self.task_row)
        Monitor(self.store,lambda:1000).start('run_one','worker',task_id=self.task_row['id'])

    def saved(self):
        return task_record(self.store,self.task_row['id'])['checkpoint']

    def test_runtime_observer_failure_sees_committed_checkpoint(self):
        observed = []
        def fail(store, task_id, content):
            observed.append(self.saved())
            raise RuntimeError('synthetic observer failure')
        with patch('laowu.monitoring.checkpoint_progress',side_effect=fail) as observation:
            checkpoint(self.store,self.task_row['id'],'committed',artifacts=['/tmp/synthetic-artifact'])
        observation.assert_called_once()
        self.assertEqual(observed[0]['summary'], 'committed')
        self.assertEqual(observed[0]['artifacts'], ['/tmp/synthetic-artifact'])
        self.assertEqual(self.saved()['summary'],'committed')

    def test_sql_rollback_cannot_undo_task_checkpoint(self):
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_observation BEFORE UPDATE ON monitor_instances BEGIN SELECT RAISE(ROLLBACK,'synthetic observation rollback'); END")
        result = checkpoint(self.store,self.task_row['id'],'survives SQL rollback',artifacts=['/tmp/synthetic-artifact'])
        self.assertEqual(self.saved(),result)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM monitor_progress').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT progress FROM monitor_instances').fetchone()[0],1000)


class SlackCheckpointIsolationTests(PhaseFixture):
    def setUp(self):
        super().setUp()
        from laowu.slack_state import SlackState
        self.state = SlackState(self.store)
        self.state.set_config(dict(team_id='T1',owner_user_id='U1',bot_user_id='UB1',enabled=True))
        self.state.update_channel('T1','D1',personal=1)
        self.source = self.state.enqueue('T1','D1','1.1','synthetic')
        self.state.update_input(self.source['id'],status='processing')
        self.slack = Service(self.root,slack_token_file=self.state.issue_binding('T1','D1'))
        self.addCleanup(self.slack.close)
        # Reentrant test mutations share the service's advisory-lock instance.
        self.state.store = self.slack.store
        self.mon = Monitor(self.store,lambda:1000)
        self.observe(self.source)

    def observe(self, source):
        self.mon.start('slack:'+source['id'],'slack',input_id=source['id'],origin='slack:T1:D1:0',generation=0)

    def summary(self):
        return self.state.channel('T1','D1')['summary']

    def test_runtime_observer_failure_sees_committed_slack_summary(self):
        observed = []
        def fail(store, access, input_id):
            observed.append((input_id,self.summary()))
            raise RuntimeError('synthetic observer failure')
        with patch('laowu.monitoring.slack_checkpoint_progress',side_effect=fail) as observation:
            self.assertTrue(self.slack.call('session_checkpoint',dict(summary='committed'))['saved'])
        observation.assert_called_once()
        self.assertEqual(observed, [(self.source['id'],'committed')])
        self.assertEqual(self.summary(),'committed')

    def test_sql_rollback_cannot_undo_slack_summary(self):
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_observation BEFORE UPDATE ON monitor_instances BEGIN SELECT RAISE(ROLLBACK,'synthetic observation rollback'); END")
        self.assertTrue(self.slack.call('session_checkpoint',dict(summary='survives SQL rollback'))['saved'])
        self.assertEqual(self.summary(),'survives SQL rollback')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT progress FROM monitor_instances').fetchone()[0],1000)

    def test_progress_remains_bound_to_original_input_and_generation(self):
        from laowu.monitoring import slack_checkpoint_progress
        observed = []
        with patch('laowu.monitoring.time.time',return_value=1100):
            self.slack.call('session_checkpoint',dict(summary='first'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT progress FROM monitor_instances').fetchone()[0],1100)
        def switch_input(store, access, input_id):
            observed.append(self.summary())
            self.state.update_input(self.source['id'],status='completed')
            self.next_source = self.state.enqueue('T1','D1','2.1','synthetic next')
            self.state.update_input(self.next_source['id'],status='processing')
            self.observe(self.next_source)
            slack_checkpoint_progress(store,access,input_id)
        with patch('laowu.monitoring.slack_checkpoint_progress',side_effect=switch_input):
            self.slack.call('session_checkpoint',dict(summary='second'))
        with self.store.connect() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT progress FROM monitor_instances ORDER BY rowid')],[1100,1000])
        def revoke(store, access, input_id):
            observed.append(self.summary())
            self.state.update_channel('T1','D1',generation=1)
            slack_checkpoint_progress(store,access,input_id)
        with patch('laowu.monitoring.slack_checkpoint_progress',side_effect=revoke):
            self.slack.call('session_checkpoint',dict(summary='third'))
        self.assertEqual(self.summary(),'third')
        self.assertEqual(observed, ['second','third'])
        with self.store.connect() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT progress FROM monitor_instances ORDER BY rowid')],[1100,1000])
        with self.assertRaises(PermissionError):
            self.slack.call('session_checkpoint',dict(summary='revoked'))
        self.assertEqual(self.summary(),'third')


class MCPObservationTests(PhaseFixture):
    def wire_call(self, name, arguments):
        from laowu import mcp
        replied = threading.Event()
        request = json.dumps(dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name=name,arguments=arguments))).encode()+b'\n'
        class Input:
            buffer = None
            def __init__(self):
                self.buffer = self
                self.first = True
            def readline(self, maximum):
                if self.first:
                    self.first = False
                    return request
                if not replied.wait(5):
                    raise AssertionError('stdio response timed out')
                return b''
        class Output(io.StringIO):
            def flush(self):
                replied.set()
        output = Output()
        with patch.object(mcp,'Service',return_value=self.svc), patch.object(mcp.signal,'signal'), \
             patch.object(mcp.sys,'stdin',Input()), patch.object(mcp.sys,'stdout',output):
            mcp.serve(self.root)
        return json.loads(output.getvalue())

    def test_metric_failure_preserves_original_tool_error_wire_semantics(self):
        for error in (RuntimeError('synthetic metrics failure'),sqlite3.OperationalError('synthetic SQL failure')):
            with self.subTest(error=type(error).__name__), patch('laowu.metrics.sample',side_effect=error) as observation:
                response = self.wire_call('task_status',dict(task_id='missing'))
            self.assertNotIn('error',response)
            self.assertTrue(response['result']['isError'])
            self.assertEqual(json.loads(response['result']['content'][0]['text'])['error'],'任务不存在')
            observation.assert_called_once()

    def test_metrics_bytes_are_recorded_without_budgeting(self):
        self.configure(mcp_budget=False,metrics=True)
        response = self.wire_call('memory_search',{})
        self.assertFalse(response['result']['isError'])
        self.assertEqual(json.loads(response['result']['content'][0]['text']),[])
        encoded_size = len(json.dumps(response['result'],ensure_ascii=False,separators=(',',':')).encode())
        with self.store.connect() as db:
            rows = db.execute("SELECT mcp_bytes,previews,pages FROM metric_samples WHERE kind='mcp'").fetchall()
        self.assertEqual([tuple(r) for r in rows],[(encoded_size,0,0)])


if __name__ == '__main__':
    unittest.main()
