"""Small official Slack SDK wrapper; credentials stay in the connector process.

The API deliberately distinguishes a known rejection from an uncertain network
result. A timeout after chat.postMessage is never permission to post it again.
"""
from __future__ import annotations

import re


# Match the official WebClient methods, rather than guessing from a namespace:
# conversations also contains writes, while chat/views/agents need nested JSON.
_CONVERSATION_READS = frozenset({
    'conversations.info', 'conversations.members',
    'conversations.history', 'conversations.replies',
})


class SlackAPIError(RuntimeError):
    def __init__(self, code, *, retry_after=None, uncertain=False):
        # Do not include SDK exception text: it can contain credentials/payloads.
        self.code = re.sub(r'[^a-zA-Z0-9_.-]', '_', str(code))[:100]
        self.retry_after = retry_after
        self.uncertain = uncertain
        super().__init__('Slack API: ' + self.code)


class SlackAPI:
    def __init__(self, token=None, *, client=None):
        if client is None:
            from slack_sdk import WebClient
            # No SDK retry of a write after ambiguous network failures.
            client = WebClient(token=token, timeout=10, retry_handlers=[])
        self.client = client

    def call(self, method, **params):
        from .retries import remaining
        budget=remaining(10)
        client=self.client
        if budget < 10 and isinstance(getattr(client,'timeout',None),(int,float)):
            # Do not change another channel's shared WebClient timeout.
            import copy
            client=copy.copy(client)
            client.timeout=min(client.timeout,budget)
        try:
            if method in _CONVERSATION_READS:
                # SDK sync transport turns GET/params into POST/form. Passing
                # json instead bypasses its query/form and boolean encoding.
                response = client.api_call(method, http_verb='GET', params=params)
            elif method == 'auth.test':
                response = client.api_call(method, params=params)
            else:
                response = client.api_call(method, json=params)
            data = dict(response.data if hasattr(response, 'data') else response)
            if not data.get('ok', False):
                code = data.get('error', 'api_rejected')
                raise SlackAPIError(code, uncertain=method.startswith('chat.') and code in
                                    ('internal_error', 'fatal_error', 'request_timeout', 'service_unavailable'))
            return data
        except SlackAPIError:
            raise
        except Exception as exc:
            response = getattr(exc, 'response', None)
            if response is not None:
                data = getattr(response, 'data', {}) or {}
                status = getattr(response, 'status_code', 0)
                headers = getattr(response, 'headers', {}) or {}
                if status == 429 or data.get('error') == 'ratelimited':
                    retry = headers.get('Retry-After', headers.get('retry-after', 30))
                    if isinstance(retry, list):
                        retry = retry[0]
                    try:
                        retry = min(3600, max(1, float(retry)))
                    except (ValueError, TypeError):
                        retry = 30
                    raise SlackAPIError('ratelimited', retry_after=retry) from None
                if data.get('error') or status >= 500:
                    code = data.get('error') or ('http_' + str(status) if status >= 500 else 'api_rejected')
                    uncertain = method.startswith('chat.') and (status >= 500 or code in
                                ('internal_error', 'fatal_error', 'request_timeout', 'service_unavailable'))
                    raise SlackAPIError(code, uncertain=uncertain) from None
            from urllib.error import URLError
            code = 'network_error' if isinstance(exc,(TimeoutError,ConnectionError,URLError)) else 'transport_uncertain'
            raise SlackAPIError(code, uncertain=True) from None

    def info(self, channel):
        return self.call('conversations.info', channel=channel)['channel']

    def read_get(self, method, **params):
        """Actual GET, fixed host, no redirects/retries or payload logging.

        The synchronous SDK rewrites GET to POST. Keep its write/reconciliation
        transport intact and use this bounded transport only for context reads.
        """
        import json
        import math
        from urllib.error import HTTPError, URLError
        from urllib.parse import urlencode
        from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler
        from .retries import remaining
        if method not in _CONVERSATION_READS | {'auth.test', 'conversations.list'}:
            raise PermissionError('Slack 上下文只允许固定只读 API')

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        query = {k: ('true' if v else 'false') if isinstance(v, bool) else str(v)
                 for k, v in params.items()}
        request = Request('https://slack.com/api/' + method + '?' + urlencode(query),
                          headers={'Authorization': 'Bearer ' + self.client.token}, method='GET')
        proxy = getattr(self.client, 'proxy', None)
        opener = build_opener(NoRedirect(), HTTPSHandler(context=getattr(self.client, 'ssl', None)),
                              ProxyHandler({'https': proxy} if proxy else {}))
        try:
            with opener.open(request, timeout=remaining(8)) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            remaining(8)
            if len(raw) > 2 * 1024 * 1024:
                raise SlackAPIError('context_page_too_large')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise SlackAPIError('invalid_response')
            if not data.get('ok'):
                raise SlackAPIError(data.get('error', 'api_rejected'))
            return data
        except HTTPError as exc:
            exc.close()
            if exc.code == 429:
                try:
                    delay = float(exc.headers.get('Retry-After', '30'))
                    delay = min(3600, max(1, delay)) if math.isfinite(delay) else 3600
                except (TypeError, ValueError):
                    delay = 30
                raise SlackAPIError('ratelimited', retry_after=delay) from None
            raise SlackAPIError('http_' + str(exc.code)) from None
        except URLError as exc:
            import errno
            import socket
            import ssl
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise SlackAPIError('tls_verification_failed') from None
            network = isinstance(reason, (TimeoutError, ConnectionError, socket.gaierror)) or (
                isinstance(reason, OSError) and reason.errno in {
                    errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT,
                    errno.ENETUNREACH, errno.EHOSTUNREACH})
            raise SlackAPIError('network_error' if network else 'transport_uncertain') from None
        except (TimeoutError, ConnectionError):
            raise SlackAPIError('network_error') from None
        except (ValueError, UnicodeError):
            raise SlackAPIError('invalid_response') from None

    def members(self, channel):
        members, cursor = set(), None
        for _ in range(100):
            page = self.call('conversations.members', channel=channel, limit=200,
                             **({'cursor': cursor} if cursor else {}))
            members.update(page.get('members', []))
            cursor = page.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                return members
        raise SlackAPIError('members_pagination_limit')

    def status(self, channel, thread_ts, status, owner=None):
        params = {'channel_id': channel, 'status': status}
        if thread_ts:
            params['thread_ts'] = thread_ts
        if owner:
            params['initiator_user_id'] = owner
        return self.call('agents.sessions.setStatus', **params)

    def post(self, channel, text, *, thread_ts=None, key=None, blocks=None):
        params = {'channel': channel, 'text': text, 'unfurl_links': False,
                  'unfurl_media': False}
        if thread_ts:
            params['thread_ts'] = thread_ts
        if key:
            params['metadata'] = {'event_type': 'laowu_delivery',
                                  'event_payload': {'key': key}}
        if blocks is not None:
            params['blocks'] = blocks
        return self.call('chat.postMessage', **params)

    def history(self, channel, *, oldest, thread_ts=None):
        method = 'conversations.replies' if thread_ts else 'conversations.history'
        params = {'channel': channel, 'oldest': str(oldest), 'inclusive': True,
                  'limit': 100, 'include_all_metadata': True}
        if thread_ts:
            params['ts'] = thread_ts
        cursor = None
        for _ in range(100):
            page = self.call(method, **params, **({'cursor': cursor} if cursor else {}))
            yield from page.get('messages', [])
            cursor = page.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                if page.get('has_more'):
                    raise SlackAPIError('history_incomplete')
                return
        raise SlackAPIError('history_pagination_limit')

    def reconcile(self, channel, *, key, oldest, thread_ts=None):
        for message in self.history(channel, oldest=oldest, thread_ts=thread_ts):
            metadata = message.get('metadata', {})
            if (metadata.get('event_type') == 'laowu_delivery' and
                    metadata.get('event_payload', {}).get('key') == key):
                return message.get('ts')
        # Absence is intentionally not proof that a timeout did not post.
        return None


def split_message(text, limit=3500):
    """Bound Slack text while keeping fenced code balanced in each part."""
    if not isinstance(text, str):
        raise TypeError('Slack回复必须为文字')
    if limit < 100:
        raise ValueError('消息分片上限过小')
    if not text:
        return []
    pieces, active, prefix = [], '', ''
    remaining = text
    while remaining:
        allowance = limit - len(prefix) - 4
        if len(remaining) <= allowance:
            body, remaining = remaining, ''
        else:
            cut = remaining.rfind('\n', 0, allowance)
            if cut < allowance // 2:
                cut = allowance
            else:
                cut += 1
            # Never cut through a triple-backtick marker.
            while cut > 0 and remaining[max(0, cut - 2):cut + 2].count('`') >= 3:
                cut -= 1
            if not cut:
                cut = allowance
            body, remaining = remaining[:cut], remaining[cut:]
        for match in re.finditer(r'```([^\n`]*)', body):
            active = '' if active else (match.group(1).strip()[:32] or ' ')
        suffix = '\n```' if active else ''
        pieces.append(prefix + body + suffix)
        prefix = ('```' + active.strip() + '\n') if active else ''
    return pieces
