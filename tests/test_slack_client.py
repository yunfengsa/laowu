"""Real official SDK request serialization; only the final network hop is fake."""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from laowu.slack_client import SlackAPI, SlackAPIError
from laowu.slack_gateway import SlackGateway, _SDK_LOG
from laowu.slack_state import SlackState
from laowu.store import Store


class SlackSDKRequestTests(unittest.TestCase):
    def setUp(self):
        try:
            from slack_sdk import WebClient
        except ImportError:
            self.skipTest('官方Slack SDK未安装')
        self.client = WebClient(token='xoxb-offline-test', retry_handlers=[], logger=_SDK_LOG)
        self.api = SlackAPI(client=self.client)
        self.requests = []
        self.responses = []
        transport = patch.object(self.client, '_perform_urllib_http_request_internal',
                                 side_effect=self.respond)
        self.transport = transport.start()
        self.addCleanup(transport.stop)
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(target, side_effect=AssertionError('network forbidden in offline regression'))
            blocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(blocked.assert_not_called)

    def respond(self, url, req):
        self.requests.append(req)
        body, status, headers = self.responses.pop(0) if self.responses else ({'ok': True}, 200, {})
        return {'body': json.dumps(body), 'status': status, 'headers': headers}

    def reply(self, **data):
        self.responses.append(({'ok': True, **data}, 200, {}))

    def form(self, req, method):
        self.assertEqual(urlsplit(req.full_url).path, '/api/' + method)
        # SDK 3.46.0 sync transport deliberately sends GET/params as POST/form.
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.get_header('Content-type'), 'application/x-www-form-urlencoded')
        self.assertEqual(req.get_header('Authorization'), 'Bearer xoxb-offline-test')
        self.assertEqual(urlsplit(req.full_url).query, '')
        return parse_qs((req.data or b'').decode(), keep_blank_values=True)

    def test_info_matches_native_sdk_request(self):
        info = {'id': 'CONE', 'is_private': False}
        self.reply(channel=info)
        self.assertEqual(self.api.info('CONE'), info)
        actual = self.form(self.requests[-1], 'conversations.info')
        self.reply(channel=info)
        self.client.conversations_info(channel='CONE')
        self.assertEqual(actual, self.form(self.requests[-1], 'conversations.info'))
        self.assertEqual(actual, {'channel': ['CONE']})

    def test_members_pagination_preserves_query_characters(self):
        cursor = 'next+/= with space'
        self.reply(members=['UOWNER'], response_metadata={'next_cursor': cursor})
        self.reply(members=['UBOT'], response_metadata={'next_cursor': ''})
        self.assertEqual(self.api.members('CONE'), {'UOWNER', 'UBOT'})
        first = self.form(self.requests[0], 'conversations.members')
        second = self.form(self.requests[1], 'conversations.members')
        self.assertEqual(first, {'channel': ['CONE'], 'limit': ['200']})
        self.assertEqual(second, {**first, 'cursor': [cursor]})
        self.reply(members=[])
        self.client.conversations_members(channel='CONE', limit=200, cursor=cursor)
        self.assertEqual(second, self.form(self.requests[-1], 'conversations.members'))

    def test_history_and_replies_match_native_sdk_with_pagination_and_booleans(self):
        for thread_ts in (None, '1783000000.100001'):
            with self.subTest(thread_ts=thread_ts):
                method = 'conversations.replies' if thread_ts else 'conversations.history'
                cursor = 'cursor+/=' + str(thread_ts)
                messages = [{'ts': '1783000001.000001'}, {'ts': '1783000002.000001'}]
                self.reply(messages=messages[:1], response_metadata={'next_cursor': cursor}, has_more=True)
                self.reply(messages=messages[1:], response_metadata={'next_cursor': ''})
                start = len(self.requests)
                self.assertEqual(list(self.api.history('CONE', oldest=1783000000.1, thread_ts=thread_ts)), messages)
                first = self.form(self.requests[start], method)
                second = self.form(self.requests[start + 1], method)
                expected = {'channel': ['CONE'], 'oldest': ['1783000000.1'], 'inclusive': ['1'],
                            'include_all_metadata': ['1'], 'limit': ['100']}
                params = {'channel': 'CONE', 'oldest': '1783000000.1', 'inclusive': True,
                          'include_all_metadata': True, 'limit': 100, 'cursor': cursor}
                if thread_ts:
                    expected['ts'] = [thread_ts]
                    params['ts'] = thread_ts
                self.assertEqual(first, expected)
                self.assertEqual(second, {**expected, 'cursor': [cursor]})
                self.reply(messages=[])
                getattr(self.client, method.replace('.', '_'))(**params)
                self.assertEqual(second, self.form(self.requests[-1], method))

    def test_json_writes_keep_nested_blocks_metadata_and_boolean_values(self):
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': '合成回复'}}]
        self.reply(ts='1.000001')
        self.api.post('CONE', '合成回复', thread_ts='1.0', key='delivery-1', blocks=blocks)
        req = self.requests[-1]
        self.assertEqual(urlsplit(req.full_url).path, '/api/chat.postMessage')
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.get_header('Content-type'), 'application/json;charset=utf-8')
        self.assertEqual(json.loads(req.data), {
            'channel': 'CONE', 'text': '合成回复', 'thread_ts': '1.0',
            'unfurl_links': False, 'unfurl_media': False, 'blocks': blocks,
            'metadata': {'event_type': 'laowu_delivery', 'event_payload': {'key': 'delivery-1'}}})
        self.api.status('CONE', '1.0', 'processing', 'UOWNER')
        self.assertEqual(json.loads(self.requests[-1].data), {
            'channel_id': 'CONE', 'thread_ts': '1.0', 'status': 'processing', 'initiator_user_id': 'UOWNER'})
        view = {'type': 'modal', 'title': {'type': 'plain_text', 'text': '问题'}, 'blocks': blocks}
        self.api.call('views.open', trigger_id='offline-trigger', view=view)
        self.assertEqual(json.loads(self.requests[-1].data), {'trigger_id': 'offline-trigger', 'view': view})

    def test_auth_test_uses_native_sdk_form_encoding(self):
        # An argument is intentional: empty auth.test JSON hid the original bug.
        self.api.call('auth.test', team_id='TONE')
        actual = self.form(self.requests[-1], 'auth.test')
        self.client.auth_test(team_id='TONE')
        self.assertEqual(actual, self.form(self.requests[-1], 'auth.test'))
        self.assertEqual(actual, {'team_id': ['TONE']})

    def test_sdk_rejection_is_sanitized_without_alternate_encoding_retry(self):
        self.responses.append(({'ok': False, 'error': 'invalid_arguments',
                                'response_metadata': {'messages': ['private request body']}}, 200, {}))
        with self.assertRaises(SlackAPIError) as caught:
            self.api.info('CONE')
        self.assertEqual(caught.exception.code, 'invalid_arguments')
        self.assertFalse(caught.exception.uncertain)
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    def test_sdk_rate_limit_is_known_rejection_and_never_retried(self):
        self.responses.append(({'ok': False, 'error': 'ratelimited'}, 429, {'Retry-After': '17'}))
        with self.assertRaises(SlackAPIError) as caught:
            self.api.post('CONE', '合成回复')
        self.assertEqual(caught.exception.retry_after, 17)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(self.requests), 1)

    def test_sdk_write_timeout_and_server_error_are_uncertain_without_retries(self):
        for failure in ('timeout', 'server'):
            with self.subTest(failure=failure):
                self.requests.clear()
                if failure == 'timeout':
                    self.transport.side_effect = TimeoutError('private request body')
                else:
                    self.transport.side_effect = self.respond
                    self.responses.append(({'ok': False, 'error': 'internal_error'}, 500, {}))
                self.transport.reset_mock()
                with self.assertRaises(SlackAPIError) as caught:
                    self.api.post('CONE', '合成回复')
                self.assertTrue(caught.exception.uncertain)
                self.assertNotIn('private', str(caught.exception))
                self.assertEqual(self.transport.call_count, 1)

    def gateway(self):
        temporary = tempfile.TemporaryDirectory(prefix='老五Slack请求构造-')
        self.addCleanup(temporary.cleanup)
        state = SlackState(Store(Path(temporary.name)))
        state.set_config({'team_id': 'TONE', 'owner_user_id': 'UOWNER',
                          'bot_user_id': 'UBOT', 'enabled': True})
        gateway = SlackGateway(state, api=self.api, session_factory=Mock())
        self.addCleanup(gateway.close)
        return gateway, state

    def test_gateway_qualifies_channel_and_owner_dm_through_real_sdk(self):
        gateway, state = self.gateway()
        self.reply(channel={'id': 'CONE', 'is_private': False})
        self.reply(members=['UOWNER', 'UBOT'])
        channel = gateway.qualify('TONE', 'CONE')
        self.assertEqual((channel['eligible'], channel['personal']), (1, 0))
        self.assertEqual(self.form(self.requests[0], 'conversations.info'), {'channel': ['CONE']})
        self.form(self.requests[1], 'conversations.members')
        self.reply(channel={'id': 'DONE', 'is_im': True, 'user': 'UOWNER'})
        dm = gateway.qualify('TONE', 'DONE')
        self.assertEqual((dm['eligible'], dm['personal']), (1, 1))
        self.assertEqual(self.form(self.requests[-1], 'conversations.info'), {'channel': ['DONE']})
        self.assertEqual(len(self.requests), 3)
        gateway.session_factory.assert_not_called()

    def fail_qualification(self, gateway, state, ts='1783000000.100001'):
        record = state.enqueue('TONE', 'CONE', ts, '合成任务', reply_ts=ts,
                               event_id='event-' + ts)
        state.update_input(record['id'], status='processing')
        self.responses.append(({'ok': False, 'error': 'invalid_arguments'}, 200, {}))
        gateway._execute(record)
        failed = gateway._input(record['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['error'], 'invalid_arguments')
        self.assertIsNone(failed['thread_id'])
        self.assertIsNone(failed['turn_id'])
        gateway.session_factory.assert_not_called()
        return failed

    def test_qualification_failure_never_submits_model_or_requeues_on_restart(self):
        gateway, state = self.gateway()
        record = self.fail_qualification(gateway, state)
        self.assertEqual(state.channel('TONE', 'CONE')['eligible'], 0)
        self.assertEqual(state.outputs()[0]['status'], 'pending')
        state.recover()
        self.assertEqual(gateway._input(record['id'])['status'], 'failed')
        with patch.object(gateway, 'deliver_outputs'), patch.object(gateway.executor, 'submit') as submit:
            gateway.tick()
        submit.assert_not_called()
        gateway.session_factory.assert_not_called()

    def recovery_recipe(self):
        doc = (Path(__file__).resolve().parents[1] / 'docs/slack-recovery.md').read_text()
        section = doc.split('<!-- slack-qualification-recovery:start -->', 1)[1]
        code = section.split('```python\n', 1)[1].split('```', 1)[0]
        namespace = {}
        exec(compile(code, 'docs/slack-recovery.md:recovery', 'exec'), namespace)
        return namespace['requeue_verified_qualification_failures']

    def requalify(self, gateway):
        self.reply(channel={'id': 'CONE', 'is_private': False})
        self.reply(members=['UOWNER', 'UBOT'])
        self.assertTrue(gateway.qualify('TONE', 'CONE')['eligible'])

    def test_documented_recovery_submits_original_input_once_without_stale_failure_post(self):
        gateway, state = self.gateway()
        original = self.fail_qualification(gateway, state)
        stale = state.outputs()[0]
        self.requalify(gateway)
        recover = self.recovery_recipe()
        self.assertEqual(recover(state.store, [original['id']]), (original['id'],))
        recovered = gateway._input(original['id'])
        self.assertEqual((recovered['status'], recovered['error']), ('pending', None))
        for field in ('id', 'event_id', 'message_ts', 'reply_ts', 'generation'):
            self.assertEqual(recovered[field], original[field])
        duplicate = state.enqueue('TONE', 'CONE', original['message_ts'], '合成任务',
                                  event_id=original['event_id'])
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['id'], original['id'])
        with self.assertRaises(ValueError):
            recover(state.store, [original['id']])

        session = gateway.session_factory.return_value
        session.start.return_value = 'synthetic-thread'
        session.start_turn.return_value = 'synthetic-turn'
        session.history_receipt.return_value = {'status': 'completed', 'turn_id': 'synthetic-turn',
                                                'final_text': '合成结果'}
        self.reply(channel={'id': 'CONE', 'is_private': False})
        self.reply(members=['UOWNER', 'UBOT'])
        state.update_input(original['id'], status='processing')
        with patch.object(gateway.closed, 'wait', return_value=False):
            gateway._execute(recovered)
        session.start_turn.assert_called_once()
        self.assertEqual(session.start_turn.call_args.args, ('合成任务', original['id']))
        self.assertEqual(gateway._input(original['id'])['status'], 'completed')
        self.reply(channel={'id': 'CONE', 'is_private': False})
        self.reply(members=['UOWNER', 'UBOT'])
        self.reply(ts='1783000010.000001')
        gateway.deliver_outputs()
        gateway.deliver_outputs()
        outputs = state.outputs()
        self.assertFalse(any(o['id'] == stale['id'] for o in outputs))
        self.assertEqual(sum(o['status'] == 'sent' for o in outputs), 1)
        posts = [req for req in self.requests if req.full_url.endswith('/chat.postMessage')]
        self.assertEqual(len(posts), 1)
        self.assertEqual(json.loads(posts[0].data)['text'], '合成结果')

    def test_documented_recovery_preserves_a_fresh_failure_diagnostic(self):
        gateway, state = self.gateway()
        original = self.fail_qualification(gateway, state)
        stale_id = state.outputs()[0]['id']
        self.requalify(gateway)
        self.recovery_recipe()(state.store, [original['id']])
        recovered = gateway._input(original['id'])
        state.update_input(original['id'], status='processing')
        self.responses.append(({'ok': False, 'error': 'missing_scope'}, 200, {}))
        gateway._execute(recovered)
        self.assertEqual(gateway._input(original['id'])['error'], 'missing_scope')
        outputs = state.outputs()
        self.assertEqual(len(outputs), 1)
        self.assertNotEqual(outputs[0]['id'], stale_id)
        self.assertEqual(outputs[0]['status'], 'pending')
        self.assertIn('missing_scope', outputs[0]['body'])
        gateway.session_factory.assert_not_called()

    def test_documented_recovery_rejects_submission_delivery_and_permission_uncertainty(self):
        recover = self.recovery_recipe()
        cases = ('turn', 'thread', 'uncertain', 'wrong_error', 'ineligible', 'generation',
                 'sending', 'uncertain_output', 'sent', 'slack_ts', 'extra_output', 'grant',
                 'approval', 'origin_notice')
        for case in cases:
            with self.subTest(case=case):
                gateway, state = self.gateway()
                record = self.fail_qualification(gateway, state)
                self.requalify(gateway)
                output = state.outputs()[0]
                if case in ('turn', 'thread'):
                    state.update_input(record['id'], **{case + '_id': 'possibly-submitted'})
                elif case == 'uncertain':
                    state.update_input(record['id'], status='uncertain')
                elif case == 'wrong_error':
                    state.update_input(record['id'], error='other_error')
                elif case == 'ineligible':
                    state.update_channel('TONE', 'CONE', eligible=0)
                elif case == 'generation':
                    state.update_input(record['id'], generation=record['generation'] + 1)
                elif case in ('sending', 'uncertain_output', 'sent'):
                    state.update_output(output['id'], status=case.replace('_output', ''))
                elif case == 'slack_ts':
                    state.update_output(output['id'], slack_ts='already-posted')
                elif case == 'extra_output':
                    state.queue_output(record['id'], '其他结果', key='final:0')
                else:
                    with state.store.transaction() as db:
                        if case == 'grant':
                            db.execute('INSERT INTO slack_task_grants VALUES(?,?,?,?,?)',
                                       ('task-synthetic', 'slack:TONE:CONE:0', 'shared', None, record['id']))
                        elif case == 'approval':
                            db.execute('INSERT INTO slack_approvals VALUES(?,?,?,?,?,?,?,?)',
                                       ('approval-synthetic', record['id'], 'request-synthetic', 'epoch',
                                        '{}', 'pending', 0, 'synthetic',))
                        else:
                            db.execute('UPDATE slack_inbox SET origin_notice=? WHERE id=?',
                                       ('notice-synthetic', record['id']))
                before = gateway._input(record['id'])
                old_outputs = state.outputs()
                with self.assertRaises(ValueError):
                    recover(state.store, [record['id']])
                self.assertEqual(gateway._input(record['id']), before)
                self.assertEqual(state.outputs(), old_outputs)

    def test_documented_batch_recovery_is_atomic(self):
        gateway, state = self.gateway()
        first = self.fail_qualification(gateway, state)
        second = self.fail_qualification(gateway, state, ts='1783000001.100001')
        self.requalify(gateway)
        second_output = next(o for o in state.outputs() if o['input_id'] == second['id'])
        state.update_output(second_output['id'], status='uncertain')
        outputs = state.outputs()
        with self.assertRaises(ValueError):
            self.recovery_recipe()(state.store, [first['id'], second['id']])
        self.assertEqual(gateway._input(first['id'])['status'], 'failed')
        self.assertEqual(gateway._input(second['id'])['status'], 'failed')
        self.assertEqual(state.outputs(), outputs)


if __name__ == '__main__':
    unittest.main()
