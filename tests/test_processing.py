"""Synthetic forget/pause state machine; no live services, models or Slack."""
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from laowu.background import Outbox, daemon_status
from laowu.processing import ProcessingPaused, set_operator_pause, status
from laowu.runtime import Manager
from laowu.service import Service
from laowu.slack_admin import execute_slack
from laowu.slack_gateway import SlackGateway
from laowu.slack_state import SlackState
from laowu.store import Store
from tests.test_slack_gateway import FakeAPI, FakeSession


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='laowu-processing-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.state = SlackState(self.store)
        self.state.set_config(dict(team_id='TONE', owner_user_id='UOWNER', bot_user_id='UBOT', enabled=True))
        self.api = FakeAPI()
        FakeSession.created = []
        FakeSession.gate = None
        self.gateway = SlackGateway(self.state, api=self.api, session_factory=FakeSession)
        self.addCleanup(self.gateway.close)
        self.fact = self.store.memory_upsert('synthetic forgotten fact', scope='shared')

    def enqueue(self, text='new request', ts='2.01'):
        return self.state.enqueue('TONE', 'DONE', ts, text)

    def drain(self):
        for _ in range(300):
            self.gateway.tick()
            if not self.gateway.running and not self.state.inbox('pending'):
                self.gateway.deliver_outputs()
                return
            time.sleep(.005)
        self.fail('synthetic input did not complete')

    def service(self):
        scheduling = patch.object(Manager, '_schedule', return_value=None)
        scheduling.start()
        self.addCleanup(scheduling.stop)
        service = Service(self.store.root)
        self.addCleanup(service.close)
        return service

    def test_success_forget_accepts_new_dm_without_resume_or_old_thread(self):
        self.enqueue('old request', '1.01')
        self.drain()
        old = FakeSession.created[0]
        before = len(self.api.posts)
        self.store.memory_forget(self.fact['id'])
        self.assertFalse(status(self.store)['paused'])
        self.assertEqual(self.state.inbox(), [])
        self.assertEqual(self.state.channel('TONE', 'DONE')['error'], 'forgotten_context')
        self.enqueue()
        self.drain()
        self.assertTrue(old.closed)
        self.assertEqual(len(FakeSession.created), 2)
        self.assertEqual(FakeSession.created[-1].starts[0][0], 'new request')
        self.assertEqual(len(self.api.posts), before + 1)
        self.assertTrue(self.state.channel('TONE', 'DONE')['eligible'])
        self.assertEqual(self.store.memory_search(), [])

    def test_service_quiesce_uses_ledger_then_resumes_new_inputs(self):
        service = self.service()
        original = service._quiesce

        def check():
            self.assertEqual(status(self.store)['reasons'], ['forget_pending'])
            original()

        with patch.object(service, '_quiesce', side_effect=check):
            service.call('memory_forget', {'memory_id': self.fact['id']})
        self.assertFalse(status(self.store)['paused'])
        self.enqueue()
        self.drain()
        self.assertEqual(len(self.api.posts), 1)

    def test_explicit_pause_survives_forget_and_restart(self):
        set_operator_pause(self.store, True)
        self.store.memory_forget(self.fact['id'])
        reopened = Store(self.store.root)
        self.assertEqual(status(reopened)['reasons'], ['manual'])
        self.enqueue()
        self.gateway.tick()
        self.assertEqual(FakeSession.created, [])
        set_operator_pause(reopened, False)
        self.drain()
        self.assertEqual(len(self.api.posts), 1)

    def test_manual_pause_during_quiesce_is_not_overwritten(self):
        def quiesce():
            set_operator_pause(self.store, True)
            with self.assertRaisesRegex(RuntimeError, '遗忘尚未完成'):
                set_operator_pause(self.store, False)
        self.store.memory_forget(self.fact['id'], quiesce=quiesce)
        self.assertEqual(status(self.store)['reasons'], ['manual'])

    def test_legacy_pause_is_not_guessed_from_completed_ledger(self):
        self.store.memory_forget(self.fact['id'])
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO background_meta VALUES('paused','1')")
        self.state.runtime(status='connected')
        self.enqueue()
        self.gateway.tick()
        result = self.state.status()
        self.assertEqual(result['runtime']['status'], 'connected')
        self.assertEqual(result['processing']['reasons'], ['legacy_unknown'])
        self.assertIn('daemon resume', result['processing']['recovery'][0])
        self.assertEqual(FakeSession.created, [])
        self.assertEqual(daemon_status(self.store)['reasons'], ['legacy_unknown'])
        doctor = execute_slack(self.store, SimpleNamespace(action='doctor'))
        self.assertEqual(doctor['processing']['reasons'], ['legacy_unknown'])
        self.assertEqual(doctor['background']['reasons'], ['legacy_unknown'])

    def test_cleanup_failure_blocks_model_and_content_until_restart_repairs(self):
        record = self.enqueue('old pending')
        self.state.queue_output(record['id'], 'old output', key='old')
        with patch.object(self.store, '_clear_runtime_files', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.store.memory_forget(self.fact['id'])
        self.assertEqual(status(self.store)['reasons'], ['forget_pending'])
        self.gateway.tick()
        self.gateway.deliver_outputs()
        self.assertEqual(FakeSession.created, [])
        self.assertEqual(self.api.posts, [])
        with self.assertRaises(RuntimeError):
            set_operator_pause(self.store, False)
        repaired = Store(self.store.root)
        self.assertFalse(status(repaired)['paused'])
        self.assertEqual(self.state.inbox(), [])
        self.assertEqual(self.state.outputs(), [])
        self.enqueue(ts='3.01')
        self.drain()
        self.assertEqual(len(self.api.posts), 1)

    def test_committed_cleanup_finalization_crash_preserves_new_input_on_repair(self):
        original = self.store._write_ledger
        def write(ids, *, pending, **kwargs):
            if not pending:
                raise OSError('synthetic finalization failure')
            return original(ids, pending=pending, **kwargs)
        with patch.object(self.store, '_write_ledger', side_effect=write):
            with self.assertRaises(OSError):
                self.store.memory_forget(self.fact['id'])
        self.enqueue()
        self.gateway.tick()
        self.assertEqual(FakeSession.created, [])
        repaired = Store(self.store.root)
        self.assertFalse(status(repaired)['paused'])
        self.assertEqual(self.state.inbox()[0]['body'], 'new request')
        self.drain()

    def test_quiesce_failure_persists_barrier_and_invalid_id_does_not_pause(self):
        service = self.service()
        with self.assertRaises(ValueError):
            service.call('memory_forget', {'memory_id': '../bad'})
        self.assertFalse(status(self.store)['paused'])
        with patch.object(service, '_quiesce', side_effect=RuntimeError('synthetic live process')):
            with self.assertRaises(RuntimeError):
                service.call('memory_forget', {'memory_id': self.fact['id']})
        self.assertTrue(status(self.store)['maintenance'])
        repaired = Store(self.store.root)
        self.assertFalse(status(repaired)['paused'])
        self.assertEqual(repaired.memory_search(), [])

    def test_forget_between_session_setup_and_submit_cannot_send_old_body(self):
        original = FakeSession.start_turn
        def interleave(session, *args, **kwargs):
            self.store.memory_forget(self.fact['id'])
            return original(session, *args, **kwargs)
        self.enqueue('old cached input')
        with patch.object(FakeSession, 'start_turn', new=interleave):
            self.drain()
        self.assertEqual(sum(len(s.starts) for s in FakeSession.created), 0)
        self.assertEqual(self.api.posts, [])
        self.enqueue('fresh input', '3.01')
        self.drain()
        self.assertEqual(FakeSession.created[-1].starts[0][0], 'fresh input')

    def test_pause_between_claim_and_execute_requeues_without_model(self):
        record = self.enqueue()
        self.state.update_input(record['id'], status='processing')
        set_operator_pause(self.store, True)
        self.gateway._execute(record)
        self.assertEqual(FakeSession.created, [])
        self.assertEqual(self.state.inbox()[0]['status'], 'pending')

    def test_forget_during_output_qualification_does_not_send_stale_body(self):
        record = self.enqueue()
        self.state.queue_output(record['id'], 'old cached output')
        original = self.api.info
        def info(channel):
            self.store.memory_forget(self.fact['id'])
            return original(channel)
        with patch.object(self.api, 'info', side_effect=info):
            self.gateway.deliver_outputs()
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.state.outputs(), [])

    def test_restore_hold_survives_forget_and_requires_separate_slack_enable(self):
        snapshot = Path(self.temp.name) / 'snapshot.zip'
        shutil.copyfile(self.store.backup()['path'], snapshot)
        self.store.restore(snapshot)
        self.assertEqual(status(self.store)['reasons'], ['restore'])
        self.store.memory_forget(self.fact['id'])
        self.assertEqual(status(Store(self.store.root))['reasons'], ['restore'])
        set_operator_pause(self.store, False)
        self.assertFalse(self.state.config()['enabled'])

    def test_corrupt_ledger_fails_closed_without_leaking_contents_in_status(self):
        self.store.ledger_path.write_text('synthetic invalid ledger')
        self.assertEqual(status(self.store)['reasons'], ['forget_ledger_invalid'])
        self.enqueue()
        self.gateway.tick()
        self.assertEqual(FakeSession.created, [])

    def test_stale_native_manager_and_uncollected_results_stay_revoked(self):
        service = self.service()
        service.registry.ensure(dict(id='engineer', name='test', description='test', instructions='test', tools=[]), write_files=False)
        task = service.manager.submit('engineer', 'old work', detached=True)
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='completed',result='{}' WHERE id=?", (task['id'],))
        self.store.memory_forget(self.fact['id'])
        Outbox(self.store).collect_tasks()
        self.assertEqual(Outbox(self.store).list(), [])
        with self.assertRaisesRegex(ValueError, '上下文'):
            service.manager.submit('engineer', 'stale cached task')
        with self.assertRaisesRegex(ValueError, '上下文'):
            service.call('session_checkpoint', {'summary': 'stale cached summary'})
        with self.assertRaisesRegex(ValueError, '上下文'):
            service.call('memory_upsert', {'content': 'stale cached memory'})
        service.call('bootstrap', {})
        new = service.manager.submit('engineer', 'fresh task')
        self.assertNotEqual(task['id'], new['id'])

    def test_failure_barrier_blocks_sync_and_background_claims(self):
        service = self.service()
        service.registry.ensure(dict(id='engineer', name='test', description='test', instructions='test', tools=[]), write_files=False)
        task = service.manager.submit('engineer', 'old queued')
        background = Manager(self.store, background=True)
        self.addCleanup(background.close)
        with patch.object(self.store, '_clear_runtime_files', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.store.memory_forget(self.fact['id'])
        self.assertIsNone(service.manager._claim())
        self.assertIsNone(background._claim())
        with self.assertRaises(ProcessingPaused):
            service.manager.submit('engineer', 'new input while incomplete')

    def test_restart_waits_for_inflight_forget_and_keeps_manual_pause(self):
        entered, proceed, reopening, reopened = (threading.Event() for _ in range(4))
        failures = []
        def quiesce():
            entered.set()
            if not proceed.wait(5):
                raise TimeoutError('test coordination failed')
        def forget():
            try:
                self.store.memory_forget(self.fact['id'], quiesce=quiesce)
            except BaseException as exc:
                failures.append(exc)
        def restart():
            reopening.set()
            try:
                Store(self.store.root)
                reopened.set()
            except BaseException as exc:
                failures.append(exc)
        first, second = threading.Thread(target=forget), threading.Thread(target=restart)
        first.start()
        try:
            self.assertTrue(entered.wait(5))
            second.start()
            self.assertTrue(reopening.wait(5))
            self.assertFalse(reopened.wait(.05))
            # The independent operation lock does not block operator writes.
            set_operator_pause(self.store, True)
        finally:
            proceed.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
        self.assertEqual(failures, [])
        self.assertTrue(reopened.is_set())
        self.assertEqual(status(self.store)['reasons'], ['manual'])

    def test_restore_waits_for_forget_and_old_snapshot_cannot_revive_content(self):
        snapshot = Path(self.temp.name) / 'snapshot.zip'
        shutil.copyfile(self.store.backup()['path'], snapshot)
        entered, proceed, restoring = (threading.Event() for _ in range(3))
        failures = []
        def quiesce():
            entered.set()
            if not proceed.wait(5):
                raise TimeoutError('test coordination failed')
        def forget():
            try:
                self.store.memory_forget(self.fact['id'], quiesce=quiesce)
            except BaseException as exc:
                failures.append(exc)
        def restore():
            restoring.set()
            try:
                self.store.restore(snapshot)
            except BaseException as exc:
                failures.append(exc)
        first, second = threading.Thread(target=forget), threading.Thread(target=restore)
        first.start()
        try:
            self.assertTrue(entered.wait(5))
            second.start()
            self.assertTrue(restoring.wait(5))
        finally:
            proceed.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.store.memory_search(), [])
        self.assertEqual(status(self.store)['reasons'], ['restore'])

    def test_cli_pause_resume_report_provenance_and_forget_preserves_manual(self):
        from laowu.cli import execute, parser
        paused = execute(parser().parse_args(['daemon', 'pause']), self.store.root)
        self.assertEqual(paused['reasons'], ['manual'])
        execute(parser().parse_args(['memory', 'forget', self.fact['id']]), self.store.root)
        self.assertEqual(status(self.store)['reasons'], ['manual'])
        resumed = execute(parser().parse_args(['daemon', 'resume']), self.store.root)
        self.assertFalse(resumed['paused'])


if __name__ == '__main__':
    unittest.main()
