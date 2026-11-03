"""Slack ingress, isolation, recovery and receipts without live Slack/model calls."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from laowu.common import now
from laowu.slack_client import SlackAPI, SlackAPIError, split_message
from laowu.slack_gateway import SlackGateway, normalize_message
from laowu.slack_state import SlackState
from laowu.store import Store


class FakeAPI:
    def __init__(self):
        self.membership = {'CONE': {'UOWNER', 'UBOT'}, 'CTWO': {'UOWNER', 'UBOT'}, 'DONE': {'UOWNER', 'UBOT'}}
        self.infos = {'CONE': {'is_private': False}, 'CTWO': {'is_private': True},
                      'DONE': {'is_im': True, 'user': 'UOWNER'}}
        self.posts = []
        self.statuses = []
        self.messages = []
        self.uncertain = False
        self.rate_limit = False
        self.unavailable = False
        self.modal_calls = []

    def info(self, channel):
        if self.unavailable:
            raise SlackAPIError('missing_scope')
        return self.infos[channel]

    def members(self, channel):
        return self.membership[channel]

    def status(self, channel, thread_ts, status, owner=None):
        self.statuses.append((channel, thread_ts, status))
        return {'ok': True}

    def post(self, channel, text, *, thread_ts=None, key=None, blocks=None):
        if self.rate_limit:
            raise SlackAPIError('ratelimited', retry_after=12)
        self.posts.append({'channel': channel, 'text': text, 'thread_ts': thread_ts, 'key': key, 'blocks': blocks})
        if self.uncertain:
            raise SlackAPIError('transport_uncertain', uncertain=True)
        return {'ok': True, 'ts': str(len(self.posts)) + '.000001'}

    def reconcile(self, channel, *, key, oldest, thread_ts=None):
        for index, post in enumerate(self.posts):
            if post['channel'] == channel and post['key'] == key:
                return str(index + 1) + '.000001'
        return None

    def history(self, channel, *, oldest, thread_ts=None):
        return iter(self.messages)

    def call(self, method, **params):
        self.modal_calls.append((method, params))
        return {'ok': True}


class FakeSession:
    created = []
    gate = None

    def __init__(self, root, token_file, thread_id=None, on_notification=None, on_request=None):
        self.thread_id = thread_id or 'thread-' + str(len(self.created))
        self.starts = []
        self.closed = False
        self.replies = []
        self.interrupts = []
        self.on_request = on_request
        self.created.append(self)

    def start(self):
        return self.thread_id

    def start_turn(self, text, client_message_id, extra_context=''):
        self.starts.append((text, client_message_id))
        return 'turn-' + client_message_id

    def start_event(self, event):
        self.starts.append(('event', event['id']))
        return 'event-turn'

    def history_receipt(self, ident, turn_id=None):
        if self.gate is not None and not self.gate.is_set():
            return {'status': 'submitted', 'turn_id': turn_id}
        return {'status': 'completed', 'turn_id': turn_id, 'final_text': '老五收到。'}

    def interrupt(self, turn):
        self.interrupts.append(turn)

    def reply(self, ident, result):
        self.replies.append((ident, result))

    def close(self):
        self.closed = True


class GatewayTests(unittest.TestCase):
    def test_provider_error_diagnostic_is_bounded_and_visible_without_raw_body(self):
        item = self.state.enqueue('TONE', 'CONE', '1.01', '测试')
        self.gateway.active[('TONE', 'CONE')] = item
        self.gateway._notification(('TONE', 'CONE'), {'method': 'error', 'params': {
            'error': {'diagnostic': {'category': 'authentication'}, 'message': 'PRIVATE_PROVIDER_BODY'}}})
        self.assertEqual(self.gateway.turn_errors[item['id']], 'client_authentication')
        self.assertNotIn('PRIVATE_PROVIDER_BODY', json.dumps(self.gateway.turn_errors))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='老五Slack-')
        self.store = Store(Path(self.tmp.name))
        self.state = SlackState(self.store)
        self.state.set_config({'team_id': 'TONE', 'owner_user_id': 'UOWNER', 'bot_user_id': 'UBOT', 'enabled': True})
        self.api = FakeAPI()
        FakeSession.created = []
        FakeSession.gate = None
        self.gateway = SlackGateway(self.state, api=self.api, session_factory=FakeSession)

    def tearDown(self):
        FakeSession.gate = None
        self.gateway.close()
        self.tmp.cleanup()

    def event(self, text='你好', *, channel='CONE', user='UOWNER', ts='1783000000.100001', kind='message', **extra):
        return {'type': 'event_callback', 'team_id': 'TONE', 'event_id': channel + ':' + kind + ':' + ts,
                'event': {'type': kind, 'channel': channel, 'user': user, 'ts': ts, 'text': text, **extra}}

    def drain(self):
        for _ in range(200):
            self.gateway.tick()
            if not self.gateway.running and not self.state.inbox('pending'):
                self.gateway.deliver_outputs()
                return
            time.sleep(0.005)
        self.fail('gateway未完成测试任务')

    def routing_snapshot(self):
        with self.store.connect() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                    for table in ('slack_channels', 'slack_threads', 'slack_inbox', 'slack_outbox', 'read_sources')}

    def assert_ignored_without_routing_effects(self, payload):
        before = self.routing_snapshot()
        self.gateway.wake.clear()
        self.assertFalse(self.gateway.ingest(payload))
        self.assertEqual(self.routing_snapshot(), before)
        self.assertFalse(self.gateway.wake.is_set())
        self.assertEqual(FakeSession.created, [])
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.api.statuses, [])
        self.assertEqual(self.api.modal_calls, [])

    def test_current_source_and_thread_are_bound_without_fetching_history(self):
        self.assertTrue(self.gateway.ingest(self.event('<@UBOT> 前面说了什么', thread_ts='1783000000.000001')))
        self.drain()
        session = FakeSession.created[0]
        self.assertEqual(session.slack_context['channel_id'], 'CONE')
        self.assertEqual(session.slack_context['reply_thread_ts'], '1783000000.000001')
        self.assertEqual(session.slack_context['context'], 'normal')
        self.assertEqual(self.api.modal_calls, [])

    def test_trigger_owner_bot_dedupe_and_thread_engagement(self):
        self.assertFalse(self.gateway.ingest(self.event()))
        self.assertFalse(self.gateway.ingest(self.event('<@UBOT> 你好', user='UOTHER')))
        self.assertFalse(self.gateway.ingest(self.event('<@UBOT> 你好', bot_id='BOTHER')))
        self.assertTrue(self.gateway.ingest(self.event('<@UBOT> 你好')))
        self.assertFalse(self.gateway.ingest(self.event('<@UBOT> 你好', kind='app_mention')))
        self.assertEqual(len(self.state.inbox()), 1)
        self.assertTrue(self.gateway.ingest(self.event('接着聊', ts='1783000001.100001', thread_ts='1783000000.100001')))
        self.assertEqual(self.state.inbox()[1]['reply_ts'], '1783000000.100001')

    def test_direct_dm_and_files(self):
        self.state.update_channel('TONE', 'CONE', mode='direct')
        self.assertTrue(self.gateway.ingest(self.event('无需@')))
        self.assertTrue(self.gateway.ingest(self.event('', channel='DONE', files=[{'id': 'FONE'}])))
        self.assertIn('首版暂不读取附件', self.state.inbox()[1]['body'])
        self.assertIsNone(self.state.inbox()[1]['reply_ts'])

    def test_engaged_thread_addressed_only_to_other_user_is_ignored(self):
        root_ts = '1783000000.100001'
        self.state.channel('TONE', 'CONE', create=True)
        self.state.engage('TONE', 'CONE', root_ts)
        self.assert_ignored_without_routing_effects(self.event(
            '<@UOTHER> 请处理', ts='1783000001.100001', thread_ts=root_ts))
        self.gateway.tick()
        self.assertEqual(self.state.inbox(), [])
        self.assertEqual(self.state.outputs(), [])
        self.assertEqual(FakeSession.created, [])

    def test_other_recipients_override_engagement_direct_mode_and_app_mention(self):
        root_ts = '1783000000.100001'
        self.state.channel('TONE', 'CONE', create=True)
        self.state.engage('TONE', 'CONE', root_ts)
        self.state.update_channel('TONE', 'CTWO', mode='direct')
        texts = (
            '<@UOTHER> 请处理', '<@WOTHER> 请处理', '<@UOTHER|另一位> 请处理',
            '<@UOTHER> <@UAGENT> 请处理', '<@UAGENT> &lt;root&gt;检查&lt;/root&gt;',
            '<@UOTHER> 示例 &lt;@UBOT&gt;', '<@UOTHER> `<@UBOT>`',
            '<@UOTHER>\n```\n<@UBOT>\n```', '<@UOTHER>\n> <@UBOT>',
            '<@UOTHER>\n&gt; <@UBOT>', '<@UOTHER|UBOT> 请处理',
            '<@UOTHER> @UBOT 请处理',
        )
        # Busy channels would otherwise emit a queued receipt immediately.
        with patch.dict(self.gateway.running, {('TONE', 'CONE'): object(), ('TONE', 'CTWO'): object()}):
            for channel, thread_ts in (('CONE', root_ts), ('CTWO', None), ('CNEW', None)):
                for kind in ('message', 'app_mention'):
                    for text in texts:
                        with self.subTest(channel=channel, kind=kind, text=text):
                            self.assert_ignored_without_routing_effects(self.event(
                                text, channel=channel, kind=kind, thread_ts=thread_ts))
        self.assertIsNone(self.state.channel('TONE', 'CNEW'))
        self.gateway.tick()
        self.assertEqual(FakeSession.created, [])

    def test_self_and_multiple_recipients_are_accepted_once(self):
        texts = ('<@UBOT> 你好', '<@UOTHER> <@UBOT> 一起处理',
                 '<@UBOT> <@UOTHER> 一起处理', '<@UOTHER> <@UBOT|老五> 一起处理',
                 '<@UBOT|老五> 你好')
        for index, text in enumerate(texts):
            ts = f'178300000{index}.100001'
            with self.subTest(text=text):
                self.assertTrue(self.gateway.ingest(self.event(text, ts=ts)))
                self.assertFalse(self.gateway.ingest(self.event(text, ts=ts, kind='app_mention')))
                self.assertEqual(len(self.state.inbox()), index + 1)
                self.drain()
        self.assertEqual(len(FakeSession.created), 1)
        self.assertEqual(len(FakeSession.created[0].starts), len(texts))
        self.assertEqual(len(self.api.posts), len(texts))

    def test_w_user_id_and_labelled_self_mention(self):
        self.state.set_config({'bot_user_id': 'WBOT'})
        self.assertTrue(self.gateway.ingest(self.event('<@WBOT|老五> &lt;root&gt;检查&lt;/root&gt;')))
        self.assertEqual(self.state.inbox()[0]['context'], 'normal')
        self.assertTrue(self.state.inbox()[0]['body'].startswith('<@WBOT|老五>'))
        self.assertFalse(self.gateway.ingest(self.event('<@WBOT> 检查', kind='app_mention')))
        self.assertEqual(len(self.state.inbox()), 1)

    def test_app_mention_event_without_real_self_does_not_trigger(self):
        texts = ('`<@UBOT>`', '```\n<@UBOT>\n```', '> <@UBOT>',
                 '&gt; <@UBOT>', '&lt;@UBOT&gt;',
                 '>>> 引用\n<@UBOT>', '@UBOT', '普通消息')
        for index, text in enumerate(texts):
            for kind in ('message', 'app_mention'):
                with self.subTest(text=text, kind=kind):
                    self.assert_ignored_without_routing_effects(self.event(
                        text, channel=f'CLITERAL{index}', kind=kind))

    def test_literals_do_not_address_users_or_prevent_thread_continuation(self):
        root_ts = '1783000000.100001'
        self.state.channel('TONE', 'CONE', create=True)
        self.state.engage('TONE', 'CONE', root_ts)
        texts = (
            '普通续聊', '@UOTHER 普通名字', '&lt;@UOTHER&gt; 转义示例',
            '`<@UOTHER>` 行内代码', '```\n<@UOTHER>\n```\n代码示例',
            '> <@UOTHER> 引用\n继续聊', '&gt; <@UOTHER> 引用\n继续聊',
            '>>> 引用\n<@UOTHER>', '&gt;&gt;&gt; 引用\n<@UOTHER>',
            '`<@UOTHER>', '```\n<@UOTHER>',
        )
        for index, text in enumerate(texts):
            with self.subTest(text=text):
                # The same literal cannot start a conversation in an unknown thread.
                self.assert_ignored_without_routing_effects(self.event(
                    text.replace('UOTHER', 'UBOT'), channel='CNEW', thread_ts='1.000001'))
                ts = f'178300001{index}.100001'
                self.assertTrue(self.gateway.ingest(self.event(text, ts=ts, thread_ts=root_ts)))
                item = self.state.inbox()[-1]
                self.assertEqual(item['reply_ts'], root_ts)
                self.assertEqual(item['context'], 'normal')
        self.assertIsNone(self.state.channel('TONE', 'CNEW'))

    def test_mentions_outside_code_and_quotes_still_address_users(self):
        self.state.update_channel('TONE', 'CONE', mode='direct')
        texts = (
            '`<@UBOT>` <@UOTHER> 请处理', '```<@UBOT>``` <@UOTHER> 请处理',
            '> <@UBOT>\n<@UOTHER> 请处理', '&gt; <@UBOT>\r\n<@UOTHER> 请处理',
            '> ```<@UBOT>\n<@UOTHER> 请处理',
        )
        for index, text in enumerate(texts):
            with self.subTest(text=text):
                self.assert_ignored_without_routing_effects(self.event(text))
                self.assertTrue(self.gateway.ingest(self.event(
                    text.replace('UBOT', 'ULITERAL').replace('UOTHER', 'UBOT'),
                    ts=f'178300000{index}.100001')))

    def test_no_mention_in_unknown_thread_or_empty_input_creates_no_channel(self):
        for text, extra in (('普通消息', {}), ('普通续聊', {'thread_ts': '1.000001'}),
                            ('', {}), ('<@UBOT>', {}), ('<@UBOT|老五>', {})):
            with self.subTest(text=text, extra=extra):
                self.assert_ignored_without_routing_effects(self.event(text, **extra))
        self.assertEqual(self.state.channels(), [])

    def test_dm_keeps_third_party_mentions_as_conversation(self):
        for index, extra in enumerate(({'channel': 'DONE'}, {'channel': 'CTWO', 'channel_type': 'im'})):
            with self.subTest(extra=extra):
                self.assertTrue(self.gateway.ingest(self.event(
                    '<@UOTHER> 帮我解释这句话', ts=f'178300000{index}.100001', **extra)))
                item = self.state.inbox()[-1]
                self.assertEqual(item['body'], '<@UOTHER> 帮我解释这句话')
                self.assertIsNone(item['reply_ts'])
                self.assertEqual(item['context'], 'normal')

    def test_recipient_rule_does_not_bypass_identity_or_event_filters(self):
        self.state.channel('TONE', 'CONE', create=True)
        root_ts = '1783000000.100001'
        self.state.engage('TONE', 'CONE', root_ts)
        for extra in ({'user': 'UOTHER'}, {'bot_id': 'BOTHER'}, {'subtype': 'bot_message'},
                      {'subtype': 'message_changed'}, {'kind': 'reaction_added'}, {'ts': 'invalid'}):
            with self.subTest(extra=extra):
                self.assert_ignored_without_routing_effects(self.event(
                    '<@UBOT> <@UOTHER> 你好', thread_ts=root_ts, **extra))
        wrong_team = self.event('<@UBOT> 你好', channel='CNEW')
        wrong_team['team_id'] = 'TOTHER'
        self.assert_ignored_without_routing_effects(wrong_team)
        self.state.set_config({'enabled': False})
        self.assert_ignored_without_routing_effects(self.event('<@UBOT> 你好', channel='CNEW'))

    def test_files_follow_recipient_rule_without_reading_attachment_text(self):
        root_ts = '1783000000.100001'
        self.state.channel('TONE', 'CONE', create=True)
        self.state.engage('TONE', 'CONE', root_ts)
        files = [{'id': 'FONE', 'title': '<@UBOT> 附件'}]
        self.assert_ignored_without_routing_effects(self.event(
            '<@UOTHER> 看附件', thread_ts=root_ts, subtype='file_share', files=files))
        for index, text in enumerate(('', '<@UBOT> <@UOTHER> 看附件')):
            self.assertTrue(self.gateway.ingest(self.event(
                text, ts=f'178300000{index + 1}.100001', thread_ts=root_ts,
                subtype='file_share', files=files)))
            self.assertIn('首版暂不读取附件', self.state.inbox()[-1]['body'])

    def test_root_normalization_preserves_literal_and_other_recipient_boundaries(self):
        for prefix in ('', '<@UBOT> '):
            for body in ('<root>检查</root>', '&lt;root&gt;检查&lt;/root&gt;'):
                with self.subTest(prefix=prefix, body=body):
                    self.assertEqual(normalize_message(prefix + body, 'UBOT'), ('<root>检查</root>', 'root'))
        texts = (
            '&lt;@UBOT&gt; &lt;root&gt;检查&lt;/root&gt;',
            '&lt;@UBOT|老五&gt; <root>检查</root>',
            '<@UBOT|老五> <root>检查</root>',
            '<@UBOT|另一位> &lt;root&gt;检查&lt;/root&gt;',
            '<@UBOT> &lt;@UBOT&gt; <root>检查</root>',
            '<@UBOT> <@UOTHER> <root>检查</root>',
            '<@UBOT> <@UBOT> <root>检查</root>',
            '<@UOTHER|老五> <root>检查</root>',
            '<@UBOT> `<root>检查</root>`',
            '<@UBOT> ```\n<root>检查</root>\n```',
            '> <@UBOT> <root>检查</root>',
            '&gt; <@UBOT|老五> &lt;root&gt;检查&lt;/root&gt;',
            '<@UBOT> 引用 <root>检查</root>',
        )
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(normalize_message(text, 'UBOT')[1], 'normal')
        # DM delivery must not promote an escaped example to a maintenance request.
        self.assertTrue(self.gateway.ingest(self.event(texts[0], channel='DONE')))
        self.assertEqual(self.state.inbox()[0]['context'], 'normal')
        self.assertTrue(self.state.inbox()[0]['body'].startswith('<@UBOT>'))

    def test_root_only_whole_owner_message_and_credentials_redacted(self):
        self.gateway.ingest(self.event('<@UBOT> &lt;root&gt;检查配置&lt;/root&gt;'))
        self.assertEqual(self.state.inbox()[0]['context'], 'root')
        self.gateway.ingest(self.event('<@UBOT> ```\n<root>检查配置</root>\n```', ts='1783000001.100001'))
        self.assertEqual(self.state.inbox()[1]['context'], 'normal')
        self.gateway.ingest(self.event('<@UBOT> xoxb-do-not-save-this', ts='1783000002.100001'))
        self.assertNotIn('xoxb-', self.state.inbox()[2]['body'])

    def test_private_membership_revokes_context_and_pauses_output(self):
        self.state.update_channel('TONE', 'CTWO', private_approved=1)
        private = self.gateway.qualify('TONE', 'CTWO')
        self.assertEqual(private['personal'], 1)
        self.state.bind_thread('TONE', 'CTWO', 'old-private')
        record = self.state.enqueue('TONE', 'CTWO', '1783000000.100001', '隐私')
        self.state.queue_output(record['id'], '私密结果')
        self.api.membership['CTWO'].add('UOTHER')
        public = self.gateway.qualify('TONE', 'CTWO')
        self.assertEqual(public['personal'], 0)
        self.assertEqual(public['generation'], 1)
        self.assertIsNone(public['thread_id'])
        self.gateway.deliver_outputs()
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.state.outputs()[0]['status'], 'cancelled')

    def test_failure_to_check_members_fails_closed(self):
        self.state.update_channel('TONE', 'CTWO', private_approved=1)
        self.gateway.qualify('TONE', 'CTWO')
        self.state.bind_thread('TONE', 'CTWO', 'old-private')
        self.api.unavailable = True
        with self.assertRaises(SlackAPIError):
            self.gateway.qualify('TONE', 'CTWO')
        row = self.state.channel('TONE', 'CTWO')
        self.assertEqual((row['eligible'], row['personal'], row['thread_id']), (0, 0, None))

    def test_end_to_end_two_messages_reuse_channel_thread(self):
        self.gateway.ingest(self.event('<@UBOT> 第一条'))
        self.gateway.ingest(self.event('<@UBOT> 第二条', ts='1783000001.100001'))
        self.drain()
        self.assertEqual(len(FakeSession.created), 1)
        self.assertEqual(len(FakeSession.created[0].starts), 2)
        self.assertEqual([r['status'] for r in self.state.inbox()], ['completed', 'completed'])
        self.assertEqual(len(self.api.posts), 2)
        self.assertTrue(all(p['thread_ts'] for p in self.api.posts))

    def test_uncertain_delivery_reconciled_without_duplicate_post(self):
        source = self.state.enqueue('TONE', 'CONE', '1783000000.100001', '请求', reply_ts='1783000000.100001')
        self.state.queue_output(source['id'], '结果')
        self.api.uncertain = True
        self.gateway.deliver_outputs()
        row = self.state.outputs()[0]
        self.assertEqual(row['status'], 'uncertain')
        self.state.update_output(row['id'], next_attempt=0)
        self.gateway.deliver_outputs()
        self.assertEqual(self.state.outputs()[0]['status'], 'sent')
        self.assertEqual(len(self.api.posts), 1)

    def test_missing_uncertain_receipt_does_not_repost(self):
        source = self.state.enqueue('TONE', 'CONE', '1783000000.100001', '请求')
        row = self.state.queue_output(source['id'], '结果')
        self.state.update_output(row['id'], status='sending')
        self.gateway.deliver_outputs()
        self.assertEqual(self.state.outputs()[0]['status'], 'uncertain')
        self.assertEqual(self.api.posts, [])

    def test_rate_limit_preserves_intent(self):
        source = self.state.enqueue('TONE', 'CONE', '1783000000.100001', '请求')
        self.state.queue_output(source['id'], '结果')
        self.api.rate_limit = True
        before = time.time()
        self.gateway.deliver_outputs()
        row = self.state.outputs()[0]
        self.assertEqual(row['status'], 'pending')
        self.assertGreaterEqual(row['next_attempt'], before + 12)

    def test_approval_owner_epoch_and_replay(self):
        source = self.state.enqueue('TONE', 'CONE', '1783000000.100001', '请求', reply_ts='1783000000.100001')
        channel = self.gateway.qualify('TONE', 'CONE')
        self.state.update_input(source['id'], status='processing')
        self.gateway.active[('TONE', 'CONE')] = source
        self.gateway._server_request(('TONE', 'CONE'), {'id': 7, 'method': 'item/commandExecution/requestApproval', 'params': {'command': 'python test.py'}})
        output = self.state.outputs()[0]
        button = json.loads(output['blocks'])[-1]['elements'][0]
        payload = {'type': 'block_actions', 'team': {'id': 'TONE'}, 'user': {'id': 'UOTHER'}, 'channel': {'id': 'CONE'}, 'actions': [button]}
        self.assertFalse(self.gateway.ingest(payload))
        payload['user']['id'] = 'UOWNER'
        self.assertTrue(self.gateway.ingest(payload))
        self.assertFalse(self.gateway.ingest(payload))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM slack_approvals').fetchone()[0], 'answered')

    def test_no_first_install_history_import(self):
        self.state.channel('TONE', 'CONE', create=True)
        self.api.messages = [{'ts': str(time.time()), 'user': 'UOWNER', 'text': '<@UBOT> 过去的消息'}]
        self.gateway.backfill()
        self.assertEqual(self.state.inbox(), [])

    def test_backfill_recovers_known_thread_in_order_without_duplicates(self):
        self.gateway.clock = lambda: 200000
        self.gateway.ingest(self.event('<@UBOT> 原线程', ts='1.000001'))
        self.state.channel('TONE', 'CTWO', create=True)  # No cursor: never import.
        calls = []

        def history(channel, *, oldest, thread_ts=None):
            calls.append((channel, oldest, thread_ts))
            if thread_ts is None:
                return iter([
                    {'ts': '113599.000001', 'user': 'UOWNER', 'text': '<@UBOT> 超过24小时'},
                    {'ts': '199992.000001', 'user': 'UOWNER', 'text': '<@UBOT> 新请求'},
                ])
            if thread_ts == '1.000001':
                reply = {'ts': '199991.000001', 'user': 'UOWNER', 'text': '线程续聊'}
                return iter([reply, reply])
            return iter([])

        self.api.history = history
        self.gateway.backfill()
        self.assertEqual(calls, [('CONE', 113600, None), ('CONE', 113600, '1.000001')])
        inputs = self.state.inbox()
        self.assertEqual([row['message_ts'] for row in inputs], ['1.000001', '199991.000001', '199992.000001'])
        self.assertEqual(inputs[1]['reply_ts'], '1.000001')
        self.assertFalse(self.gateway.ingest(self.event('<@UBOT> 新请求', ts='199992.000001')))
        self.gateway._socket_closed()
        self.gateway.backfill()
        self.assertEqual(len(self.state.inbox()), 3)
        self.assertEqual(FakeSession.created, [])

    def test_background_completion_only_after_slack_receipt(self):
        from laowu.background import Outbox
        event = Outbox(self.store).publish('后台完成', event_key='gateway-test')
        source = self.state.enqueue('TONE', 'CONE', 'notice:' + event['id'], '后台完成', origin_notice=event['id'])
        self.state.update_input(source['id'], status='completed')
        self.state.queue_output(source['id'], '后台结果')
        self.gateway._settle_notifications()
        self.assertEqual(Outbox(self.store).get(event['id'])['status'], 'pending')
        self.gateway.deliver_outputs()
        self.gateway._settle_notifications()
        self.assertEqual(Outbox(self.store).get(event['id'])['status'], 'delivered')

    def test_idle_session_release_keeps_mapping(self):
        row = self.gateway.qualify('TONE', 'CONE')
        session = self.gateway._get_session(row)
        self.gateway.last_used[('TONE', 'CONE')] = time.time() - 601
        self.gateway.tick()
        self.assertTrue(session.closed)
        self.assertEqual(self.state.channel('TONE', 'CONE')['thread_id'], session.thread_id)

    def test_stop_expires_approval_before_late_click(self):
        source = self.state.enqueue('TONE', 'CONE', str(time.time()), '请求', reply_ts='1783000000.100001')
        self.state.update_input(source['id'], status='processing')
        self.gateway.qualify('TONE', 'CONE')
        self.gateway.active[('TONE', 'CONE')] = source
        self.gateway._server_request(('TONE', 'CONE'), {'id': 8, 'method': 'item/commandExecution/requestApproval', 'params': {'command': 'python test.py'}})
        button = json.loads(self.state.outputs()[0]['blocks'])[-1]['elements'][0]
        self.gateway._stop_event('TONE', 'CONE', source['reply_ts'], 'stop:unique', str(time.time() + 1))
        payload = {'type': 'block_actions', 'team': {'id': 'TONE'}, 'user': {'id': 'UOWNER'}, 'channel': {'id': 'CONE'}, 'actions': [button]}
        self.assertFalse(self.gateway.ingest(payload))
        self.gateway._approvals()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM slack_approvals').fetchone()[0], 'expired')

    def test_old_native_stop_cannot_cancel_a_new_request(self):
        source = self.state.enqueue('TONE', 'CONE', str(time.time()), '请求', reply_ts='1783000000.100001')
        self.state.update_input(source['id'], status='processing')
        self.gateway.active[('TONE', 'CONE')] = source
        self.gateway._stop_event('TONE', 'CONE', source['reply_ts'], 'old-stop', '100.000001')
        self.assertEqual(self.gateway._input(source['id'])['status'], 'processing')

    def test_manually_cancelled_inputs_stay_cancelled_after_duplicates_and_recovery(self):
        root_ts = '1783000000.000001'
        originals = []
        for index, text in enumerate(('<@UOTHER> 旧版误接收', '<@UBOT> <@UOTHER> 一起处理')):
            payload = self.event(text, ts=f'178300000{index + 1}.100001', thread_ts=root_ts)
            originals.append(payload['event'])
            # Seed a legacy inbox entry; production history is never touched.
            source = self.state.enqueue('TONE', 'CONE', payload['event']['ts'], text,
                                        reply_ts=root_ts, event_id=payload['event_id'])
            self.state.engage('TONE', 'CONE', root_ts)
            self.state.update_input(source['id'], status='processing')
            self.gateway.active[('TONE', 'CONE')] = source
            self.assertTrue(self.gateway.ingest(self.event(
                '', kind='agent_session_stopped', ts=payload['event']['ts'],
                thread_ts=root_ts, event_ts=str(time.time() + 1))))
            self.assertEqual(self.gateway._input(source['id'])['error'], 'user_stopped')
        self.gateway.active.clear()
        before = self.state.inbox()
        self.state.recover()
        for original in originals:
            for kind in ('message', 'app_mention'):
                self.assertFalse(self.gateway.ingest(self.event(
                    original['text'], ts=original['ts'], thread_ts=root_ts, kind=kind)))
        # Exercise a repeated history page with a lagging synthetic watermark.
        self.state.update_channel('TONE', 'CONE', last_ts=root_ts)
        self.gateway.clock = lambda: 1783000010
        self.api.messages = originals + [{'ts': '1783000003.100001', 'user': 'UOWNER',
                                         'text': '<@UOTHER> 新请求', 'thread_ts': root_ts}]
        self.gateway.backfill()
        self.assertEqual(self.state.inbox(), before)
        self.gateway.tick()
        self.assertEqual([row['status'] for row in self.state.inbox()], ['cancelled', 'cancelled'])
        self.assertEqual([row['error'] for row in self.state.inbox()], ['user_stopped', 'user_stopped'])
        self.assertEqual(self.state.outputs(), [])
        self.assertEqual(FakeSession.created, [])
        self.assertEqual(self.api.posts, [])

    def test_pause_keeps_new_input_and_output_durable_without_model(self):
        self.gateway.ingest(self.event('<@UBOT> 队列任务'))
        source = self.state.inbox()[0]
        self.state.queue_output(source['id'], '尚不发送')
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO background_meta VALUES('paused','1')")
        self.gateway.tick()
        self.gateway.deliver_outputs()
        self.assertEqual(FakeSession.created, [])
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.state.inbox()[0]['status'], 'pending')

    def test_two_channel_limit_and_same_channel_serial(self):
        FakeSession.gate = threading.Event()
        self.gateway.ingest(self.event('<@UBOT> 一'))
        self.gateway.ingest(self.event('<@UBOT> 二', ts='1783000001.100001'))
        self.gateway.ingest(self.event('<@UBOT> 三', channel='CTWO', ts='1783000002.100001'))
        self.gateway.ingest(self.event('四', channel='DONE', ts='1783000003.100001'))
        self.gateway.tick()
        for _ in range(100):
            if len(FakeSession.created) == 2 and sum(len(s.starts) for s in FakeSession.created) == 2:
                break
            time.sleep(0.005)
        self.assertEqual(len(self.gateway.running), 2)
        self.assertEqual(len(FakeSession.created), 2)
        self.assertEqual(sum(len(s.starts) for s in FakeSession.created), 2)
        FakeSession.gate.set()
        self.drain()
        self.assertEqual(len(FakeSession.created), 3)
        self.assertEqual(sum(len(s.starts) for s in FakeSession.created), 4)

    def test_current_message_survives_private_to_shared_reset(self):
        self.state.update_channel('TONE', 'CTWO', private_approved=1)
        self.gateway.qualify('TONE', 'CTWO')
        self.state.bind_thread('TONE', 'CTWO', 'old-private')
        self.gateway.ingest(self.event('<@UBOT> 新的公开问题', channel='CTWO'))
        self.api.membership['CTWO'].add('UOTHER')
        self.drain()
        source = self.state.inbox()[0]
        self.assertEqual(source['status'], 'completed')
        self.assertEqual(source['generation'], 1)
        self.assertEqual(FakeSession.created[0].starts[0][0], '新的公开问题')
        self.assertNotEqual(FakeSession.created[0].thread_id, 'old-private')

    def test_lost_background_start_response_reconciles_notice_id(self):
        from laowu.background import Outbox
        class LostEventSession(FakeSession):
            receipts = []
            def start_event(self, event):
                self.starts.append(('event', event['id']))
                raise TimeoutError('lost response')
            def history_receipt(self, ident, turn_id=None):
                self.receipts.append(ident)
                return {'status': 'completed', 'turn_id': 'already-completed', 'final_text': '后台结果'}
        self.gateway.session_factory = LostEventSession
        event = Outbox(self.store).publish('完成', event_key='lost-event')
        source = self.state.enqueue('TONE', 'CONE', 'notice:' + event['id'], '完成', origin_notice=event['id'])
        record = dict(source, _reconcile=False)
        self.state.update_input(source['id'], status='processing')
        self.gateway._execute(record)
        self.assertEqual(self.gateway._input(source['id'])['status'], 'uncertain')
        self.state.update_input(source['id'], status='processing')
        self.gateway._execute(dict(self.gateway._input(source['id']), _reconcile=True))
        self.assertEqual(LostEventSession.receipts, [event['id']])
        self.assertEqual(len(FakeSession.created[0].starts), 1)
        self.assertEqual(self.gateway._input(source['id'])['status'], 'completed')

    def test_static_question_modal_returns_selected_option(self):
        source = self.state.enqueue('TONE', 'CONE', '1783000000.100001', '请求')
        self.state.update_input(source['id'], status='processing')
        self.gateway.qualify('TONE', 'CONE')
        self.gateway.active[('TONE', 'CONE')] = source
        self.gateway._server_request(('TONE', 'CONE'), {'id': 7, 'method': 'item/tool/requestUserInput', 'params': {'questions': [{'id': 'choice', 'question': '哪种方式？', 'options': [{'label': '第一种'}]}]}})
        with self.store.connect() as db:
            approval = dict(db.execute('SELECT * FROM slack_approvals').fetchone())
        payload = {'type': 'view_submission', 'team': {'id': 'TONE'}, 'user': {'id': 'UOWNER'}, 'view': {
            'callback_id': 'laowu_question', 'private_metadata': json.dumps({'approval': approval['id'], 'epoch': self.gateway.epoch}),
            'state': {'values': {'choice': {'answer': {'selected_option': {'value': '第一种'}}}}}}}
        self.assertTrue(self.gateway.ingest(payload))
        with self.store.connect() as db:
            answer = json.loads(db.execute('SELECT payload FROM slack_approvals').fetchone()[0])['answer']
        self.assertEqual(answer, {'answers': {'choice': {'answers': ['第一种']}}})

    def test_external_reset_drops_cached_personal_session(self):
        row = self.gateway.qualify('TONE', 'DONE')
        session = self.gateway._get_session(row)
        self.state.reset('TONE', 'DONE')
        self.gateway.tick()
        self.assertTrue(session.closed)
        self.assertNotIn(('TONE', 'DONE'), self.gateway.sessions)

    def test_settled_transcript_content_purged_uncertain_retained(self):
        source = self.state.enqueue('TONE', 'CONE', '1783000000.100001', '不长期存聊天原文')
        sent = self.state.queue_output(source['id'], '已发送结果')
        self.state.update_input(source['id'], status='completed')
        self.state.update_output(sent['id'], status='sent', slack_ts='10.000001')
        uncertain = self.state.enqueue('TONE', 'CONE', '1783000001.100001', '待确定输入')
        out = self.state.queue_output(uncertain['id'], '待确定结果')
        self.state.update_output(out['id'], status='uncertain')
        self.gateway._settle_notifications()
        self.assertEqual(self.gateway._input(source['id'])['body'], '')
        self.assertEqual(self.gateway._input(uncertain['id'])['body'], '待确定输入')
        self.assertEqual(self.state.outputs('sent')[0]['body'], '')
        self.assertEqual(self.state.outputs('uncertain')[0]['body'], '待确定结果')

    def test_deferred_first_part_blocks_following_part(self):
        source = self.state.enqueue('TONE', 'CONE', '1783000000.100001', '长回复')
        first = self.state.queue_output(source['id'], '第一段', key='final:0')
        self.state.queue_output(source['id'], '第二段', key='final:1')
        self.state.update_output(first['id'], next_attempt=time.time() + 30)
        self.gateway.deliver_outputs()
        self.assertEqual(self.api.posts, [])
        self.state.update_output(first['id'], next_attempt=0)
        self.gateway.deliver_outputs()
        self.assertEqual([p['text'] for p in self.api.posts], ['第一段', '第二段'])

    def test_socket_ack_follows_real_bolt_commit_and_db_failure_no_ack(self):
        try:
            from slack_sdk import WebClient
        except ImportError:
            self.skipTest('官方Slack SDK未安装')
        self.api.client = WebClient(token='xoxb-unit-test', retry_handlers=[])
        self.gateway.build_bolt()
        sent = []
        client = SimpleNamespace(send_socket_mode_response=lambda response: sent.append((response.envelope_id, len(self.state.inbox()))))
        request = SimpleNamespace(type='events_api', envelope_id='envelope-1', payload=self.event('<@UBOT> durable'))
        self.gateway.socket_request(client, request)
        self.assertEqual(sent, [('envelope-1', 1)])
        original = self.state.enqueue
        def fail(*args, **kwargs):
            raise OSError('disk failed')
        self.state.enqueue = fail
        request.payload = self.event('<@UBOT> must not ack', ts='1783000002.100001')
        request.envelope_id = 'envelope-2'
        self.gateway.socket_request(client, request)
        self.assertEqual(len(sent), 1)
        self.state.enqueue = original

    def test_socket_ack_for_other_recipient_does_not_create_channel_or_work(self):
        try:
            from slack_sdk import WebClient
        except ImportError:
            self.skipTest('官方Slack SDK未安装')
        self.api.client = WebClient(token='xoxb-unit-test', retry_handlers=[])
        self.gateway.build_bolt()
        sent = []
        client = SimpleNamespace(send_socket_mode_response=lambda response: sent.append(response.envelope_id))
        before = self.routing_snapshot()
        for kind in ('message', 'app_mention'):
            request = SimpleNamespace(type='events_api', envelope_id='ignored-' + kind,
                                      payload=self.event('<@UOTHER> 请处理', kind=kind))
            self.gateway.socket_request(client, request)
        self.assertEqual(sent, ['ignored-message', 'ignored-app_mention'])
        self.assertEqual(self.routing_snapshot(), before)
        self.gateway.tick()
        self.assertEqual(FakeSession.created, [])
        self.assertEqual(self.api.posts, [])


class SlackAPIUnitTests(unittest.TestCase):
    def test_sdk_rate_limit_and_transport_errors_are_sanitized(self):
        class Client:
            def api_call(self, method, json):
                exc = RuntimeError('xoxb-secret-body')
                exc.response = SimpleNamespace(status_code=429, data={'error': 'ratelimited'}, headers={'Retry-After': ['17']})
                raise exc
        with self.assertRaises(SlackAPIError) as caught:
            SlackAPI(client=Client()).call('chat.postMessage')
        self.assertEqual(caught.exception.retry_after, 17)
        self.assertNotIn('secret', str(caught.exception))

    def test_long_chinese_code_is_bounded_and_balanced(self):
        text = '你好\n```python\n' + ('print("中文")\n' * 900) + '```\n结束'
        pieces = split_message(text)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= 3500 for p in pieces))
        self.assertTrue(all(p.count('```') % 2 == 0 for p in pieces))
        self.assertEqual(normalize_message('&lt;root&gt;维护&lt;/root&gt;', 'UBOT')[1], 'root')

    def test_slack_internal_error_is_uncertain_write(self):
        class Client:
            def api_call(self, method, json):
                return {'ok': False, 'error': 'internal_error'}
        with self.assertRaises(SlackAPIError) as caught:
            SlackAPI(client=Client()).call('chat.postMessage')
        self.assertTrue(caught.exception.uncertain)


if __name__ == '__main__':
    unittest.main()
