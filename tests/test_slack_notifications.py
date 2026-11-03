import tempfile
import unittest
import json
from unittest.mock import patch
from laowu.store import Store
from laowu.slack_state import SlackState, conversation_id
from laowu.background import Outbox
from laowu.slack_notifications import route_slack_notifications
from laowu.service import Service
from laowu.runtime import Manager


class SlackNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.state = SlackState(self.store)
        self.state.set_config({'team_id': 'T01', 'owner_user_id': 'U01', 'enabled': True})
        self.state.runtime(status='connected')
        self.state.update_channel('T01', 'D01', personal=1, last_activity=10)
        self.state.enqueue('T01', 'D01', '1.1', '你好')
        self.box = Outbox(self.store)

    def test_no_native_server_is_needed_and_unclear_audience_uses_private(self):
        self.state.update_channel('T01', 'C01', last_activity=99999999999)
        notice = self.box.publish('后台交接', event_key='ev1')
        self.assertEqual(route_slack_notifications(self.store), 1)
        value = self.box.get(notice['id'])
        self.assertEqual(value['target_thread'], 'slack:T01:D01:0')
        self.assertEqual(value['status'], 'submitted')
        self.assertEqual(route_slack_notifications(self.store), 0)
        self.assertEqual(len(self.state.inbox()), 2)

    def test_origin_is_not_changed_to_newest_channel(self):
        origin = conversation_id(self.state.channel('T01', 'D01'))
        self.state.update_channel('T01', 'D02', personal=1, last_activity=99999999999)
        notice = self.box.publish('异步结果', event_key='ev2', origin_thread=origin)
        route_slack_notifications(self.store)
        self.assertEqual(self.box.get(notice['id'])['target_thread'], origin)

    def test_revoked_original_endpoint_waits(self):
        origin = conversation_id(self.state.channel('T01', 'D01'))
        notice = self.box.publish('异步结果', event_key='ev3', origin_thread=origin)
        self.state.update_channel('T01', 'D01', eligible=0)
        self.assertEqual(route_slack_notifications(self.store), 0)
        self.assertEqual(self.box.get(notice['id'])['status'], 'pending')

    def test_native_origin_is_never_taken_by_slack(self):
        notice = self.box.publish('原桌面结果', event_key='ev4', origin_thread='native-thread')
        self.assertEqual(route_slack_notifications(self.store), 0)
        self.assertIsNone(self.box.get(notice['id'])['target_thread'])

    def native_result(self):
        scheduling = patch.object(Manager, '_schedule', return_value=None)
        scheduling.start()
        self.addCleanup(scheduling.stop)
        gui = Service(self.store.root)
        self.addCleanup(gui.close)
        gui.registry.ensure(dict(id='confidant', name='test', description='test', instructions='test',
                                 skills=[], tools=['memory', 'checkpoint'], can_write=False))
        task = gui.call('task_start', {'friend_id': 'confidant', 'request': 'native scheduled result'})
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='completed',result=? WHERE id=?", (json.dumps({'result': '原生任务结果'}), task['id']))
        return task

    def scoped(self, channel='D01'):
        self.state.update_channel('T01', channel, personal=1)
        service = Service(self.store.root, slack_token_file=self.state.issue_binding('T01', channel))
        self.addCleanup(service.close)
        return service

    def test_routed_native_result_is_read_only_and_channel_bound(self):
        task = self.native_result()
        notice = self.box.publish('任务完成', event_key='native-result', task_id=task['id'])
        self.assertEqual(route_slack_notifications(self.store), 1)
        service = self.scoped()
        record = service.call('task_status', {'task_id': task['id']})
        self.assertEqual(record['result']['result'], '原生任务结果')
        self.assertEqual(service.call('task_wait', {'task_id': task['id'], 'timeout': 0})['id'], task['id'])
        for operation in ('task_cancel', 'task_resume', 'task_checkpoint'):
            args = {'task_id': task['id']}
            if operation == 'task_checkpoint':
                args['summary'] = '不能修改原任务'
            with self.subTest(operation=operation), self.assertRaises(PermissionError):
                service.call(operation, args)
        other = self.scoped('D02')
        with self.assertRaises(PermissionError):
            other.call('task_status', {'task_id': task['id']})
        self.assertEqual(self.box.get(notice['id'])['target_thread'], 'slack:T01:D01:0')

    def test_delivered_payload_cleanup_preserves_only_result_read_grant(self):
        from laowu.slack_gateway import SlackGateway
        task = self.native_result()
        notice = self.box.publish('结果通知', event_key='clean-result', task_id=task['id'])
        route_slack_notifications(self.store)
        source = next(row for row in self.state.inbox() if row['origin_notice'] == notice['id'])
        self.state.update_input(source['id'], status='completed')
        output = self.state.queue_output(source['id'], '只在Slack保存的消息正文')
        self.state.update_output(output['id'], status='sent', slack_ts='4.000001')
        gateway = SlackGateway(self.state)
        try:
            gateway._settle_notifications()
        finally:
            gateway.close()
        self.assertEqual(self.state.outputs()[0]['body'], '')
        self.assertEqual(self.state.outputs()[0]['slack_ts'], '4.000001')
        self.assertEqual(self.box.get(notice['id'])['status'], 'delivered')
        service = self.scoped()
        self.assertEqual(service.call('task_status', {'task_id': task['id']})['id'], task['id'])
        with self.assertRaises(PermissionError):
            service.call('task_resume', {'task_id': task['id']})
        unrelated = self.native_result()
        with self.assertRaises(PermissionError):
            service.call('task_status', {'task_id': unrelated['id']})


if __name__ == '__main__':
    unittest.main()
