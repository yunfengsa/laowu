"""Exercise the uninjected production startup with the real official SDK."""
import tempfile
import unittest
from unittest.mock import patch

from laowu.slack_gateway import SlackGateway, _SDK_LOG
from laowu.slack_state import SlackState
from laowu.store import Store


class SDKStartupTests(unittest.TestCase):
    def test_default_webclient_initializes_readonly_logger_before_authentication(self):
        try:
            from slack_sdk import WebClient
            from slack_sdk.socket_mode import SocketModeClient
            import slack_bolt
        except ImportError:
            self.skipTest('安装 Slack 可选依赖后执行官方 SDK 启动检查')

        with tempfile.TemporaryDirectory(prefix='老五Slack启动-') as root:
            state = SlackState(Store(root))
            state.set_config({'team_id': 'TTEST', 'owner_user_id': 'UOWNER', 'enabled': True})
            gateway = SlackGateway(state)
            with patch.object(state, 'credentials', return_value={
                    'app_token': 'xapp-offline-test', 'bot_token': 'xoxb-offline-test'}), \
                    patch.object(WebClient, 'api_call', return_value={
                        'ok': True, 'team_id': 'TTEST', 'user_id': 'UBOT'}) as auth, \
                    patch.object(SocketModeClient, 'connect', lambda _: gateway.closed.set()), \
                    patch('laowu.slack_read_proxy.SlackReadProxy.start'), \
                    patch('laowu.slack_read_proxy.SlackReadProxy.close'), \
                    patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
                try:
                    gateway.run()
                    self.assertIs(gateway.api.client.logger, _SDK_LOG)
                    self.assertEqual(auth.call_args.args, ('auth.test',))
                finally:
                    gateway.close()


if __name__ == '__main__':
    unittest.main()
