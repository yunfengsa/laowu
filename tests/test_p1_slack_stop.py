"""Stop cleanup is local, input-scoped and recoverable without live services."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from laowu.common import now
from laowu.retries import RetryPending
from laowu.runtime import cancel_task
from laowu.slack_codex import SlackCodexError
from laowu.slack_gateway import SlackGateway
from laowu.slack_state import SlackState
from laowu.store import Store


class OfflineAPI:
    def info(self, channel):
        return {'is_private': False}

    def members(self, channel):
        return {'UOWNER', 'UBOT'}

    def status(self, *args, **kwargs):
        return {'ok': True}

    def post(self, *args, **kwargs):
        raise AssertionError('Stop regression must not send Slack messages')


class SlackStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='laowu-stop-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.state = SlackState(self.store)
        self.state.set_config({'team_id': 'TONE', 'owner_user_id': 'UOWNER',
                               'bot_user_id': 'UBOT', 'enabled': True})
        self.state.channel('TONE', 'CONE', create=True)
        self.interrupt_error = None
        self.submission_error = None
        self.stop_during_receipt = False
        self.interrupt_calls = []
        self.interrupt_task_states = []
        test = self

        class StoppingSession:
            def __init__(self, root, binding, thread_id=None, **callbacks):
                self.thread_id = thread_id or 'native-thread'

            def start(self):
                return self.thread_id

            def start_turn(self, text, ident, extra_context=''):
                self.input_id = ident
                row = test.gateway._input(ident)
                if not test.stop_during_receipt:
                    test.gateway._stop_event('TONE', 'CONE', row['reply_ts'] or row['message_ts'])
                if test.submission_error:
                    raise test.submission_error
                return 'native-turn'

            def history_receipt(self, ident, turn_id=None):
                row = test.gateway._input(self.input_id)
                test.gateway._stop_event('TONE', 'CONE', row['reply_ts'] or row['message_ts'])
                raise RetryPending('synthetic transient receipt failure')

            def interrupt(self, turn_id):
                test.interrupt_calls.append(turn_id)
                test.interrupt_task_states.append(test.task_status('sync-task'))
                if test.interrupt_error:
                    raise test.interrupt_error
                return {}

            def close(self):
                pass

        self.gateway = SlackGateway(self.state, api=OfflineAPI(),
                                    session_factory=StoppingSession, clock=lambda: 1000.0)
        self.addCleanup(self.gateway.close)
        self.source = self.state.enqueue('TONE', 'CONE', '100.000001', '合成停止请求')
        self.add_task('sync-task', self.source['id'])
        self.add_task('async-task', self.source['id'], asynchronous=True)
        self.add_task('other-task', 'another-input')

    def add_task(self, ident, input_id, *, asynchronous=False):
        with self.store.transaction() as db:
            db.execute('INSERT INTO tasks(id,session_id,friend_id,client,request,workspace,status,'
                       'request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (ident, 'synthetic-session', 'engineer', 'codex', 'synthetic work',
                        self.temp.name, 'running', 'synthetic-hash', now(), now()))
            db.execute('INSERT INTO slack_task_grants VALUES(?,?,?,?,?)',
                       (ident, 'slack:TONE:CONE:0', 'shared', None, input_id))
            if asynchronous:
                db.execute('INSERT INTO async_tasks(task_id,origin_thread,context) VALUES(?,?,?)',
                           (ident, 'slack:TONE:CONE:0', 'normal'))

    def task_status(self, ident):
        with self.store.connect() as db:
            return db.execute('SELECT status FROM tasks WHERE id=?', (ident,)).fetchone()[0]

    def execute_stop(self):
        self.state.update_input(self.source['id'], status='processing')
        self.gateway._execute(self.gateway._input(self.source['id']))
        return self.gateway._input(self.source['id'])

    def tick_without_delivery(self):
        with patch.object(self.gateway, 'deliver_outputs'):
            self.gateway.tick()

    def assert_unrelated_work_running(self):
        self.assertEqual(self.task_status('async-task'), 'running')
        self.assertEqual(self.task_status('other-task'), 'running')

    def test_interrupt_timeout_cannot_skip_or_delay_local_cancellation(self):
        self.interrupt_error = TimeoutError('PRIVATE_PROVIDER_PAYLOAD')
        result = self.execute_stop()
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.task_status('sync-task'), 'cancelled')
        self.assertEqual(self.interrupt_task_states, ['cancelled'])
        self.assertEqual(result['error'], 'user_stop_interrupt_uncertain:TimeoutError')
        output = self.state.outputs()[0]['body']
        self.assertIn('同步任务已取消', output)
        self.assertIn('模型停止尚未确认', output)
        self.assertNotIn('已停止', output)
        self.assertNotIn('PRIVATE_PROVIDER_PAYLOAD', output + result['error'])
        self.assert_unrelated_work_running()

    def test_success_reports_requested_interrupt_and_preserves_async_work(self):
        result = self.execute_stop()
        self.assertEqual(result['error'], 'user_stop_cleanup_complete')
        self.assertEqual(self.interrupt_calls, ['native-turn'])
        self.assertEqual(self.task_status('sync-task'), 'cancelled')
        self.assertIn('已请求停止', self.state.outputs()[0]['body'])
        self.assert_unrelated_work_running()

    def test_local_cleanup_failure_is_durable_and_retried_without_model_rpc(self):
        with patch('laowu.access.cancel_input_tasks',
                   side_effect=sqlite3.OperationalError('PRIVATE_DATABASE_PAYLOAD')):
            result = self.execute_stop()
        self.assertEqual(self.task_status('sync-task'), 'running')
        self.assertEqual(result['error'], 'user_stop_cleanup_pending:OperationalError')
        self.assertIn('取消尚未完成', self.state.outputs()[0]['body'])
        self.assertNotIn('PRIVATE_DATABASE_PAYLOAD', result['error'])
        self.gateway.clock = lambda: 1001.0
        self.tick_without_delivery()
        self.assertEqual(self.task_status('sync-task'), 'running')
        self.gateway.clock = lambda: 1003.0
        self.tick_without_delivery()
        self.assertEqual(self.task_status('sync-task'), 'cancelled')
        self.assertEqual(self.gateway._input(self.source['id'])['error'], 'user_stop_cleanup_complete')
        self.assertEqual(self.interrupt_calls, ['native-turn'])
        self.assertEqual(len(self.state.outputs()), 2)
        self.assert_unrelated_work_running()

    def test_both_failures_recover_after_restart_without_replaying_interrupt(self):
        self.interrupt_error = SlackCodexError('connection_lost', 'PRIVATE_REMOTE_PAYLOAD')
        with patch('laowu.access.cancel_input_tasks', side_effect=PermissionError('PRIVATE_LOCAL_PAYLOAD')):
            result = self.execute_stop()
        self.assertEqual(result['error'],
                         'user_stop_cleanup_pending_interrupt_uncertain:PermissionError:connection_lost')
        self.gateway.close()
        self.state.recover()
        self.gateway = SlackGateway(self.state, api=OfflineAPI(), clock=lambda: 1004.0,
                                    session_factory=lambda *a, **k: self.fail('Recovery must not start a model'))
        self.addCleanup(self.gateway.close)
        self.tick_without_delivery()
        result = self.gateway._input(self.source['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['error'], 'user_stop_interrupt_uncertain')
        self.assertEqual(self.task_status('sync-task'), 'cancelled')
        self.assertEqual(self.interrupt_calls, ['native-turn'])
        self.assertIn('模型停止尚未确认', self.state.outputs()[-1]['body'])
        self.assert_unrelated_work_running()

    def test_partially_cancelled_tasks_are_safe_to_retry(self):
        self.add_task('second-sync-task', self.source['id'])
        calls = []

        def cancel_first_then_fail(store, ident):
            calls.append(ident)
            if len(calls) == 2:
                raise sqlite3.OperationalError('synthetic partial cancellation')
            return cancel_task(store, ident)

        with patch('laowu.runtime.cancel_task', side_effect=cancel_first_then_fail):
            result = self.execute_stop()
        statuses = [self.task_status(ident) for ident in ('sync-task', 'second-sync-task')]
        self.assertCountEqual(statuses, ['cancelled', 'running'])
        self.assertEqual(result['error'], 'user_stop_cleanup_pending:OperationalError')
        self.gateway.clock = lambda: 1003.0
        self.tick_without_delivery()
        self.assertEqual(self.task_status('sync-task'), 'cancelled')
        self.assertEqual(self.task_status('second-sync-task'), 'cancelled')
        self.assertEqual(self.interrupt_calls, ['native-turn'])
        self.assert_unrelated_work_running()

    def test_stop_during_lost_submission_reply_still_cleans_local_tasks(self):
        self.submission_error = TimeoutError('lost submission reply')
        result = self.execute_stop()
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['error'], 'user_stop_interrupt_uncertain')
        self.assertEqual(self.task_status('sync-task'), 'cancelled')
        self.assertEqual(self.interrupt_calls, [])
        self.assert_unrelated_work_running()

    def test_receipt_retry_cannot_overwrite_a_concurrent_user_stop(self):
        self.stop_during_receipt = True
        with patch.object(self.gateway.closed, 'wait', return_value=False):
            result = self.execute_stop()
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['error'], 'user_stop_cleanup_complete')
        self.assertEqual(self.task_status('sync-task'), 'cancelled')
        self.assertEqual(self.interrupt_calls, ['native-turn'])
        self.assert_unrelated_work_running()

    def test_repeated_local_failures_do_not_requeue_a_purged_warning(self):
        with patch('laowu.access.cancel_input_tasks', side_effect=ValueError('synthetic failure')):
            self.execute_stop()
            warning = self.state.outputs()[0]
            self.state.update_output(warning['id'], status='sent', slack_ts='101.000001')
            self.gateway._settle_notifications()
            self.assertEqual(self.state.outputs()[0]['body'], '')
            self.gateway.clock = lambda: 1003.0
            self.tick_without_delivery()
        self.assertEqual(len(self.state.outputs()), 1)
        self.assertEqual(self.gateway._input(self.source['id'])['error'], 'user_stop_cleanup_pending:ValueError')
        self.gateway.clock = lambda: 1006.0
        self.tick_without_delivery()
        self.assertEqual(self.task_status('sync-task'), 'cancelled')
        self.assertEqual(self.interrupt_calls, ['native-turn'])

    def test_marker_write_failure_rolls_back_receipt_and_does_not_block_scheduling(self):
        self.interrupt_error = TimeoutError('synthetic interrupt timeout')
        # Fail the actual SQLite marker update after its receipt INSERT. The
        # trigger does not match the ingress marker "user_stopped".
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_stop_marker BEFORE UPDATE OF error ON slack_inbox "
                       "WHEN NEW.error GLOB 'user_stop_*' BEGIN "
                       "SELECT RAISE(ABORT, 'PRIVATE_MARKER_FAILURE'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute_stop()
        self.assertEqual(self.gateway._input(self.source['id'])['error'], 'user_stopped')
        self.assertEqual(self.task_status('sync-task'), 'cancelled')
        self.assertEqual(self.state.outputs(), [])
        self.assertEqual(self.gateway.active, {})
        next_input = self.state.enqueue('TONE', 'CONE', '102.000001', 'unrelated pending input')
        self.gateway.clock = lambda: 1004.0
        with self.assertLogs('laowu.slack_gateway', level='WARNING') as logs:
            with patch.object(self.gateway, 'deliver_outputs') as delivery:
                with patch.object(self.gateway, '_execute') as execute:
                    self.gateway.tick()
                    self.gateway.running[('TONE', 'CONE')].result(timeout=3)
                    self.assertEqual(execute.call_args.args[0]['id'], next_input['id'])
                delivery.assert_called_once()
        self.assertNotIn('PRIVATE_MARKER_FAILURE', '\n'.join(logs.output))
        self.assertEqual(self.state.outputs(), [])
        with self.store.transaction() as db:
            db.execute('DROP TRIGGER fail_stop_marker')
        self.gateway.clock = lambda: 1007.0
        self.tick_without_delivery()
        self.assertEqual(self.gateway._input(self.source['id'])['error'], 'user_stop_interrupt_uncertain')
        self.assertEqual(len(self.state.outputs()), 1)
        self.assertEqual(self.interrupt_calls, ['native-turn'])

    def test_restart_reuses_purged_receipt_and_continues_with_other_inputs(self):
        self.interrupt_error = TimeoutError('synthetic interrupt timeout')
        self.execute_stop()
        # Recreate the pre-fix crash window: receipt committed, stop marker not
        # committed, then delivery cleared the receipt's body before recovery.
        self.state.update_input(self.source['id'], error='user_stopped')
        receipt = self.state.outputs()[0]
        self.state.update_output(receipt['id'], status='sent', slack_ts='101.000001')
        self.gateway._settle_notifications()
        self.assertEqual(self.state.outputs()[0]['body'], '')
        self.gateway.close()
        self.state.recover()
        self.gateway = SlackGateway(self.state, api=OfflineAPI(), clock=lambda: 1004.0,
                                    session_factory=lambda *a, **k: self.fail('Recovery must not start a model'))
        self.addCleanup(self.gateway.close)
        next_input = self.state.enqueue('TONE', 'CONE', '102.000001', 'unrelated pending input')
        with patch.object(self.gateway, 'deliver_outputs') as delivery:
            with patch.object(self.gateway, '_execute') as execute:
                self.gateway.tick()
                self.gateway.running[('TONE', 'CONE')].result(timeout=3)
                self.assertEqual(execute.call_args.args[0]['id'], next_input['id'])
            delivery.assert_called_once()
        result = self.gateway._input(self.source['id'])
        self.assertEqual(result['error'], 'user_stop_interrupt_uncertain')
        outputs = self.state.outputs()
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]['id'], receipt['id'])
        self.assertEqual(outputs[0]['status'], 'sent')
        self.assertEqual(outputs[0]['body'], '')
        self.assertEqual(self.interrupt_calls, ['native-turn'])

    def test_one_failed_stop_recovery_does_not_skip_another_stop_record(self):
        self.state.update_input(self.source['id'], status='cancelled', error='user_stopped')
        second = self.state.enqueue('TONE', 'CONE', '101.000001', 'another stopped input')
        self.state.update_input(second['id'], status='cancelled', error='user_stopped')
        self.add_task('second-sync-task', second['id'])
        finish = self.gateway._finish_stop

        def fail_first(record, **kwargs):
            if record['id'] == self.source['id']:
                raise sqlite3.OperationalError('PRIVATE_STOP_FAILURE')
            return finish(record, **kwargs)

        with self.assertLogs('laowu.slack_gateway', level='WARNING'):
            with patch.object(self.gateway, '_finish_stop', side_effect=fail_first):
                self.tick_without_delivery()
        self.assertEqual(self.gateway._input(self.source['id'])['error'], 'user_stopped')
        self.assertEqual(self.gateway._input(second['id'])['error'], 'user_stop_interrupt_uncertain')
        self.assertEqual(self.task_status('second-sync-task'), 'cancelled')
        self.assertEqual(self.interrupt_calls, [])


if __name__ == '__main__':
    unittest.main()
