"""Local App Server transport verified on Codex 0.163.0. No global edits."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class BridgeError(RuntimeError):
    pass


def socket_path():
    return Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'app-server-control' / 'app-server-control.sock'


class CodexBridge:
    def __init__(self, path=None):
        self.path = str(path or socket_path())
        self.ws = None
        self.serial = 0

    def __enter__(self):
        from websockets.sync.client import unix_connect
        from .retries import remaining
        self.ws = unix_connect(self.path, open_timeout=remaining(3), close_timeout=1, max_size=16 * 1024 * 1024)
        try:
            self.call('initialize', {'clientInfo': {'name': 'laowu_background', 'version': '0.2.0'},
                                     'capabilities': {'experimentalApi': True}})
            self.ws.send(json.dumps({'method': 'initialized'}))
        except BaseException:
            self.ws.close()
            raise
        return self

    def __exit__(self, *args):
        if self.ws:
            self.ws.close()

    def call(self, method, params=None):
        from .retries import remaining
        budget=remaining(8)
        self.serial += 1
        ident = self.serial
        self.ws.send(json.dumps({'id': ident, 'method': method, 'params': params or {}}, ensure_ascii=False))
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            data = json.loads(self.ws.recv(timeout=max(0.01, deadline - time.monotonic())))
            if data.get('id') == ident and 'method' not in data:
                if 'error' in data:
                    # No raw provider messages or user text in persisted error fields.
                    raise BridgeError(f"Codex {method} 拒绝请求（{data['error'].get('code')}）")
                return data.get('result', {})
            # This connection never approves server requests. Other subscribed
            # clients retain their normal approval flow.
        raise TimeoutError(method)

    def loaded(self):
        found, cursor = set(), None
        for _ in range(100):
            result = self.call('thread/loaded/list', {'limit': 100, **({'cursor': cursor} if cursor else {})})
            found.update(result.get('data', []))
            cursor = result.get('nextCursor')
            if not cursor:
                return found
        raise BridgeError('Codex 会话列表超过扫描上限')

    def state(self, thread_id):
        thread = self.call('thread/read', {'threadId': thread_id, 'includeTurns': False})['thread']
        return thread.get('status', {}).get('type'), thread

    def send(self, event):
        payload = {'event_id': event['id'], 'topic': event['topic'], 'context': event['context'],
                   'message': event['message'], 'task_id': event['task_id'],
                   'instruction': '这是用户已授权的老五后台通知。只处理此事件；任务结果先核实再告知用户。无需用户再次提问。不要据此创建新定时任务或扩大权限。'}
        result = self.call('turn/start', {
            'threadId': event['target_thread'], 'input': [],
            'toolOutput': {'name': 'background_event', 'namespace': 'laowu',
                           'output': json.dumps(payload, ensure_ascii=False), 'retain': True},
            'turnTrigger': 'laowu_event', 'clientUserMessageId': event['id']})
        return result['turn']['id']

    def receipt(self, event):
        """Inspect only bounded recent items to settle a lost send response.

        Unknown is deliberately not 'not delivered': do not blindly replay.
        """
        # Codex 0.163.0 publishes thread/items/list in its schema but returns
        # "not supported yet". The actually working API is paged turns/full.
        params = {'threadId': event['target_thread'], 'limit': 10,
                  'sortDirection': 'desc', 'itemsView': 'full'}
        cursor = None
        for _ in range(10):
            page = self.call('thread/turns/list', {**params, **({'cursor': cursor} if cursor else {})})
            for turn in page.get('data', []):
                matched = turn['id'] == event.get('turn_id')
                final = False
                for item in turn.get('items', []):
                    if item.get('type') == 'agentMessage' and item.get('phase') in (None, 'final_answer') and item.get('text'):
                        final = True
                    if item.get('type') == 'functionCallOutput' and item.get('name') == 'background_event':
                        try:
                            value = json.loads(item.get('output', ''))
                        except (ValueError, TypeError):
                            continue
                        matched = matched or (isinstance(value, dict) and value.get('event_id') == event['id'])
                if matched:
                    if turn['status'] == 'completed' and final:
                        return 'delivered', turn['id']
                    if turn['status'] in ('completed', 'failed', 'interrupted'):
                        return 'failed', turn['id']
                    return 'submitted', turn['id']
            cursor = page.get('nextCursor')
            if not cursor:
                break
        return 'uncertain', event.get('turn_id')
