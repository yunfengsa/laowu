"""Receipt handling against the actual paged-turn protocol shape."""
import json
import unittest
from unittest.mock import Mock, patch
from laowu.codex_bridge import CodexBridge


class ReceiptTests(unittest.TestCase):
    def event(self, turn=None):
        return {'id': 'event-1', 'target_thread': 'thread-1', 'turn_id': turn}

    def marker(self, value=None):
        return {'type': 'functionCallOutput', 'name': 'background_event',
                'output': json.dumps(value if value is not None else {'event_id': 'event-1'})}

    def turn(self, status='completed', items=None):
        return {'id': 'turn-1', 'status': status, 'items': items or []}

    def test_lost_send_response_matches_marker_across_pages(self):
        bridge=CodexBridge()
        final={'type':'agentMessage','phase':'final_answer','text':'结果'}
        bridge.call=Mock(side_effect=[{'data': [], 'nextCursor':'page-2'},
                                     {'data':[self.turn(items=[self.marker(),final])]}])
        self.assertEqual(bridge.receipt(self.event()),('delivered','turn-1'))
        self.assertEqual(bridge.call.call_args.args[0],'thread/turns/list')
        self.assertEqual(bridge.call.call_args.args[1]['cursor'],'page-2')

    def test_completed_without_final_is_failed_not_infinite_pending(self):
        bridge=CodexBridge()
        bridge.call=Mock(return_value={'data':[self.turn(items=[self.marker()])]})
        self.assertEqual(bridge.receipt(self.event('turn-1')),('failed','turn-1'))

    def test_unrelated_invalid_marker_does_not_become_receipt(self):
        bridge=CodexBridge()
        bridge.call=Mock(return_value={'data':[self.turn(items=[self.marker(['bad-shape'])])]})
        self.assertEqual(bridge.receipt(self.event()),('uncertain',None))

    def test_active_turn_remains_submitted(self):
        bridge=CodexBridge()
        bridge.call=Mock(return_value={'data':[self.turn('inProgress',[self.marker()])]})
        self.assertEqual(bridge.receipt(self.event()),('submitted','turn-1'))

    def test_initialization_failure_closes_socket(self):
        socket=Mock()
        bridge=CodexBridge()
        bridge.call=Mock(side_effect=TimeoutError)
        with patch('websockets.sync.client.unix_connect',return_value=socket):
            with self.assertRaises(TimeoutError):bridge.__enter__()
        socket.close.assert_called_once()

    def test_server_request_with_same_id_is_not_a_response(self):
        bridge=CodexBridge()
        bridge.ws=Mock()
        bridge.ws.recv.side_effect=[json.dumps({'id':1,'method':'item/commandExecution/requestApproval'}),
                                   json.dumps({'id':1,'result':{'confirmed':True}})]
        self.assertEqual(bridge.call('probe'),{'confirmed':True})
        self.assertEqual(bridge.ws.send.call_count,1)

if __name__=='__main__':unittest.main()
