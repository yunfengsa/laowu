"""Configured budgets, cursor lifetimes and explicit Slack coverage."""
import json
from types import SimpleNamespace

from laowu.context import Context, body_size, dumps
from laowu.service import visible_tools
from laowu.slack_context import ContextReadError
from test_slack_context import ContextFixture


class BudgetReviewTests(ContextFixture):
    def policy(self, *, response=8192, page=2048, ttl=900, enabled=True):
        config = self.root / 'config'
        config.mkdir(exist_ok=True)
        (config / 'features.json').write_text(dumps({'mcp_budget': enabled}))
        (config / 'runtime-policy.json').write_text(dumps({
            'bootstrap_bytes': max(32768, response), 'response_bytes': response,
            'page_bytes': page, 'reference_ttl_seconds': ttl}))
        self.response_limit, self.page_limit = min(response, 16384), page

    def check_page(self, page, method='slack_history'):
        field = 'channels' if method == 'slack_channels' else 'messages'
        self.assertLessEqual(len(dumps(page[field]).encode()), self.page_limit)
        wire = dumps({'content': [{'type': 'text', 'text': dumps(page)}], 'isError': False}).encode()
        self.assertEqual(len(wire), body_size(page))
        self.assertLessEqual(len(wire), self.response_limit)
        self.assertLessEqual(len(dumps({'data': page}).encode()) + 1, 16384)
        self.assertEqual(json.loads(json.loads(wire)['content'][0]['text']), page)
        service = SimpleNamespace(store=self.state.store, access=self.access, worker_run=None,
                                  manager=SimpleNamespace(id='budget-review'))
        self.assertEqual(Context(service).present(method, {}, page), page)
        self.assertEqual(page['has_more'], page['next_cursor'] is not None)

    def restore(self, method='slack_history', **args):
        restored, seen, cursor, pages = {}, set(), None, 0
        while True:
            page = self.read(method, **args, **({'cursor': cursor} if cursor else {}))
            self.check_page(page, method)
            for message in page['messages']:
                previous = restored.get(message['ts'], '')
                self.assertEqual(message['text_offset'], len(previous))
                self.assertTrue(message['text'] or message['text_length'] == 0)
                restored[message['ts']] = previous + message['text']
                self.assertEqual(message['text_complete'], len(restored[message['ts']]) == message['text_length'])
            pages += 1
            self.assertLess(pages, 150, 'pagination must make progress')
            cursor = page['next_cursor']
            if cursor is None:
                return restored, pages
            self.assertNotIn(cursor, seen)
            seen.add(cursor)

    def test_8k_response_2k_page_chinese_two_messages_history_and_thread(self):
        self.policy()
        self.api.messages = [dict(user='UOWNER', ts='2.000001', text='中' * 2200),
                             dict(user='UBOT', ts='1.000001', text='文' * 500)]
        before = self.counts()
        for method, args in [('slack_history', {}), ('slack_thread', {'thread_ts': '1.000001'})]:
            with self.subTest(method=method):
                restored, pages = self.restore(method, **args)
                self.assertGreater(pages, 2)
                self.assertEqual(restored, {m['ts']: m['text'] for m in self.api.messages})
        self.assertEqual(self.counts(), before)

    def test_single_chinese_and_json_escapes_restore_without_body_cache(self):
        self.policy()
        original = '独立审阅正文尾标🌟\x00\b\f\r\t\\"\n' * 400
        self.api.messages[0]['text'] = original
        before = self.counts()
        restored, pages = self.restore()
        self.assertEqual(restored, {'2.000001': original})
        self.assertGreater(pages, 3)
        self.assertEqual(self.counts(), before)
        self.assertEqual(sum(m == 'auth.test' for m, _ in self.api.calls), pages)
        self.assertEqual(sum(m == 'conversations.history' for m, _ in self.api.calls), pages)
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn('独立审阅正文尾标'.encode(), path.read_bytes())

    def test_actual_signed_native_cursor_and_mcp_escaping_fit_response(self):
        self.policy(response=8192, page=8192)
        native = 'n' * 1024
        text = '中文\\"\x00\n' * 1300
        self.api.pages = {'': {'messages': [dict(ts='2.000001', text=text)],
                               'response_metadata': {'next_cursor': native}},
                          native: {'messages': [dict(ts='1.000001', text='最后一页')]}}
        for method, args in [('slack_history', {}), ('slack_thread', {'thread_ts': '1.000001'})]:
            with self.subTest(method=method):
                restored, pages = self.restore(method, **args)
                self.assertEqual(restored, {'2.000001': text, '1.000001': '最后一页'})
                self.assertGreater(pages, 3)

    def test_budget_changes_apply_to_existing_cursor_without_losing_text(self):
        self.policy(response=16384, page=8192)
        original = '配置变化中文\\"\n' * 1400
        self.api.messages[0]['text'] = original
        first = self.read()
        self.check_page(first)
        restored = first['messages'][0]['text']
        cursor = first['next_cursor']
        for limits in [(8192, 2048), (4096, 1024), (16384, 8192)]:
            self.assertIsNotNone(cursor)
            self.policy(response=limits[0], page=limits[1])
            page = self.read(cursor=cursor)
            self.check_page(page)
            self.assertEqual(page['messages'][0]['text_offset'], len(restored))
            restored += page['messages'][0]['text']
            cursor = page['next_cursor']
        for _ in range(100):
            if not cursor:
                break
            page = self.read(cursor=cursor)
            self.check_page(page)
            self.assertEqual(page['messages'][0]['text_offset'], len(restored))
            restored += page['messages'][0]['text']
            cursor = page['next_cursor']
        self.assertIsNone(cursor)
        self.assertEqual(restored, original)

    def test_proxy_16k_cap_survives_larger_policy_and_disabled_phase1(self):
        self.api.messages[0]['text'] = '硬上限\\"\x00' * 1600
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.policy(response=32768, page=32768, enabled=enabled)
                restored, pages = self.restore()
                self.assertEqual(restored, {'2.000001': self.api.messages[0]['text']})
                self.assertGreater(pages, 1)

    def test_context_guard_rejects_response_or_data_above_live_policy(self):
        self.policy(response=16384, page=8192)
        self.api.messages[0]['text'] = '中' * 2200
        page = self.read()
        self.assertGreater(body_size(page), 4096)
        service = SimpleNamespace(store=self.state.store, worker_run=None, manager=SimpleNamespace(id='review'))
        context = Context(service)
        self.policy(response=4096, page=4096)
        with self.assertRaisesRegex(ValueError, '数值策略预算'):
            context.present('slack_history', {}, page)
        self.policy(response=8192, page=2048)
        self.assertLess(body_size(page), 8192)
        with self.assertRaisesRegex(ValueError, '数值策略预算'):
            context.present('slack_history', {}, page)
        self.policy(response=8192, page=8192)
        self.assertEqual(context.present('slack_history', {}, page), page)

    def test_channels_are_paged_by_data_budget_and_revalidated(self):
        self.policy(response=4096, page=1024)
        for channel in ('C1', 'C2', 'C3'):
            self.api.info[channel] = dict(self.api.info['CA'], id=channel)
            self.api.members[channel] = {'UOWNER', 'UBOT'}
        for info in self.api.info.values():
            info['name'] = '频' * 80
        expected = ['CA', 'CB', 'C1', 'C2', 'C3']
        actual, cursor, pages = [], None, 0
        before = self.counts()
        while True:
            page = self.read('slack_channels', **({'cursor': cursor} if cursor else {}))
            self.check_page(page, 'slack_channels')
            self.assertTrue(page['channels'])
            actual.extend(c['channel_id'] for c in page['channels'])
            pages += 1
            self.assertLess(pages, 10)
            cursor = page['next_cursor']
            if not cursor:
                break
        self.assertEqual(actual, expected)
        self.assertGreater(pages, 1)
        self.assertEqual(self.counts(), before)
        first = self.read('slack_channels')
        self.api.members['CA'].remove('UOWNER')
        with self.assertRaises(PermissionError):
            self.read('slack_channels', cursor=first['next_cursor'])

    def test_metadata_budget_failure_is_explicit_without_empty_continuation(self):
        self.policy(response=1024, page=1024)
        self.api.pages[''] = {'messages': self.api.messages,
                              'response_metadata': {'next_cursor': 'n' * 1024}}
        for text in ('', '中' * 2200, '\x00\\"' * 1000):
            with self.subTest(text_length=len(text)):
                self.api.messages[0]['text'] = text
                with self.assertRaisesRegex(ContextReadError, 'response_budget:.*元数据'):
                    self.read()

    def test_empty_message_is_a_complete_item_not_zero_progress(self):
        self.policy()
        self.api.messages = [dict(ts='2.000001', text=''), dict(ts='1.000001', text='中' * 2200)]
        restored, pages = self.restore()
        self.assertEqual(restored, {'2.000001': '', '1.000001': '中' * 2200})
        self.assertGreater(pages, 1)
        # A final empty item can fit where metadata plus a cursor cannot.
        self.policy(response=1024, page=1024)
        self.api.messages = [dict(ts='2.000001', text='')]
        restored, pages = self.restore()
        self.assertEqual((restored, pages), ({'2.000001': ''}, 1))

    def test_30_second_ttl_does_not_slide_and_expires_at_31(self):
        self.policy(ttl=30)
        self.api.messages[0]['text'] = '中' * 8000
        first = self.read()['next_cursor']
        issued = self.reader._open(first)
        self.assertEqual(issued['expires'] - issued['issued'], 30)
        self.clock[0] += 20
        second = self.read(cursor=first)['next_cursor']
        self.assertEqual(self.reader._open(second)['expires'], issued['expires'])
        self.clock[0] += 11
        calls = len(self.api.calls)
        for cursor in (first, second):
            with self.assertRaisesRegex(ContextReadError, 'cursor_invalid'):
                self.read(cursor=cursor)
        self.assertEqual(len(self.api.calls), calls)

    def test_shortened_policy_expires_older_cursor_from_original_issue_time(self):
        self.policy(ttl=900)
        self.api.messages[0]['text'] = '中' * 8000
        cursor = self.read()['next_cursor']
        self.clock[0] += 20
        self.policy(ttl=30)
        second = self.read(cursor=cursor)['next_cursor']
        self.clock[0] += 11
        for value in (cursor, second):
            with self.assertRaisesRegex(ContextReadError, 'cursor_invalid'):
                self.read(cursor=value)

    def test_larger_ttl_cannot_extend_old_expiry_or_900_second_cap(self):
        self.policy(ttl=30)
        self.api.messages[0]['text'] = '中' * 8000
        cursor = self.read()['next_cursor']
        self.policy(ttl=1800)
        self.clock[0] += 31
        with self.assertRaisesRegex(ContextReadError, 'cursor_invalid'):
            self.read(cursor=cursor)
        cursor = self.read()['next_cursor']
        state = self.reader._open(cursor)
        self.assertEqual(state['expires'] - state['issued'], 900)
        self.clock[0] += 901
        with self.assertRaisesRegex(ContextReadError, 'cursor_invalid'):
            self.read(cursor=cursor)


class CoverageReviewTests(ContextFixture):
    def test_history_top_level_and_thread_scope_remain_explicit_at_end(self):
        self.api.messages[0]['reply_count'] = 3
        page = self.read()
        self.assertFalse(page['has_more'])
        self.assertEqual(page['coverage']['scope'], 'channel_top_level')
        self.assertEqual(page['coverage']['thread_replies'], 'not_read')
        self.assertEqual(page['coverage']['read_replies_with'], 'slack_thread(thread_ts=root ts)')
        self.assertEqual(page['messages'][0]['reply_count'], 3)
        self.assertTrue(page['messages'][0]['has_replies'])
        root_ts = page['messages'][0]['ts']
        thread = self.read('slack_thread', thread_ts=root_ts)
        self.assertEqual(thread['coverage']['scope'], 'thread')
        self.assertEqual(thread['coverage']['thread_ts'], root_ts)
        self.assertEqual(thread['coverage']['thread_replies'], 'included')
        for value in (page, thread):
            for field in ('attachments', 'files', 'blocks'):
                self.assertEqual(value['coverage'][field], 'not_read')
        single = self.read('slack_thread', thread_ts=root_ts, message_ts=root_ts)
        self.assertEqual(single['coverage']['message_ts'], root_ts)

    def test_reply_metadata_bounded_and_tool_descriptions_explain_coverage(self):
        for raw, expected in [(3, 3), (2**100, 2**31 - 1), (-3, 0), (True, 0), ('3', 0), (None, 0)]:
            with self.subTest(raw=raw):
                self.api.messages[0].update(reply_count=raw, replies=[{'secret': 'no'}],
                                            reply_users=['hidden'], blocks=[{'text': 'no'}])
                message = self.read()['messages'][0]
                self.assertEqual(message['reply_count'], expected)
                self.assertEqual(message['has_replies'], expected > 0)
                self.assertTrue({'replies', 'reply_users', 'blocks'}.isdisjoint(message))
        descriptions = {name: description for name, description, _ in visible_tools(None, self.access)}
        self.assertIn('顶层', descriptions['slack_history'])
        self.assertIn('has_more=false', descriptions['slack_history'])
        self.assertIn('slack_thread(thread_ts=根消息ts)', descriptions['slack_history'])
        self.assertIn('根消息及回复', descriptions['slack_thread'])
        for method in ('slack_history', 'slack_thread'):
            self.assertIn('附件/文件/blocks不展开', descriptions[method])
