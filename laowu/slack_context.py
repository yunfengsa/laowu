"""Gateway-owned, revocable Slack reads. No message cache or credential export."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from .access import SlackAccess
from .context import body_size, digest, dumps
from .policy import settings
from .retries import ReadRetry, RetryPending, RetrySuspended, _READ_DEADLINE, remaining
from .slack_client import SlackAPIError

TOOLS = frozenset({'slack_channels', 'slack_history', 'slack_thread'})
MAX_RESPONSE = 16 * 1024
TTL = 900
REQUEST_SECONDS = 25
_ID = re.compile(r'[CDG][A-Z0-9]{1,63}\Z')
_TS = re.compile(r'[0-9]{1,16}\.[0-9]{1,9}\Z')
_TOKEN = re.compile(r'\b(?:xox[baprs]-|xapp-)[A-Za-z0-9-]+')


class ContextReadError(RuntimeError):
    pass


class SlackContextReader:
    def __init__(self, state, api, *, clock=time.time):
        self.state, self.store, self.api, self.clock = state, state.store, api, clock
        self.key = secrets.token_bytes(32)
        self.reads = ReadRetry(self.store, clock)

    def _seal(self, value):
        body = base64.urlsafe_b64encode(dumps(value).encode()).decode().rstrip('=')
        signature = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()
        return body + '.' + signature

    def _open(self, token):
        try:
            body, signature = token.split('.')
            if len(token) > 6000 or not hmac.compare_digest(
                    signature, hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()):
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
            now = self.clock()
            ttl = min(settings(self.store)['reference_ttl_seconds'], TTL)
            if not value['issued'] <= now < min(value['expires'], value['issued'] + ttl):
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError):
            raise ContextReadError('cursor_invalid: 游标过期、篡改或网关已重启，请重新读取') from None

    def _get(self, method, **params):
        remaining(8)
        try:
            value = self.api.read_get(method, **params)
        except SlackAPIError as exc:
            if exc.code in {'not_in_channel', 'channel_not_found', 'access_denied'}:
                raise PermissionError('channel_unavailable: 目标不可读，请核对本人及 bot 成员资格') from None
            raise
        remaining(8)
        return value

    def _channel(self, ident, config):
        info = self._get('conversations.info', channel=ident).get('channel', {})
        if (info.get('id') != ident or info.get('is_archived') or info.get('is_mpim')
                or info.get('is_ext_shared') or info.get('is_org_shared') or info.get('is_shared')
                or any(info.get(k) and info[k] != config['team_id'] for k in ('team_id', 'context_team_id'))
                or any(t != config['team_id'] for t in info.get('shared_team_ids', []))):
            raise PermissionError('channel_unavailable: 频道不可读；首版不支持跨工作区/共享频道或多人私信')
        # The token is authenticated as this bot. An owner's one-to-one IM is
        # its membership proof; conversations.members is not available for IMs.
        if info.get('is_im'):
            if info.get('user') != config['owner_user_id'] or info.get('is_user_deleted'):
                raise PermissionError('channel_unavailable: 仅允许已绑定本人与 bot 的私信')
            members = {config['owner_user_id'], config['bot_user_id']}
        else:
            if info.get('is_member') is False:
                raise PermissionError('channel_unavailable: bot 未加入频道')
            members, cursor = set(), ''
            for _ in range(10):
                page = self._get('conversations.members', channel=ident, limit=200,
                                 **({'cursor': cursor} if cursor else {}))
                members.update(page.get('members', []))
                cursor = self._next(page)
                if not cursor:
                    break
            else:
                raise ContextReadError('membership_budget: 成员超过核验预算，未读取内容')
            if not {config['owner_user_id'], config['bot_user_id']} <= members:
                raise PermissionError('channel_unavailable: 本人和 bot 必须均已加入频道；不会自动加入')
        return info, members

    def _source(self, access, config):
        row = access.validate()
        if (not config.get('enabled') or row['team_id'] != config.get('team_id')
                or self.state.config() != config):
            raise PermissionError('source_revoked: 工作区或绑定已改变')
        info, members = self._channel(row['channel_id'], config)
        if access.audience == 'personal' and not (info.get('is_im') or
                (info.get('is_private') and members == {config['owner_user_id'], config['bot_user_id']})):
            raise PermissionError('source_revoked: 私人来源已不满足独占边界，请重建主会话')
        if self.state.config() != config:
            raise PermissionError('source_revoked: 读取期间工作区或绑定配置改变')
        access.validate()
        return row, info

    def _target(self, channel, source, source_info, config):
        info, _ = self._channel(channel, config)
        if channel != source['channel_id']:
            personal_dm = bool(source['personal'] and source_info.get('is_im'))
            public = bool(info.get('is_channel') and not info.get('is_private') and not info.get('is_im'))
            if not public and not personal_dm:
                raise PermissionError('audience_boundary: 此来源仅可跨读 public 频道；私有频道可读自身')
        return info

    def _query(self, method, args, access):
        saved = self._open(args['cursor']) if args.get('cursor') else None
        if saved:
            if saved['principal'] != access.token_hash or saved['source'] != access.conversation_id or saved['method'] != method:
                raise PermissionError('cursor_binding: 游标不属于当前身份、来源或工具')
            query = saved['query']
            if any(query.get(k) != v for k, v in args.items() if k != 'cursor'):
                raise PermissionError('cursor_query: 续读不能更换目标、线程、范围或 limit')
            return saved
        query = dict(args)
        query.pop('cursor', None)
        if method != 'slack_channels':
            query.setdefault('channel_id', access.validate()['channel_id'])
            query.setdefault('limit', 10)
            if not _ID.fullmatch(query['channel_id']):
                raise ValueError('channel_id 须为 Slack 频道 ID')
            for k in ('thread_ts', 'oldest', 'latest', 'message_ts'):
                if k in query and not _TS.fullmatch(query[k]):
                    raise ValueError(k + ' 须为 Slack 时间戳')
            if 'oldest' in query and 'latest' in query and float(query['oldest']) > float(query['latest']):
                raise ValueError('oldest 不能晚于 latest')
            if 'message_ts' in query:
                if 'oldest' in query or 'latest' in query or ('limit' in args and args['limit'] != 1):
                    raise ValueError('单条片段使用 message_ts，不能混用时间范围，limit 须为 1')
                query['limit'] = 1
                if query.get('text_offset', 0) and 'text_version' not in query:
                    raise ValueError('从非零 offset 续读须提供原 text_version')
                if 'text_version' in query and not re.fullmatch(r'[0-9a-f]{64}', query['text_version']):
                    raise ValueError('text_version 须为已返回的正文版本')
            elif 'text_offset' in query or 'text_version' in query:
                raise ValueError('text_offset/text_version 仅适用于 message_ts 单条读取')
        position = {} if method == 'slack_channels' else {'latest': query.get('latest', f'{self.clock():.6f}')}
        if 'message_ts' in query:
            position = {'oldest': query['message_ts'], 'latest': query['message_ts']}
        issued = self.clock()
        return {'principal': access.token_hash, 'source': access.conversation_id, 'method': method,
                'query': query, 'issued': issued,
                'expires': issued + min(settings(self.store)['reference_ttl_seconds'], TTL),
                'position': position, 'index': 0, 'offset': query.get('text_offset', 0), 'version': None}

    def read(self, token, method, arguments):
        from .service import validate_arguments
        if method not in TOOLS:
            raise PermissionError('read_method: 只允许 Slack 上下文读取工具')
        validate_arguments(method, arguments)
        access = SlackAccess.from_token(self.store, token)
        state = self._query(method, arguments, access)
        # Opaque key contains no Slack text/IDs/token; an identical page shares
        # its persistent failure budget. Reads are retried only on another call.
        retry_key = 'slack-context:' + digest([access.token_hash, method, arguments])
        failure = []

        def run():
            deadline = self.clock() + min(REQUEST_SECONDS, remaining(REQUEST_SECONDS))
            mark = _READ_DEADLINE.set((self.clock, deadline))
            try:
                return self._read(access, state)
            except Exception as exc:
                failure.append(exc)
                # Unknown SDK/transport errors are not transient evidence.
                if isinstance(exc, SlackAPIError) and exc.code == 'transport_uncertain':
                    raise ContextReadError('transport_unknown: 未知传输错误，需检查网关') from None
                raise
            finally:
                _READ_DEADLINE.reset(mark)
        try:
            return self.reads.call(retry_key, 'slack.context', run)
        except (RetryPending, RetrySuspended) as exc:
            details = ''
            if failure:
                last = failure[-1]
                if isinstance(last, SlackAPIError):
                    details = ' Slack=' + last.code + '。' + self._api_hint(last.code)
                elif isinstance(last, (PermissionError, ValueError, ContextReadError)):
                    details = ' ' + str(last)
            with self.store.connect() as db:
                row = db.execute('SELECT next_attempt FROM read_retries WHERE id=?', (retry_key,)).fetchone()
            retry_at = row[0] if row else None
            raise ContextReadError(str(exc) + details + f' read_key={retry_key}; retry_at={retry_at}。'
                                   '等待时间后用相同参数续读；挂起时由本机 reads retry 显式恢复。未取得历史内容。') from None
        except SlackAPIError as exc:
            raise ContextReadError('Slack=' + exc.code + '。' + self._api_hint(exc.code)) from None

    @staticmethod
    def _api_hint(code):
        if code in ('missing_scope', 'not_allowed_token_type'):
            return '请管理员核对现有 bot 的 channels/groups/im history/read 授权与安装；不自动要求 user token。'
        if code in ('not_in_channel', 'channel_not_found', 'access_denied'):
            return '请核实本人及 bot 的成员资格；不会自动加入。'
        if code in ('invalid_auth', 'token_revoked', 'account_inactive'):
            return '请在本机核对应用授权，勿在聊天中发送令牌。'
        if code == 'ratelimited':
            return '已遵守 Retry-After，等待后继续；不会立即循环重试。'
        if code == 'context_page_too_large':
            return 'API 页超过 2MiB，请减少 limit 后重读；未静默截断。'
        return '读取未完成，请核对网关/API 状态后重试。'

    def _read(self, access, state):
        config = self.state.config()
        auth = self._get('auth.test')
        if auth.get('team_id') != config.get('team_id') or auth.get('user_id') != config.get('bot_user_id'):
            raise PermissionError('workspace_mismatch: bot 与工作区绑定不符')
        source, source_info = self._source(access, config)
        with self.store.connect() as db:
            row = db.execute("SELECT reply_ts FROM slack_inbox WHERE team_id=? AND channel_id=? AND generation=? AND status IN ('processing','running') ORDER BY created_at DESC LIMIT 1",
                             (source['team_id'], source['channel_id'], source['generation'])).fetchone()
        result = {'version': 1, 'source': {'channel_id': source['channel_id'], 'reply_thread_ts': row[0] if row else None},
                  'trust': 'untrusted_reference_only', 'next_cursor': None, 'has_more': False}
        method, query = state['method'], state['query']
        if method == 'slack_channels':
            # Shared sources never request private discovery. Even eligible
            # names are revalidated before disclosure; no qualification writes.
            types = 'public_channel,private_channel,im' if source['personal'] and source_info.get('is_im') else 'public_channel'
            page = self._get('conversations.list', types=types, exclude_archived=True, limit=5, **state['position'])
            channels = []
            for candidate in page.get('channels', []):
                ident = candidate.get('id', '')
                if not _ID.fullmatch(ident):
                    continue
                try:
                    info = self._target(ident, source, source_info, config)
                except PermissionError:
                    continue
                channels.append({'channel_id': ident, 'name': info.get('name', '')[:80],
                                 'is_private': bool(info.get('is_private') or info.get('is_im'))})
            # Drop a candidate if its membership/boundary changed during listing.
            readable = []
            for channel in channels:
                try:
                    self._target(channel['channel_id'], source, source_info, config)
                except PermissionError:
                    continue
                readable.append(channel)
            cursor = self._next(page)
            result['order'] = 'Slack API channel order; filtered pages may be empty'
            following = ({**state, 'position': {'cursor': cursor}, 'index': 0, 'version': None}
                         if cursor else None)
            self._pack_channels(result, readable, state, following)
        else:
            target = query['channel_id']
            self._target(target, source, source_info, config)
            params = {'channel': target, 'limit': query['limit'], 'inclusive': 'message_ts' in query,
                      **{k: query[k] for k in ('oldest', 'latest') if k in query}, **state['position']}
            if method == 'slack_thread':
                params['ts'] = query['thread_ts']
            page = self._get('conversations.replies' if method == 'slack_thread' else 'conversations.history', **params)
            messages = [self._message(m) for m in page.get('messages', [])]
            if 'message_ts' in query:
                messages = [m for m in messages if m['ts'] == query['message_ts']]
                if len(messages) != 1:
                    raise ContextReadError('message_missing: 未找到指定消息，请重新读取频道或线程')
                message = messages[0]
                if query.get('text_version', message['text_version']) != message['text_version']:
                    raise ContextReadError('page_changed: 消息版本改变，请从 offset 0 重读')
                if state['offset'] > len(message['text']):
                    raise ValueError('text_offset 超过消息完整长度')
            version = digest(messages)
            if state['version'] and state['version'] != version:
                raise ContextReadError('page_changed: 消息已编辑、删除或页已变化；请重新读取，不拼接旧片段')
            coverage = {'scope': 'thread' if method == 'slack_thread' else 'channel_top_level',
                        'thread_replies': 'included' if method == 'slack_thread' else 'not_read',
                        'attachments': 'not_read', 'files': 'not_read', 'blocks': 'not_read'}
            if method == 'slack_thread':
                coverage['thread_ts'] = query['thread_ts']
            else:
                coverage['read_replies_with'] = 'slack_thread(thread_ts=root ts)'
            if 'message_ts' in query:
                coverage['message_ts'] = query['message_ts']
            result.update(channel_id=target, messages=[], order='ascending_ts' if method == 'slack_thread' else 'descending_ts',
                          text_offset_unit='unicode_codepoints', attachments='not_read', coverage=coverage)
            following = None
            if 'message_ts' not in query:
                cursor = self._next(page)
                position = None
                if cursor:
                    position = {**state['position'], 'cursor': cursor}
                elif page.get('has_more'):
                    if not messages:
                        raise ContextReadError('page_incomplete: Slack 返回空页且标记更多，请重新读取')
                    bound = 'oldest' if method == 'slack_thread' else 'latest'
                    position = {k: v for k, v in state['position'].items() if k != 'cursor'}
                    position[bound] = messages[-1]['ts']
                if position:
                    following = {**state, 'position': position, 'index': 0, 'offset': 0, 'version': None}
            self._pack(result, messages, state, version, following)
            self._target(target, source, source_info, config)
        result['has_more'] = result['next_cursor'] is not None
        # Last live source read, then durable identity/generation check. Never
        # hold a Store lock over network I/O (that would block revocation).
        self._source(access, config)
        access.validate()
        remaining(1)
        if not self._fits(result, 'channels' if method == 'slack_channels' else 'messages', settings(self.store)):
            raise ContextReadError('response_budget: 当前预算不足以容纳响应元数据或预算已改变，请调整数值策略后重读')
        return result

    @staticmethod
    def _next(page):
        value = page.get('response_metadata', {}).get('next_cursor', '')
        if not isinstance(value, str) or len(value) > 1024:
            raise ContextReadError('invalid_cursor: Slack 游标格式或长度异常')
        return value

    @staticmethod
    def _message(message):
        ts = message.get('ts', '')
        if not _TS.fullmatch(ts):
            raise ContextReadError('invalid_message: Slack 消息缺少有效 ts')
        text = message.get('text', '')
        if not isinstance(text, str):
            raise ContextReadError('invalid_message: Slack 消息正文无效')
        redacted = _TOKEN.sub('[Slack令牌已移除]', text)
        # Counts are hints only, never a copy of Slack's reply metadata/users.
        count = message.get('reply_count', 0)
        count = max(0, min(count, 2**31 - 1)) if type(count) is int else 0
        result = {'author_id': str(message.get('user') or message.get('bot_id') or '')[:64],
                'ts': ts, 'thread_ts': str(message.get('thread_ts') or ts)[:32],
                'text': redacted, 'original_text_length': len(text), 'redacted': redacted != text,
                'reply_count': count, 'has_replies': count > 0,
                'has_attachments': bool(message.get('files') or message.get('attachments'))}
        return {**result, 'text_version': digest(result)}

    def _page(self, result, field, items, next_state):
        return {**result, field: items, 'has_more': next_state is not None,
                'next_cursor': self._seal(next_state) if next_state is not None else None}

    @staticmethod
    def _fits(page, field, policy):
        # page_bytes bounds the JSON data array; response_bytes also includes
        # its MCP escaping, metadata and the actual signed continuation.
        return (len(dumps(page[field]).encode()) <= policy['page_bytes']
                and body_size(page) <= min(policy['response_bytes'], MAX_RESPONSE))

    def _pack_channels(self, result, channels, state, following):
        policy, version = settings(self.store), digest(channels)
        if state['version'] and state['version'] != version:
            raise ContextReadError('page_changed: 频道页或权限已变化，请重新读取')
        result['channels'] = []
        for index in range(state['index'], len(channels)):
            next_state = ({**state, 'index': index + 1, 'version': version}
                          if index + 1 < len(channels) else following)
            page = self._page(result, 'channels', result['channels'] + [channels[index]], next_state)
            if not self._fits(page, 'channels', policy):
                if result['channels']:
                    return
                raise ContextReadError('response_budget: 预算不足以容纳频道元数据与游标，请调整数值策略')
            result.update(page)
        if not channels:
            result.update(self._page(result, 'channels', [], following))

    def _pack(self, result, messages, state, version, following=None):
        policy = settings(self.store)
        for index in range(state['index'], len(messages)):
            message = messages[index]
            offset = state['offset'] if index == state['index'] else 0
            text = message['text']
            def candidate(end):
                item = {**message, 'text': text[offset:end], 'text_offset': offset,
                        'text_length': len(text), 'text_complete': end == len(text),
                        'fragment': offset > 0 or end < len(text)}
                if end < len(text):
                    next_state = {**state, 'index': index, 'offset': end, 'version': version}
                elif index + 1 < len(messages):
                    next_state = {**state, 'index': index + 1, 'offset': 0, 'version': version}
                else:
                    next_state = following
                return self._page(result, 'messages', result['messages'] + [item], next_state), next_state

            # A complete final message may fit because its cursor disappears.
            # Otherwise search only partial fragments (monotonic escaped size).
            page, next_state = candidate(len(text))
            if self._fits(page, 'messages', policy):
                result.update(page)
                continue
            low, high = offset, min(len(text) - 1, offset + policy['page_bytes'])
            while low < high:
                mid = (low + high + 1) // 2
                if self._fits(candidate(mid)[0], 'messages', policy):
                    low = mid
                else:
                    high = mid - 1
            if low == offset:
                if result['messages']:
                    return {**state, 'index': index, 'offset': offset, 'version': version}
                raise ContextReadError('response_budget: 预算不足以容纳消息元数据、游标与一个字符，请调整数值策略')
            page, next_state = candidate(low)
            result.update(page)
            return next_state
        if not messages:
            result.update(self._page(result, 'messages', [], following))
        return following
