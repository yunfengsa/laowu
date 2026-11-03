"""Build the real Slack SDK/Bolt and exercise gateway startup without I/O."""
import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from laowu.slack_client import SlackAPI
from laowu.slack_gateway import SlackGateway
from laowu.slack_state import SlackState
from laowu.store import Store


class SocketModeRunTests(unittest.TestCase):
    def patched(self, target, attribute=None, **kwargs):
        patcher = patch(target, **kwargs) if attribute is None else patch.object(target, attribute, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def setUp(self):
        try:
            from slack_bolt import App
            from slack_sdk import WebClient
            from slack_sdk.socket_mode import SocketModeClient
            from slack_sdk.socket_mode.builtin.connection import Connection
        except ImportError:
            self.skipTest('官方Slack SDK/Bolt未安装')
        self.app_type = App
        self.socket_type = SocketModeClient
        self.connection_type = Connection
        temporary = tempfile.TemporaryDirectory(prefix='老五Slack启动-')
        self.addCleanup(temporary.cleanup)
        self.state = SlackState(Store(Path(temporary.name)))
        self.state.set_config({'team_id': 'TONE', 'owner_user_id': 'UOWNER', 'enabled': True})
        self.api = SlackAPI(client=WebClient(token='xoxb-offline-test', retry_handlers=[]))
        self.gateway = SlackGateway(self.state, api=self.api)
        # This suite tests Slack SDK startup; the read proxy has a separate
        # real Unix socket integration test (host-gated in restricted sandboxes).
        self.patched('laowu.slack_read_proxy.SlackReadProxy.start')
        self.patched('laowu.slack_read_proxy.SlackReadProxy.close')
        self.patched(self.state, 'credentials', return_value={
            'bot_token': 'xoxb-offline-test', 'app_token': 'xapp-offline-test'})

        def api_call(client, method, **params):
            self.assertEqual(method, 'auth.test')
            return {'ok': True, 'team_id': 'TONE', 'user_id': 'UBOT'}

        self.api_call = self.patched(WebClient, 'api_call', autospec=True, side_effect=api_call)
        # Fail closed if any path accidentally tries to create a network connection.
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex'):
            guard = self.patched(target, side_effect=AssertionError('network forbidden in offline regression'))
            self.addCleanup(guard.assert_not_called)
        # The real client is constructed; its periodic workers stay dormant so
        # connection transitions are controlled by the test in this thread.
        self.patched('slack_sdk.socket_mode.interval_runner.IntervalRunner.start',
                     autospec=True, side_effect=lambda runner: runner)
        self.addCleanup(self.gateway.close)

    def connection(self, ident, *, active=True):
        connection = self.connection_type(url='wss://offline.invalid', logger=self.api.client.logger)
        connection.session_id = ident
        connection.sock = Mock() if active else None
        return connection

    def completed(self):
        future = concurrent.futures.Future()
        future.set_result(None)
        return future

    def run_sessions(self, sessions, *, futures=None):
        sessions = iter(sessions)

        def connect(client):
            client.current_session = next(sessions)

        def advance(_timeout):
            try:
                self.gateway.socket.current_session = next(sessions)
            except StopIteration:
                self.gateway.closed.set()

        self.patched(self.socket_type, 'connect', autospec=True, side_effect=connect)
        self.patched(self.gateway, 'tick')  # No model or Web API work in this loop.
        self.patched(self.gateway.wake, 'wait', side_effect=advance)
        submit = self.patched(self.gateway.executor, 'submit',
                              side_effect=futures if futures is not None else lambda fn: self.completed())
        runtime = self.patched(self.gateway, '_runtime', wraps=self.gateway._runtime)
        self.gateway.run()
        self.assertTrue(self.gateway.socket.closed)
        self.assertEqual(self.state.status()['runtime']['status'], 'stopped')
        return [call.args[0] for call in runtime.call_args_list], submit

    def test_real_sdk_startup_registers_listeners_at_construction(self):
        constructor = self.patched('slack_sdk.socket_mode.SocketModeClient', wraps=self.socket_type)
        statuses, submit = self.run_sessions([None])
        self.assertIsInstance(self.gateway.socket, self.socket_type)
        self.assertIsInstance(self.gateway.app, self.app_type)
        self.assertEqual(self.state.config()['bot_user_id'], 'UBOT')
        self.api_call.assert_called_once_with(self.api.client, 'auth.test', params={})
        listeners = constructor.call_args.kwargs['on_close_listeners']
        self.assertEqual(len(listeners), 1)
        self.assertEqual(self.gateway.socket.on_close_listeners, listeners)
        self.assertEqual(self.gateway.socket.socket_mode_request_listeners, [self.gateway.socket_request])
        self.assertEqual(statuses, ['connecting', 'disconnected'])
        submit.assert_not_called()
        self.gateway.backfilled = True
        self.gateway.wake.clear()
        listeners[0](1001, 'offline close')
        self.assertFalse(self.gateway.backfilled)
        self.assertTrue(self.gateway.wake.is_set())

    def test_real_sdk_startup_tolerates_nullable_listener_default(self):
        # Model the reported nullable default while retaining real SDK
        # construction, connection inspection, Bolt build and cleanup.
        def nullable_default(**kwargs):
            client = self.socket_type(**kwargs)
            client.on_close_listeners = kwargs.get('on_close_listeners')
            return client

        self.patched('slack_sdk.socket_mode.SocketModeClient', side_effect=nullable_default)
        self.run_sessions([None])
        self.assertIsInstance(self.gateway.socket, self.socket_type)
        self.assertEqual(len(self.gateway.socket.on_close_listeners), 1)

    def test_disconnected_start_and_reconnect_use_real_connection_state(self):
        first, second = self.connection('first'), self.connection('second')
        inactive = self.connection('inactive', active=False)
        statuses, submit = self.run_sessions([inactive, first, None, second, second])
        self.assertEqual(statuses, ['connecting', 'disconnected', 'connected',
                                    'disconnected', 'connected', 'connected'])
        self.assertEqual(submit.call_count, 2)
        self.assertTrue(all(call.args == (self.gateway.backfill,) for call in submit.call_args_list))

    def test_endpoint_rotation_rearms_backfill_without_close_callback(self):
        first, second = self.connection('first'), self.connection('second')
        statuses, submit = self.run_sessions([first, second, second])
        self.assertEqual(statuses, ['connecting', 'connected', 'connected', 'connected'])
        self.assertEqual(submit.call_count, 2)

    def test_close_callback_rearms_backfill_after_fast_reconnect(self):
        connection = self.connection('same-session')

        def sessions():
            yield connection
            self.gateway.socket.on_close_listeners[0](1001, 'offline close')
            yield connection
            yield connection

        statuses, submit = self.run_sessions(sessions())
        self.assertEqual(statuses, ['connecting', 'connected', 'connected', 'connected'])
        self.assertEqual(submit.call_count, 2)

    def test_reconnect_waits_for_inflight_backfill_then_schedules_again(self):
        first, second = self.connection('first'), self.connection('second')
        pending = concurrent.futures.Future()

        def sessions():
            yield first
            yield None
            yield second
            self.assertEqual(self.gateway.executor.submit.call_count, 1)
            self.assertFalse(self.gateway.backfilled)
            pending.set_result(None)
            yield second
            yield second

        statuses, submit = self.run_sessions(sessions(), futures=[pending, self.completed()])
        self.assertEqual(statuses, ['connecting', 'connected', 'disconnected',
                                    'connected', 'connected', 'connected'])
        self.assertEqual(submit.call_count, 2)

    def test_connect_failure_closes_real_sdk_client(self):
        self.patched(self.socket_type, 'connect', side_effect=OSError('offline connect failure'))
        runtime = self.patched(self.gateway, '_runtime', wraps=self.gateway._runtime)
        with self.assertRaisesRegex(OSError, 'offline connect failure'):
            self.gateway.run()
        self.assertTrue(self.gateway.socket.closed)
        self.assertEqual([call.args[0] for call in runtime.call_args_list], ['connecting'])
        self.assertEqual(self.state.status()['runtime']['status'], 'stopped')


if __name__ == '__main__':
    unittest.main()
