"""Durable Slack Socket Mode ingress and per-channel Codex conversation workers.

Only this process reads Slack credentials. Socket receipt is synchronous and
small: commit input first, then acknowledge. Model/API work happens off that
path. Importing this module neither connects Slack nor starts a model.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import html
import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime

from .telemetry import best_effort
from .common import json_text, new_id, now
from .slack_client import SlackAPI, SlackAPIError, split_message

_LOG = logging.getLogger(__name__)


class _QuietSDKLogger(logging.Logger):
    # SDK internals sometimes log complete HTTP requests/exception traces.
    def __init__(self):
        super().__init__('laowu.slack.sdk', level=logging.CRITICAL + 1)
        self.addHandler(logging.NullHandler())
        self.propagate = False


_SDK_LOG = _QuietSDKLogger()


def slack_text(text):
    text = html.escape(text, quote=False)
    return re.sub(r'\[([^\]\n]+)\]\((https?://[^)\s]+)\)',
                  lambda m: '<' + m.group(2) + '|' + m.group(1) + '>', text)
_ROOT = re.compile(r'\A<root>\s*(.*?)\s*</root>\Z', re.S)
_TOKEN = re.compile(r'\b(?:xox[baprs]-|xapp-)[A-Za-z0-9-]+')
_USER_MENTION = re.compile(r'<@([UW][A-Z0-9]+)(?:\|[^<>|\r\n]+)?>')
_MENTION_LITERAL = re.compile(
    r'^[ \t]*(?:>{3}|(?:&gt;){3})[\s\S]*'
    r'|^[ \t]*(?:>|&gt;)[^\r\n]*'
    r'|```[\s\S]*?(?:```|\Z)'
    r'|`[^`\r\n]*(?:`|(?=\r?$))', re.M)


def _mentioned_users(text):
    """Read recipients from raw Slack text, not decoded display text.

    Mask code and quote literals (including unfinished code and >>> quotes).
    This deliberately does not parse blocks, attachments or display names.
    """
    return set(_USER_MENTION.findall(_MENTION_LITERAL.sub(' ', text)))


def normalize_message(text, bot_id):
    """Decode Slack entities; only a direct, whole-message root tag changes mode."""
    text = (text or '').lstrip()
    prefix = '<@' + bot_id + '>'
    if bot_id and text.startswith(prefix):
        text = text[len(prefix):]
    # Keep the existing canonical prefix; routing compatibility must not
    # expand root parsing, and escaped examples must not expose root.
    text = html.unescape(text).strip()
    return text, 'root' if _ROOT.fullmatch(text) else 'normal'


def _safe_error(exc):
    if isinstance(exc, SlackAPIError):
        return exc.code
    code = getattr(exc, 'code', None)
    return code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', code) else type(exc).__name__[:80]


def _user_id(payload):
    user = payload.get('user')
    return user.get('id') if isinstance(user, dict) else user


class SlackGateway:
    def __init__(self, state, *, api=None, session_factory=None, clock=time.time):
        self.state = state
        self.store = state.store
        self.api = api
        self.session_factory = session_factory
        self.clock = clock
        from .monitoring import Monitor
        from .retries import ReadRetry
        self.monitor = Monitor(self.store,clock)
        self.reads = ReadRetry(self.store,clock)
        self.provider_usage = {}
        self.epoch = secrets.token_hex(24)
        self.turn_errors = {}
        self.sessions = {}
        self.session_generations = {}
        self.running = {}
        self.active = {}
        self.last_used = {}
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='laowu-slack')
        self.socket = None
        self.app = None
        self.backfilled = False
        self.next_backfill = 0
        self.backfill_future = None
        self.retry_at = {}
        self.read_proxy = None

    def _input(self, ident):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM slack_inbox WHERE id=?', (ident,)).fetchone()
        return dict(row) if row else None

    def _approval(self, ident):
        with self.store.connect() as db:
            row = db.execute('SELECT a.*,i.team_id,i.channel_id,i.generation,i.reply_ts,i.message_ts FROM slack_approvals a JOIN slack_inbox i ON i.id=a.input_id WHERE a.id=?', (ident,)).fetchone()
        return dict(row) if row else None

    def ingest(self, payload):
        """Called within Bolt middleware before a Socket Mode acknowledgment.

        No Web API call, model call or credential payload is persisted here.
        Ignored events are safe to acknowledge; a storage failure propagates.
        """
        config = self.state.config()
        team = payload.get('team_id') or payload.get('team', {}).get('id')
        if not config.get('enabled') or team != config.get('team_id'):
            return False
        if payload.get('type') in ('block_actions', 'view_submission'):
            return self._interaction(payload)
        event = payload.get('event', {})
        kind = event.get('type')
        channel_id = event.get('channel')
        if not channel_id:
            return False
        if kind in ('member_joined_channel', 'member_left_channel'):
            row = self.state.channel(team, channel_id, create=event.get('user') == config.get('bot_user_id'))
            if row:
                # Pause before acknowledging a membership change. Validation in
                # the worker restores eligibility; no new private data can flow.
                self.state.update_channel(team, channel_id, eligible=0, error='membership_changed')
                self.wake.set()
            return bool(row)
        if _user_id(event) != config.get('owner_user_id'):
            return False
        if kind == 'app_home_opened':
            if event.get('tab') == 'messages':
                self.state.channel(team, channel_id, create=True)
            return True
        if kind == 'agent_session_stopped':
            self._stop_event(team, channel_id, event.get('thread_ts'), payload.get('event_id'), event.get('event_ts'))
            return True
        if kind not in ('message', 'app_mention') or event.get('bot_id') or event.get('subtype') not in (None, 'file_share'):
            return False
        ts = event.get('ts')
        if not isinstance(ts, str) or not re.fullmatch(r'\d+\.\d+', ts):
            return False
        bot_id = config.get('bot_user_id', '')
        raw = event.get('text') or ''
        mentions = _mentioned_users(raw)
        mentioned = bool(bot_id and bot_id in mentions)
        is_dm = event.get('channel_type') == 'im' or channel_id.startswith('D')
        # Explicit recipients outrank thread engagement, direct mode and even
        # a contradictory app_mention event. Ignore before registering a channel.
        if not is_dm and mentions and not mentioned:
            return False
        row = self.state.channel(team, channel_id)
        mode = row['mode'] if row else 'mention'
        root_ts = event.get('thread_ts') or ts
        if not (is_dm or mode == 'direct' or mentioned
                or (event.get('thread_ts') and self.state.engaged(team, channel_id, root_ts))):
            return False
        text, context = normalize_message(raw, bot_id)
        bare_mention = _USER_MENTION.fullmatch(raw.strip())
        no_request = not text or (bare_mention and bare_mention.group(1) == bot_id)
        if no_request and not event.get('files'):
            return False
        # Credentials should never be forwarded to a model or durable history.
        text = _TOKEN.sub('[Slack令牌已移除，请在本机凭据文件配置]', text)
        if event.get('files'):
            text += '\n[本条含附件；首版暂不读取附件，请仅根据已有文字回应并告知附件支持范围。]'
        if len(text) > 32000:
            text = text[:31800] + '\n[输入超出32000字符，后续内容未接收，请分开发送。]'
        reply_ts = root_ts if (not is_dm and mode == 'mention') else event.get('thread_ts')
        record = self.state.enqueue(team, channel_id, ts, text, reply_ts=reply_ts,
                                    context=context, event_id=payload.get('event_id'))
        self.state.engage(team, channel_id, root_ts)
        with self.lock:
            busy = (team, channel_id) in self.running or len(self.running) >= 2
        if busy and not record.get('duplicate'):
            self.state.queue_output(record['id'], '已收到，正在排队。', key='queued')
        self.wake.set()
        return not record.get('duplicate')

    def _stop_event(self, team, channel, reply_ts, event_id=None, event_ts=None):
        with self.lock:
            active = self.active.get((team, channel))
        if not active or (active['reply_ts'] or active['message_ts']) != reply_ts:
            return
        # An old stop retry must not stop a later turn in the same Slack thread.
        if event_ts:
            try:
                if float(event_ts) < datetime.fromisoformat(active['created_at']).timestamp():
                    return
            except (ValueError, TypeError):
                return
        with self.store.transaction() as db:
            if event_id:
                seen = db.execute('SELECT 1 FROM slack_meta WHERE key=?', ('stop:' + event_id,)).fetchone()
                if seen:
                    return
                db.execute('INSERT INTO slack_meta VALUES(?,?)', ('stop:' + event_id, active['id']))
            db.execute("UPDATE slack_inbox SET status='cancelled',error='user_stopped',updated_at=? WHERE id=? AND status='processing'", (now(), active['id']))
            db.execute("UPDATE slack_approvals SET status='expired' WHERE input_id=? AND status IN ('pending','answered')", (active['id'],))
        self.wake.set()

    def _interaction(self, payload):
        config = self.state.config()
        if _user_id(payload) != config.get('owner_user_id'):
            return False
        kind = payload.get('type')
        if kind == 'view_submission':
            view = payload.get('view', {})
            if view.get('callback_id') != 'laowu_question':
                return False
            try:
                metadata = json.loads(view.get('private_metadata', '{}'))
            except (TypeError, ValueError):
                return False
            ident, answer = metadata.get('approval'), view.get('state', {}).get('values', {})
        else:
            actions = payload.get('actions', [])
            if not actions:
                return False
            action = actions[0]
            try:
                value = json.loads(action.get('value', '{}'))
            except (TypeError, ValueError):
                return False
            ident = value.get('approval')
            answer = value.get('decision')
            metadata = value
        row = self._approval(ident) if isinstance(ident, str) else None
        if not row or row['epoch'] != self.epoch or metadata.get('epoch') != self.epoch or row['expires_at'] < self.clock() or row['status'] != 'pending':
            return False
        source = self._input(row['input_id'])
        with self.lock:
            active = self.active.get((row['team_id'], row['channel_id']))
        if not source or source['status'] != 'processing' or not active or active['id'] != row['input_id']:
            return False
        channel = self.state.channel(row['team_id'], row['channel_id'])
        if not channel or not channel['eligible'] or row['generation'] != channel['generation']:
            return False
        container = payload.get('container', {})
        channel_id = payload.get('channel', {}).get('id') or container.get('channel_id')
        if kind != 'view_submission' and channel_id != row['channel_id']:
            return False
        if kind != 'view_submission' and answer == 'question':
            # The short-lived trigger_id must be consumed immediately after the
            # durable request; this operation does not authorize model work.
            request = json.loads(row['payload'])
            request['modal_trigger'] = payload.get('trigger_id')
            with self.store.transaction() as db:
                db.execute("UPDATE slack_approvals SET payload=? WHERE id=? AND status='pending'", (json_text(request), ident))
            self.wake.set()
            return True
        request = json.loads(row['payload'])
        if request.get('method') == 'item/tool/requestUserInput':
            if kind != 'view_submission':
                return False
            result = {'answers': {}}
            for question in request.get('params', {}).get('questions', []):
                qid = question.get('id')
                values = answer.get(qid, {}).get('answer', {})
                selected = values.get('selected_options') or []
                vals = ([v.get('value', '') for v in selected] if selected else
                        [values.get('selected_option', {}).get('value', values.get('value', ''))])
                result['answers'][qid] = {'answers': vals}
        else:
            if answer not in ('accept', 'decline'):
                return False
            result = {'decision': answer}
        request['answer'] = result
        with self.store.transaction() as db:
            db.execute("UPDATE slack_approvals SET payload=?,status='answered' WHERE id=? AND status='pending' AND epoch=? AND expires_at>?", (json_text(request), ident, self.epoch, self.clock()))
        self.wake.set()
        return True

    def build_bolt(self):
        from slack_bolt import App
        from slack_bolt.authorization import AuthorizeResult
        config = self.state.config()
        def authorize(**_):
            return AuthorizeResult(enterprise_id=None, team_id=config['team_id'],
                                   bot_token=self.api.client.token, bot_user_id=config['bot_user_id'])
        app = App(client=self.api.client, authorize=authorize, process_before_response=True, logger=_SDK_LOG,
                  request_verification_enabled=False, token_verification_enabled=False,
                  ignoring_self_events_enabled=False)
        @app.middleware
        def durable_ingress(body, next):
            self.ingest(body)
            return next()
        def handled(ack):
            ack()
        for kind in ('message', 'app_mention', 'app_home_opened', 'member_joined_channel',
                     'member_left_channel', 'agent_session_stopped'):
            app.event(kind)(handled)
        app.action(re.compile(r'^laowu_'))(handled)
        app.view('laowu_question')(handled)
        self.app = app
        return app

    def socket_request(self, client, request):
        from slack_bolt.request import BoltRequest
        from slack_sdk.socket_mode.response import SocketModeResponse
        if request.type not in ('events_api', 'interactive'):
            client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
            return
        try:
            # Bolt's listener auto-ack is only an in-memory BoltResponse. The
            # actual network ack below happens after durable middleware returns.
            response = self.app.dispatch(BoltRequest(body=request.payload, mode='socket_mode'))
            if response.status == 200:
                client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        except Exception as exc:
            # Do not log envelopes, request bodies, SDK tracebacks or tokens.
            _LOG.warning('Slack收件未确认: %s', _safe_error(exc))

    def qualify(self, team, channel_id, retry_key=None):
        row = self.state.channel(team, channel_id, create=True)
        config = self.state.config()
        try:
            retry_key = retry_key or f'qualify:{team}:{channel_id}:{row["generation"]}'
            info = self.reads.call(retry_key+':info','slack.qualify',lambda:self.api.info(channel_id))
            dm = bool(info.get('is_im'))
            members = ({config['owner_user_id'], config['bot_user_id']} if dm and info.get('user') == config['owner_user_id'] else self.reads.call(retry_key+':members','slack.qualify',lambda:self.api.members(channel_id)))
            accessible = bool(not info.get('is_archived') and config['bot_user_id'] in members
                              and config['owner_user_id'] in members and not info.get('is_mpim'))
            personal = bool(accessible and ((dm and info.get('user') == config['owner_user_id'])
                            or (row.get('private_approved') and info.get('is_private') and
                                members == {config['owner_user_id'], config['bot_user_id']})))
            digest = hashlib.sha256(json_text([sorted(members), bool(info.get('is_private')), dm]).encode()).hexdigest()
            if (row.get('thread_id') or row.get('summary')) and (bool(row['personal']) != personal or
                    (row.get('member_digest') and row['member_digest'] != digest)):
                self._drop_session((team, channel_id))
                self.state.reset(team, channel_id)
            return self.state.update_channel(team, channel_id, personal=int(personal),
                                             eligible=int(accessible), member_digest=digest,
                                             error=None if accessible else 'channel_not_accessible')
        except Exception as exc:
            from .retries import RetryPending, RetrySuspended
            if isinstance(exc,(RetryPending,RetrySuspended)):
                raise
            if row.get('personal'):
                self._drop_session((team, channel_id))
                self.state.reset(team, channel_id)
            self.state.update_channel(team, channel_id, eligible=0, personal=0, error=_safe_error(exc))
            raise

    def _drop_session(self, key):
        with self.lock:
            session = self.sessions.pop(key, None)
            self.session_generations.pop(key, None)
            self.last_used.pop(key, None)
        if session:
            with contextlib.suppress(Exception):
                session.close()

    def _get_session(self, row):
        key = row['team_id'], row['channel_id']
        with self.lock:
            existing = self.sessions.get(key)
        if existing:
            if (self.session_generations.get(key) == row['generation'] and
                    getattr(existing, 'thread_id', None) == row.get('thread_id')):
                return existing
            self._drop_session(key)
        if self.session_factory is None:
            from .slack_codex import SlackCodexSession
            factory = SlackCodexSession
        else:
            factory = self.session_factory
        binding = self.state.issue_binding(*key)
        session = factory(self.store.root, binding, thread_id=row.get('thread_id'),
                          on_notification=lambda event: self._notification(key, event),
                          on_request=lambda event: self._server_request(key, event, generation=row['generation']))
        try:
            thread_id = session.start()
            self.state.bind_thread(*key, thread_id)
        except Exception:
            with contextlib.suppress(Exception):
                session.close()
            raise
        with self.lock:
            self.sessions[key] = session
            self.session_generations[key] = row['generation']
            self.last_used[key] = self.clock()
        return session

    def _notification(self, key, envelope):
        # Only final answers are exported. Polling a durable receipt provides
        # completion, so token deltas and reasoning events need no processing.
        from .monitoring import event_metadata
        params = envelope.get('params') or {}
        turn_id = params.get('turnId') or (params.get('turn') or {}).get('id')
        channel = self.state.channel(*key)
        if not channel or (params.get('threadId') and params['threadId'] != channel.get('thread_id')):
            return
        with self.lock:
            active = self.active.get(key)
        # Late callbacks from an old turn must never advance a newer turn.
        # Reconciliation pauses the model worker, not the independent observer.
        if turn_id and (not active or active.get('turn_id') != turn_id):
            with self.store.connect() as db:
                saved=db.execute("SELECT * FROM slack_inbox WHERE team_id=? AND channel_id=? AND generation=? AND turn_id=? AND status IN ('processing','running','uncertain') LIMIT 1",(*key,channel['generation'],turn_id)).fetchone()
            if saved:
                active=dict(saved)
            elif not active or active.get('turn_id'):
                active=None
        if active:
            metadata = event_metadata(envelope)
            best_effort(self.monitor.observe, 'slack:'+active['id'],**metadata)
            if metadata.get('usage'):
                self.provider_usage.setdefault(active['id'],{}).update(metadata['usage'])
        params = envelope.get('params') or {}
        error = params.get('error') or (params.get('turn') or {}).get('error')
        category = error.get('diagnostic', {}).get('category') if isinstance(error, dict) else None
        if category in {'authentication', 'permission', 'configuration', 'rate_limit', 'usage_limit',
                        'network', 'stream', 'server', 'context_limit', 'filesystem_read_only'} and not params.get('willRetry'):
            if active:
                self.turn_errors[active['id']] = 'client_' + category
        self.wake.set()

    def _server_request(self, key, envelope, generation=None):
        with self.lock:
            active = self.active.get(key)
        if not active:
            return
        channel = self.state.channel(*key)
        params = envelope.get('params', {})
        if not channel or (generation is not None and channel['generation'] != generation) or (
                params.get('threadId') and params['threadId'] != channel.get('thread_id')):
            return
        method = envelope.get('method', '')
        allowed = {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                   'item/tool/requestUserInput'}
        if method not in allowed:
            with self.lock:
                session = self.sessions.get(key)
            if session:
                session.reply(envelope.get('id'), {'decision': 'decline'})
            return
        best_effort(self.monitor.observe, 'slack:'+active['id'],phase='user_wait' if method == 'item/tool/requestUserInput' else 'approval_wait',client_event=True)
        ident = new_id('slackapproval')
        params = envelope.get('params', {})
        # Approval transport stores only fields needed for safe review/questions;
        # arbitrary provider metadata/command environment never enters the DB.
        if method == 'item/tool/requestUserInput':
            safe_params = {'questions': params.get('questions', [])}
        else:
            from .store import _SECRET
            command = str(params.get('command') or params.get('reason') or '')
            command = _TOKEN.sub('[令牌已移除]', command)
            command = _SECRET.sub('[秘密已移除]', command)
            command = re.sub(r'(?i)(password|passwd|secret|token|api[_-]?key|authorization)(\s*(?:=|:)\s*|\s+)([^\s]+)',
                             lambda m: m.group(1) + '=[秘密已移除]', command)
            command = re.sub(r'https?://[^/\s:@]+:[^/\s@]+@', 'https://[凭据已移除]@', command)
            safe_params = {'preview': command[:2600], 'itemId': params.get('itemId'), 'turnId': params.get('turnId')}
        payload = {'method': method, 'params': safe_params}
        # Raw command arguments can contain secrets. Approval exposes a bounded
        # description only; credential-looking Slack strings are redacted.
        with self.store.transaction() as db:
            db.execute('INSERT INTO slack_approvals VALUES(?,?,?,?,?,?,?,?)',
                       (ident, active['id'], json_text(envelope.get('id')), self.epoch,
                        json_text(payload), 'pending', self.clock() + 900, now()))
        value = lambda decision: json.dumps({'approval': ident, 'epoch': self.epoch, 'decision': decision})
        question = method == 'item/tool/requestUserInput'
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text':
                    '老五需要你补充信息。' if question else '老五需要你确认本次操作。'}},
                  {'type': 'actions', 'elements': ([{'type': 'button', 'action_id': 'laowu_question',
                     'text': {'type': 'plain_text', 'text': '填写回答'}, 'value': value('question')}]
                    if question else [
                     {'type': 'button', 'action_id': 'laowu_accept', 'text': {'type': 'plain_text', 'text': '允许本次'}, 'value': value('accept')},
                     {'type': 'button', 'action_id': 'laowu_decline', 'text': {'type': 'plain_text', 'text': '拒绝'}, 'value': value('decline')}])}]
        if not question:
            params = payload['params']
            description = params.get('preview') or '文件修改请求；请在私聊核对具体任务。'
            blocks.insert(1, {'type': 'section', 'text': {'type': 'mrkdwn',
                          'text': _TOKEN.sub('[令牌已移除]', str(description))[:2600]}})
        self.state.queue_output(active['id'], '等待你的回答。' if question else '等待本次操作确认。', blocks, key=ident)
        with contextlib.suppress(Exception):
            self.api.status(key[1], active['reply_ts'] or active['message_ts'], 'suspended')
        self.wake.set()

    def _approvals(self):
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM slack_approvals WHERE epoch=? AND status IN ('pending','answered')", (self.epoch,))]
        for row in rows:
            source = self._input(row['input_id'])
            if not source:
                continue
            key = source['team_id'], source['channel_id']
            with self.lock:
                session = self.sessions.get(key)
                active = self.active.get(key)
            if source['status'] != 'processing' or not active or active['id'] != source['id']:
                with self.store.transaction() as db:
                    db.execute("UPDATE slack_approvals SET status='expired' WHERE id=? AND status IN ('pending','answered')", (row['id'],))
                continue
            payload = json.loads(row['payload'])
            if row['expires_at'] <= self.clock():
                with self.store.transaction() as db:
                    db.execute("UPDATE slack_approvals SET status='expired' WHERE id=?", (row['id'],))
                if session:
                    session.interrupt(source.get('turn_id'))
                continue
            if payload.get('modal_trigger'):
                trigger = payload.pop('modal_trigger')
                # Remove trigger durably before using it; expired triggers need
                # another deliberate button click rather than repeated calls.
                with self.store.transaction() as db:
                    db.execute('UPDATE slack_approvals SET payload=? WHERE id=?', (json_text(payload), row['id']))
                blocks = []
                for question in payload.get('params', {}).get('questions', [])[:10]:
                    qid = str(question.get('id', 'question'))
                    options = question.get('options') or []
                    element = {'type': 'plain_text_input', 'action_id': 'answer', 'multiline': True}
                    if options:
                        element = {'type': 'multi_static_select' if question.get('isOther') is False and question.get('isMultiple') else 'static_select', 'action_id': 'answer',
                                   'options': [{'text': {'type': 'plain_text', 'text': str(o.get('label', ''))[:75]}, 'value': str(o.get('label', ''))[:150]} for o in options[:100]]}
                    blocks.append({'type': 'input', 'block_id': qid, 'label': {'type': 'plain_text', 'text': str(question.get('question', '请补充'))[:150]}, 'element': element})
                try:
                    self.api.call('views.open', trigger_id=trigger, view={'type': 'modal', 'callback_id': 'laowu_question',
                                  'private_metadata': json.dumps({'approval': row['id'], 'epoch': self.epoch}),
                                  'title': {'type': 'plain_text', 'text': '回复老五'},
                                  'submit': {'type': 'plain_text', 'text': '提交'}, 'blocks': blocks})
                except Exception:
                    self.state.queue_output(source['id'], '回答窗口已过期，请重新点击填写回答。', key=row['id'] + ':expired')
            if row['status'] == 'answered' and session:
                channel = self.qualify(*key)
                if not channel['eligible'] or channel['generation'] != source['generation']:
                    continue
                with self.lock:
                    current = self._input(source['id'])
                    active = self.active.get(key)
                    with self.store.connect() as db:
                        approval_status = db.execute('SELECT status FROM slack_approvals WHERE id=?', (row['id'],)).fetchone()
                    if not current or current['status'] != 'processing' or not active or active['id'] != source['id'] or not approval_status or approval_status[0] != 'answered':
                        continue
                    session.reply(json.loads(row['request_id']), payload['answer'])
                    with self.store.transaction() as db:
                        db.execute("UPDATE slack_approvals SET status='replied' WHERE id=? AND status='answered'", (row['id'],))
                best_effort(self.monitor.observe, 'slack:'+source['id'],phase='working')
                self.api.status(key[1], source['reply_ts'] or source['message_ts'], 'processing')

    def tick(self):
        # Must be called by one coordinator. Per-channel model work is isolated
        # in two workers; receiving Socket envelopes never waits for it.
        for key, future in list(self.running.items()):
            if future.done():
                with contextlib.suppress(Exception):
                    future.result()
                self.running.pop(key, None)
        config = self.state.config()
        if not config.get('enabled'):
            return
        # A transport interruption must not strand the independent local
        # cancellation. These durable markers also survive a gateway restart.
        with contextlib.closing(self.store.connect()) as db:
            stopped = [dict(row) for row in db.execute("SELECT * FROM slack_inbox WHERE status='cancelled' "
                       "AND (error='user_stopped' OR error GLOB 'user_stop_cleanup_pending*')")]
        for record in stopped:
            key = record['team_id'], record['channel_id']
            if key not in self.running and self.retry_at.get(record['id'], 0) <= self.clock():
                try:
                    self._finish_stop(record, retry=True)
                except Exception as exc:
                    # One damaged/unavailable stop record must not prevent
                    # delivery or scheduling for every other input.
                    self.retry_at[record['id']] = self.clock() + 2
                    _LOG.warning('Slack停止恢复未完成: %s', _safe_error(exc))
        if self._paused():
            self.deliver_outputs(alerts_only=True)
            return
        for key in list(self.sessions):
            channel = self.state.channel(*key)
            session = self.sessions.get(key)
            if (not channel or not channel['eligible'] or self.session_generations.get(key) != channel['generation']
                    or getattr(session, 'thread_id', None) != channel.get('thread_id')):
                self._drop_session(key)
        self._approvals()
        self.deliver_outputs()
        self._settle_notifications()
        for channel in self.state.channels():
            key = channel['team_id'], channel['channel_id']
            if channel.get('error') == 'membership_changed' and key not in self.running:
                with contextlib.suppress(Exception):
                    self.qualify(*key)
        for record in self.state.inbox():
            if record['status'] not in ('pending', 'uncertain') or self.retry_at.get(record['id'], 0) > self.clock():
                continue
            key = record['team_id'], record['channel_id']
            if key in self.running or len(self.running) >= 2:
                continue
            # Claim before scheduling to prevent a second tick duplicating it.
            old_status = record['status']
            with self.store.transaction() as db:
                claimed = db.execute("UPDATE slack_inbox SET status='processing',updated_at=? WHERE id=? AND status=?", (now(), record['id'], old_status)).rowcount
                if claimed and old_status == 'uncertain':
                    db.execute("INSERT OR REPLACE INTO read_sources VALUES('input',?,'uncertain')", (record['id'],))
            if not claimed:
                continue
            record['_reconcile'] = old_status == 'uncertain'
            self.running[key] = self.executor.submit(self._execute, record)
        for key in list(self.sessions):
            if key not in self.running and self.clock() - self.last_used.get(key, 0) >= 600:
                self._drop_session(key)

    def _execute(self, record):
        from .features import enabled
        from .retries import RetryPending, RetrySuspended
        start_time = self.clock()
        from .metrics import begin,seconds
        key = record['team_id'], record['channel_id']
        session, turn_id = None, record.get('turn_id')
        try:
            best_effort(begin, self.store,record['id'],'slack',seconds(record['created_at']),start_time)
            best_effort(self.monitor.start, 'slack:'+record['id'],'slack',origin=f"slack:{record['team_id']}:{record['channel_id']}:{record['generation']}",
                               input_id=record['id'],context=record['context'],generation=record['generation'])
            if record.get('_reconcile') or record.get('status') == 'uncertain':
                self.state.submitting(record['id'])
            record['_reconcile'] = self.state.read_source('input',record['id']) != 'pending'
            channel = self.qualify(*key,retry_key=record['id']+':qualify')
            if not channel['eligible']:
                raise PermissionError('channel_not_accessible')
            current = self._input(record['id'])
            if not current:
                return
            if channel['generation'] != record['generation']:
                # A freshly received owner's message can begin the replacement
                # shared context. Earlier queued/private/reconciled work stays cancelled.
                if record.get('_reconcile') or record.get('origin_notice'):
                    self.state.update_input(record['id'], status='uncertain', error='context_changed_needs_reconciliation')
                    self.retry_at[record['id']] = self.clock() + 30
                    return
                record['generation'] = channel['generation']
                self.state.update_input(record['id'], status='processing', body=record['body'], generation=channel['generation'])
            elif current['status'] == 'cancelled':
                return
            if record['body'].strip() in ('重新开始当前对话', '/laowu reset') and not record.get('origin_notice'):
                self._cancel_sync(record)
                self._drop_session(key)
                self.state.reset(*key)
                self.state.update_input(record['id'], status='completed', generation=channel['generation'] + 1)
                self.state.queue_output(record['id'], '当前对话已重新开始，长期记忆保留。', key='reset')
                return
            session = self._get_session(channel)
            with self.lock:
                self.active[key] = record
            self.api.status(key[1], record['reply_ts'] or record['message_ts'], 'processing', self.state.config()['owner_user_id'])
            self.state.update_input(record['id'], thread_id=channel.get('thread_id') or self.state.channel(*key)['thread_id'])
            receipt_key = record.get('origin_notice') or record['id']
            if record.get('_reconcile'):
                receipt = self.reads.call(record['id']+':receipt','slack.receipt',lambda:self._receipt(session,receipt_key,record.get('turn_id')))
                if receipt['status'] == 'uncertain':
                    self.state.update_input(record['id'], status='uncertain', error='model_submission_uncertain')
                    self.retry_at[record['id']] = self.clock() + 30
                    return
                turn_id = receipt.get('turn_id')
            else:
                self.state.submitting(record['id'])
                session.slack_context = {'channel_id': record['channel_id'],
                                         'reply_thread_ts': record.get('reply_ts'),
                                         'message_ts': record['message_ts'],
                                         'context': record['context']}
                if record.get('origin_notice'):
                    with self.store.connect() as db:
                        event = db.execute('SELECT * FROM notifications WHERE id=?', (record['origin_notice'],)).fetchone()
                    if not event:
                        raise ValueError('background_event_missing')
                    turn_id = session.start_event(dict(event))
                else:
                    context = ('可信Slack会话：本轮语境=' + record['context'] + '。只向本频道回答。'
                               ' 用户来源为已绑定本人。附件标记只描述能力边界。')
                    turn_id = session.start_turn(record['body'], record['id'], extra_context=context)
                receipt = {'status': 'submitted'}
            self.state.update_input(record['id'], turn_id=turn_id)
            record['turn_id'] = turn_id
            while not self.closed.is_set():
                best_effort(self.monitor.observe, 'slack:'+record['id'],heartbeat=True)
                current = self._input(record['id'])
                if current and current['status'] == 'cancelled':
                    return
                if receipt['status'] == 'completed':
                    text = receipt.get('final_text', '')
                    if not text:
                        text = '本轮已结束，但没有收到可交付的文字结果。'
                    with self.store._lock():
                        live = self._input(record['id'])
                        if not live or live['status'] != 'processing' or live['generation'] != record['generation']:
                            return
                        self._queue_final(record, text)
                        self.state.update_input(record['id'], status='completed', error=None)
                    best_effort(self.monitor.observe, 'slack:'+record['id'],progress_key='valid_handoff')
                    return
                if receipt['status'] in ('failed', 'interrupted'):
                    code = self.turn_errors.pop(record['id'], 'codex_' + receipt['status'])
                    self.state.update_input(record['id'], status=receipt['status'], error=code)
                    label = {'client_authentication': 'Codex 登录或身份认证失败',
                             'client_permission': 'Codex 权限不足', 'client_usage_limit': 'Codex 使用额度不足',
                             'client_rate_limit': 'Codex 服务限流', 'client_network': 'Codex 网络连接失败'}.get(code, '本次执行未能完成')
                    self.state.queue_output(record['id'], label + '（' + code + '）。可检查本机 slack doctor 后继续处理。', key='execution-failed')
                    return
                self.closed.wait(1)
                receipt = self.reads.call(record['id']+':receipt','slack.receipt',lambda:self._receipt(session,receipt_key,turn_id))
        except (RetryPending,RetrySuspended) as exc:
            with self.store._lock():
                live = self._input(record['id'])
                if not live or live['status'] == 'cancelled' or live['generation'] != record['generation']:
                    return
                status = self.state.read_source('input',record['id'])
                if isinstance(exc,RetrySuspended): status = 'blocked'
                self.state.update_input(record['id'],status=status,error=type(exc).__name__)
            self.retry_at[record['id']] = self.clock() + 2
        except Exception as exc:
            # Unknown submission is reconciled on restart/next iteration, never
            # automatically rerun after a possibly successful external action.
            status = 'uncertain' if self.state.read_source('input',record['id']) != 'pending' else 'failed'
            with self.store._lock():
                live = self._input(record['id'])
                if not live or live['status'] == 'cancelled' or live['generation'] != record['generation']:
                    return
                self.state.update_input(record['id'], status=status, error=_safe_error(exc))
            self.retry_at[record['id']] = self.clock() + 30
            if status == 'failed':
                self.state.queue_output(record['id'], '本次请求暂时受阻（' + _safe_error(exc) + '）。请检查本机 slack doctor。', key='failed')
        finally:
            try:
                current = best_effort(self._input, record['id'])
                if current and current['status'] == 'cancelled' and current['error'] == 'user_stopped':
                    self._finish_stop(current, session=session, turn_id=turn_id)
                if current and current['status'] in ('completed','failed','interrupted','cancelled','blocked'):
                    best_effort(self.monitor.observe, 'slack:'+record['id'],finished=True)
                    from .metrics import finish
                    best_effort(finish, self.store,record['id'],'slack',self.clock(),**self.provider_usage.pop(record['id'],{}))
            finally:
                with self.lock:
                    self.active.pop(key, None)
                    self.turn_errors.pop(record['id'], None)
                    self.last_used[key] = self.clock()
                self.wake.set()
                with contextlib.suppress(Exception):
                    self.api.status(key[1], record['reply_ts'] or record['message_ts'], 'active')

    def _receipt(self,session,key,turn_id):
        from .features import enabled
        value = session.history_receipt(key,turn_id)
        if enabled(self.store,'read_retries') and value['status']=='uncertain':
            from .retries import ReceiptUnavailable
            raise ReceiptUnavailable()
        return value

    def _reconcile_output(self, source, output, oldest):
        from .features import enabled
        ts = self.api.reconcile(source['channel_id'],key=output['id'],oldest=oldest,thread_ts=source.get('reply_ts'))
        if not ts and enabled(self.store,'read_retries'):
            from .retries import ReceiptUnavailable
            raise ReceiptUnavailable()
        return ts

    def _finish_stop(self, record, *, session=None, turn_id=None, retry=False):
        """Cancel local work independently; never replay an uncertain RPC."""
        cleanup_error = None
        try:
            self._cancel_sync(record, strict=True)
        except Exception as exc:
            cleanup_error = _safe_error(exc)

        interrupt_error = None
        if retry:
            previous = record.get('error') or ''
            interrupt_uncertain = previous == 'user_stopped' or 'interrupt_uncertain' in previous
        else:
            interrupt_uncertain = True
            if session is not None and turn_id:
                try:
                    session.interrupt(turn_id)
                    interrupt_uncertain = False
                except Exception as exc:
                    interrupt_error = _safe_error(exc)

        if cleanup_error:
            error = 'user_stop_cleanup_pending'
            if interrupt_uncertain:
                error += '_interrupt_uncertain'
            error += ':' + cleanup_error
            if interrupt_error:
                error += ':' + interrupt_error
            message = ('模型停止尚未确认，且同步任务取消尚未完成；'
                       if interrupt_uncertain else '已请求停止本轮模型执行，但同步任务取消尚未完成；')
            message += '系统会重试本地取消，请检查本机 Slack 状态。'
            output_key = 'stop-cleanup-pending:' + str(interrupt_uncertain)
            self.retry_at[record['id']] = self.clock() + 2
        else:
            error = ('user_stop_interrupt_uncertain' + (':' + interrupt_error if interrupt_error else '')
                     if interrupt_uncertain else 'user_stop_cleanup_complete')
            message = ('本次请求的同步任务已取消；模型停止尚未确认，请在本机核查本轮状态。'
                       if interrupt_uncertain else '已请求停止本轮模型执行，并取消本次请求的同步任务。')
            output_key = 'stopped:' + str(interrupt_uncertain)
            self.retry_at.pop(record['id'], None)
        # The result marker and receipt form one commit. Delivery can clear a
        # settled receipt's body, so a recovery must also reuse that receipt
        # without restoring its text or sending it again.
        with self.store.transaction() as db:
            current = db.execute('SELECT status,generation FROM slack_inbox WHERE id=?',
                                 (record['id'],)).fetchone()
            if not current or current['status'] != 'cancelled' or current['generation'] != record['generation']:
                return
            if not retry or not cleanup_error:
                part = int(hashlib.sha256(output_key.encode()).hexdigest()[:12], 16)
                existing = db.execute('SELECT body,blocks,status FROM slack_outbox WHERE input_id=? AND part=?',
                                      (record['id'], part)).fetchone()
                if existing:
                    purged = existing['status'] in ('sent', 'cancelled') and not existing['body']
                    if existing['blocks'] != '[]' or (existing['body'] != message and not purged):
                        raise ValueError('同一停止回执标识已用于不同内容')
                else:
                    ident, stamp = new_id('slackout'), now()
                    db.execute('INSERT INTO slack_outbox(id,input_id,part,body,blocks,created_at,updated_at) '
                               'VALUES(?,?,?,?,?,?,?)', (ident, record['id'], part, message, '[]', stamp, stamp))
                    db.execute("INSERT INTO read_sources VALUES('output',?,'pending')", (ident,))
            # Only bounded codes are saved; never exception/provider payloads.
            db.execute('UPDATE slack_inbox SET error=?,updated_at=? WHERE id=?', (error, now(), record['id']))

    def _cancel_sync(self, record, *, strict=False):
        from .access import cancel_input_tasks
        ident = 'slack:' + record['team_id'] + ':' + record['channel_id'] + ':' + str(record['generation'])
        if strict:
            return cancel_input_tasks(self.store, ident, record['id'])
        with contextlib.suppress(PermissionError, ValueError):
            return cancel_input_tasks(self.store, ident, record['id'])

    def _paused(self):
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM background_meta WHERE key='paused'").fetchone()
        return bool(row and row[0] == '1')

    def _queue_final(self, record, text):
        text = slack_text(_TOKEN.sub('[Slack令牌已移除]', text))
        for index, part in enumerate(split_message(text)):
            self.state.queue_output(record['id'], part, key='final:' + str(index))

    def deliver_outputs(self,alerts_only=False):
        from .features import enabled
        from .retries import RetryPending, RetrySuspended
        if not self.state.config().get('enabled'):
            return
        alerts_only = alerts_only or self._paused()
        blocked = set()
        for output in self.state.outputs():
            if alerts_only and not output['id'].startswith('monitor:'):
                continue
            if output['input_id'] in blocked or output['status'] not in ('pending', 'uncertain', 'sending'):
                continue
            if output['next_attempt'] > self.clock():
                blocked.add(output['input_id'])
                continue
            attempted_post = False
            source = self._input(output['input_id'])
            if not source:
                continue
            try:
                channel = self.qualify(source['team_id'], source['channel_id'],retry_key=output['id']+':qualify')
                if not channel['eligible']:
                    blocked.add(output['input_id'])
                    continue
                if channel['generation'] != source['generation']:
                    self.state.update_output(output['id'], status='cancelled', error='privacy_generation_changed', body='', blocks='[]')
                    continue
                if output['status'] in ('uncertain', 'sending'):
                    oldest = max(0, datetime.fromisoformat(output['created_at']).timestamp() - 60)
                    ts = self.reads.call(output['id']+':reconcile','slack.reconcile',lambda:self._reconcile_output(source,output,oldest))
                    if ts:
                        self.state.update_output(output['id'], status='sent', slack_ts=ts, error=None)
                        from .metrics import sample,seconds
                        best_effort(sample, self.store,output['input_id'],'delivery',sample_id='delivery:'+output['id'],delivery_seconds=max(0,self.clock()-seconds(output['created_at'])))
                    else:
                        self.state.update_output(output['id'], status='uncertain', error='delivery_uncertain', next_attempt=self.clock() + 60)
                        blocked.add(output['input_id'])
                    continue
                self.state.update_output(output['id'], status='sending')
                attempted_post = True
                response = self.api.post(source['channel_id'], output['body'], thread_ts=source.get('reply_ts'),
                                         key=output['id'], blocks=json.loads(output['blocks']) or None)
                self.state.update_output(output['id'], status='sent', slack_ts=response['ts'], error=None)
                from .metrics import sample, seconds
                best_effort(sample, self.store,output['input_id'],'delivery',sample_id='delivery:'+output['id'],delivery_seconds=max(0,self.clock()-seconds(output['created_at'])))
            except (RetryPending,RetrySuspended) as exc:
                self.state.update_output(output['id'],status='blocked' if isinstance(exc,RetrySuspended) else output['status'],
                                         error=type(exc).__name__,next_attempt=self.clock()+2)
                blocked.add(output['input_id'])
            except SlackAPIError as exc:
                if enabled(self.store,'read_retries'):
                    self.state.update_output(output['id'],status='uncertain' if attempted_post and exc.uncertain else 'blocked',error=exc.code,next_attempt=self.clock()+2)
                    blocked.add(output['input_id'])
                    continue
                blocked.add(output['input_id'])
                uncertain = output['status'] in ('uncertain', 'sending') or (attempted_post and exc.uncertain)
                self.state.update_output(output['id'], status='uncertain' if uncertain else 'pending',
                                         error=exc.code, next_attempt=self.clock() + (exc.retry_after or 60))
            except Exception as exc:
                if enabled(self.store,'read_retries'):
                    self.state.update_output(output['id'],status='uncertain' if attempted_post else 'blocked',error=_safe_error(exc),next_attempt=self.clock()+2)
                    blocked.add(output['input_id'])
                    continue
                blocked.add(output['input_id'])
                self.state.update_output(output['id'], status='uncertain' if output['status'] != 'pending' or attempted_post else 'pending',
                                         error=_safe_error(exc), next_attempt=self.clock() + 60)

    def _settle_notifications(self):
        with self.store.transaction() as db:
            db.execute("""UPDATE notifications SET status='delivered',error=NULL,updated_at=?
                WHERE id IN (SELECT i.origin_notice FROM slack_inbox i
                  WHERE i.origin_notice IS NOT NULL AND i.status='completed'
                  AND EXISTS (SELECT 1 FROM slack_outbox o WHERE o.input_id=i.id)
                  AND NOT EXISTS (SELECT 1 FROM slack_outbox o WHERE o.input_id=i.id AND o.status!='sent'))
                  AND status!='cancelled'""", (now(),))
            # A durable queue is not another transcript archive. Once every
            # output is known sent/cancelled, retain only routing/receipt data.
            settled = "SELECT i.id FROM slack_inbox i WHERE i.status IN ('completed','cancelled','failed','interrupted') AND NOT EXISTS (SELECT 1 FROM slack_outbox o WHERE o.input_id=i.id AND o.status NOT IN ('sent','cancelled'))"
            db.execute('UPDATE slack_inbox SET body=\'\' WHERE id IN (' + settled + ')')
            db.execute("UPDATE slack_outbox SET body='',blocks='[]' WHERE input_id IN (" + settled + " ) AND status IN ('sent','cancelled')")
            db.execute("UPDATE slack_approvals SET payload='{}' WHERE input_id IN (" + settled + " ) AND status IN ('replied','expired')")

    def backfill(self):
        """Bounded recovery of registered channels/known threads, never raw import."""
        config = self.state.config()
        for channel in self.state.channels():
            if not channel.get('last_ts'):
                continue  # First installation must not ingest old history.
            oldest = max(float(channel['last_ts']), self.clock() - 86400)
            with self.store.connect() as db:
                roots = [r[0] for r in db.execute('SELECT root_ts FROM slack_threads WHERE team_id=? AND channel_id=?', (channel['team_id'], channel['channel_id']))]
            try:
                recovered = {}
                for root_ts in [None, *roots]:
                    read_key=f"backfill:{channel['team_id']}:{channel['channel_id']}:{channel['generation']}:{channel['last_ts']}:{root_ts}"
                    messages=self.reads.call(read_key,'slack.backfill',lambda:list(self.api.history(channel['channel_id'],oldest=oldest,thread_ts=root_ts)))
                    for message in messages:
                        if float(message.get('ts', 0)) <= oldest:
                            continue
                        event = dict(message, type='message', channel=channel['channel_id'])
                        if root_ts:
                            event.setdefault('thread_ts', root_ts)
                        recovered[event['ts']] = event
                for ts in sorted(recovered, key=float):
                    self.ingest({'type': 'event_callback', 'team_id': config['team_id'],
                                 'event_id': 'backfill:' + channel['channel_id'] + ':' + ts,
                                 'event': recovered[ts]})
                self.state.update_channel(channel['team_id'], channel['channel_id'], error=None)
            except Exception as exc:
                self.state.update_channel(channel['team_id'], channel['channel_id'], error='backfill_' + _safe_error(exc))

    def run(self):
        from slack_sdk.socket_mode import SocketModeClient
        config = self.state.config()
        if not config.get('enabled'):
            return
        credentials = self.state.credentials()
        if self.api is None:
            from slack_sdk import WebClient
            # WebClient.logger is read-only in the official SDK. Configure it
            # during construction, before any authenticated request can log.
            self.api = SlackAPI(client=WebClient(token=credentials['bot_token'], timeout=10,
                                                  retry_handlers=[], logger=_SDK_LOG))
        auth = self.api.call('auth.test')
        if auth.get('team_id') != config['team_id']:
            raise PermissionError('Slack工作区与配置不符')
        self.state.set_config({'bot_user_id': auth['user_id']})
        self.state.recover()
        self.build_bolt()
        # Disable SDK debug logging because raw envelopes may contain personal
        # data and token values. Exceptions are reported as bounded codes.
        logging.getLogger('slack_sdk').setLevel(logging.CRITICAL)
        logging.getLogger('slack_bolt').setLevel(logging.CRITICAL)
        # Pass optional lifecycle listeners at construction; do not assume the
        # SDK has initialized a mutable list for an omitted argument.
        self.socket = SocketModeClient(app_token=credentials['app_token'], web_client=self.api.client, logger=_SDK_LOG,
                                       on_close_listeners=[lambda *_: self._socket_closed()])
        self.socket.socket_mode_request_listeners.append(self.socket_request)
        session_id = None
        try:
            from .slack_context import SlackContextReader
            from .slack_read_proxy import SlackReadProxy
            self.read_proxy = SlackReadProxy(SlackContextReader(self.state, self.api, clock=self.clock))
            self.read_proxy.start()
            self._runtime('connecting')
            self.socket.connect()
            while not self.closed.is_set() and self.state.config().get('enabled'):
                try:
                    self.tick()
                    if self.socket.is_connected():
                        current_session_id = self.socket.session_id()
                        if current_session_id != session_id:
                            # SDK endpoint rotation can replace a connection
                            # without invoking the close listeners.
                            session_id = current_session_id
                            self.backfilled = False
                        if not self.backfilled and (not self.backfill_future or self.backfill_future.done()):
                            # Separate worker prevents history APIs blocking input.
                            self.backfill_future = self.executor.submit(self.backfill)
                            self.backfilled = True
                        self._runtime('connected')
                    else:
                        self.backfilled = False
                        self._runtime('disconnected')
                except Exception as exc:
                    self._runtime('degraded', error=_safe_error(exc))
                self.wake.wait(0.5)
                self.wake.clear()
        finally:
            self.close()

    def _socket_closed(self):
        self.backfilled = False
        self.wake.set()

    def _runtime(self, status, **extra):
        import os
        from .common import process_identity
        self.state.runtime(status=status, pid=os.getpid(), identity=process_identity(os.getpid()), **extra)

    def close(self):
        self.closed.set()
        self.wake.set()
        if self.read_proxy:
            self.read_proxy.close()
            self.read_proxy = None
        if self.socket:
            with contextlib.suppress(Exception):
                self.socket.close()
        for key in list(self.sessions):
            self._drop_session(key)
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.state.runtime(status='stopped')


def run(store):
    from .slack_state import SlackState
    gateway = SlackGateway(SlackState(store))
    import signal
    def shutdown(*_):
        gateway.closed.set()
        gateway.wake.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    gateway.run()
