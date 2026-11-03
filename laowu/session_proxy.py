"""Optional CLI transport whose subscriptions provide exact presence leases.

Only connection metadata reaches Presence. JSON frames are forwarded in memory,
without changing initialization, authentication, model, or approval settings.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

from . import codex_bridge
from .background import Presence

LEASE_TTL = 30
REFRESH_SECONDS = 10
OPEN_TIMEOUT = 3
CLOSE_TIMEOUT = 0.5
MAX_SIZE = 16 * 1024 * 1024
SUBSCRIBE = frozenset(('thread/start', 'thread/resume', 'thread/fork'))


def _message(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _request_id(message):
    ident = message.get('id')
    # bool is a subclass of int, but isn't a JSON-RPC request ID.
    return (type(ident), ident) if type(ident) in (int, str) else None


def _abort(ws):
    if ws is not None:
        # Interrupt blocked recv/send as well as unresponsive close handshakes.
        # These sockets belong exclusively to this proxy connection.
        ws.close_socket()


class _Session:
    def __init__(self, proxy, client):
        self.proxy = proxy
        self.client = client
        self.upstream = None
        self.ident = 'chat:' + uuid.uuid4().hex
        self.serial = 0
        self.pending = {}
        self.leases = {}
        self.lock = threading.RLock()
        self.closed = False

    def request(self, raw):
        message = _message(raw)
        method, ident = message.get('method'), _request_id(message)
        if ident is None or method not in SUBSCRIBE | {'thread/unsubscribe'}:
            return
        params = message.get('params')
        thread = params.get('threadId') if isinstance(params, dict) else None
        with self.lock:
            if self.closed:
                return
            if len(self.pending) >= 1024 or ident in self.pending:
                raise ValueError('订阅请求过多或标识重复')
            self.pending[ident] = (method, thread)

    def response(self, raw):
        message = _message(raw)
        # Server-initiated requests can share IDs with client requests.
        if 'method' in message:
            return
        with self.lock:
            request = self.pending.pop(_request_id(message), None)
            if request is None or self.closed or 'error' in message or 'result' not in message:
                return
            method, thread = request
            if method == 'thread/unsubscribe':
                if isinstance(thread, str):
                    ident = self.leases.pop(thread, None)
                    if ident:
                        self.proxy.presence.disconnect(ident)
                return
            result = message['result']
            record = result.get('thread') if isinstance(result, dict) else None
            thread = record.get('id') if isinstance(record, dict) else None
            cwd = record.get('cwd') if isinstance(record, dict) else None
            if (not isinstance(thread, str) or not 1 <= len(thread) <= 200
                    or not isinstance(cwd, str) or not Path(cwd).is_absolute()
                    or Path(cwd).resolve() != self.proxy.root):
                # Don't deliver a successful subscription to a foreign project.
                # Closing our upstream connection releases only its subscription.
                raise ValueError('Codex 返回了本项目以外或无效的会话')
            previous = self.leases.pop(thread, None)
            if previous:
                self.proxy.presence.disconnect(previous)
            self.serial += 1
            # A new subscription gets a new connection generation. Presence's
            # upsert deliberately preserves connected_at when refreshing an ID.
            ident = f'{self.ident}:{self.serial}:{thread}'
            self.leases[thread] = ident
            self.proxy.presence.connect(ident, thread, mode='lease', ttl=LEASE_TTL)

    def refresh(self):
        with self.lock:
            if not self.closed:
                for thread, ident in self.leases.items():
                    self.proxy.presence.connect(ident, thread, mode='lease', ttl=LEASE_TTL)

    def close(self):
        # Abort transport before taking the metadata lock, unblocking any send.
        _abort(self.client)
        _abort(self.upstream)
        with self.lock:
            if self.closed:
                return
            self.closed = True
            failure = None
            try:
                for ident in self.leases.values():
                    try:
                        self.proxy.presence.disconnect(ident)
                    except Exception as error:
                        failure = error
            finally:
                self.leases.clear()
                self.pending.clear()
            if failure:
                self.proxy.failed.set()
                raise failure

    def run(self):
        upstream = unix_connect(self.proxy.upstream_path, open_timeout=OPEN_TIMEOUT,
                                close_timeout=CLOSE_TIMEOUT, max_size=MAX_SIZE)
        with self.lock:
            self.upstream = upstream
            if self.closed:
                _abort(upstream)
                return
        reader = threading.Thread(target=self._responses, name='laowu-chat-responses')
        reader.start()
        next_refresh = time.monotonic() + REFRESH_SECONDS
        try:
            while not self.proxy.stopping.is_set():
                try:
                    raw = self.client.recv(timeout=0.25)
                except TimeoutError:
                    pass
                else:
                    self.request(raw)
                    upstream.send(raw)
                if time.monotonic() >= next_refresh:
                    self.refresh()
                    next_refresh = time.monotonic() + REFRESH_SECONDS
        finally:
            self.close()
            reader.join(timeout=OPEN_TIMEOUT + 1)

    def _responses(self):
        try:
            for raw in self.upstream:
                self.response(raw)
                self.client.send(raw)
        except ConnectionClosed:
            pass
        except Exception:
            # Surface failure without logging raw upstream messages or user data.
            self.proxy.failed.set()
        finally:
            self.close()


class _Proxy:
    def __init__(self, store, upstream_path):
        self.root = Path(store.root).resolve()
        self.presence = Presence(store)
        self.upstream_path = str(upstream_path)
        self.stopping = threading.Event()
        self.failed = threading.Event()
        self.condition = threading.Condition()
        self.sessions = set()
        self.temporary = self.server = self.listener = None

    def __enter__(self):
        try:
            # Don't use project cwd or TMPDIR: Chinese UTF-8 paths can exceed
            # sockaddr_un's 108-byte limit. TemporaryDirectory is private (0700).
            self.temporary = tempfile.TemporaryDirectory(prefix='laowu-chat-', dir='/tmp')
            self.path = str(Path(self.temporary.name) / 'ws.sock')
            self.server = unix_serve(self._handle, self.path, open_timeout=1,
                                     close_timeout=CLOSE_TIMEOUT, max_size=MAX_SIZE)
            os.chmod(self.path, 0o600)
            self.listener = threading.Thread(target=self.server.serve_forever,
                                             name='laowu-chat-listener')
            self.listener.start()
            return self
        except BaseException:
            self.close()
            raise

    def _handle(self, client):
        session = _Session(self, client)
        with self.condition:
            if self.stopping.is_set():
                _abort(client)
                return
            self.sessions.add(session)
        try:
            session.run()
        except ConnectionClosed:
            pass
        except Exception:
            self.failed.set()
        finally:
            try:
                session.close()
            finally:
                with self.condition:
                    self.sessions.discard(session)
                    self.condition.notify_all()

    def close(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        if self.server:
            self.server.shutdown()
        with self.condition:
            sessions = list(self.sessions)
        for session in sessions:
            try:
                session.close()
            except Exception:
                self.failed.set()
        if self.listener:
            self.listener.join(timeout=1)
        with self.condition:
            self.condition.wait_for(lambda: not self.sessions, timeout=OPEN_TIMEOUT + 1)
        if self.temporary:
            self.temporary.cleanup()

    def __exit__(self, *args):
        self.close()


def _stop_child(process):
    if process is None or process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        process.wait(timeout=2)


def run_chat(store, extra_args: list[str]) -> int:
    """Run the user's Codex CLI against a private, supervised presence proxy."""
    # An extra transport/cwd flag could bypass the proxy or its project boundary.
    for arg in extra_args:
        if arg == '--':
            break
        if arg in ('--remote', '--cd', '-C') or arg.startswith(('--remote=', '--cd=', '-C')):
            print('laowu chat: --remote 和 -C/--cd 由项目连接入口管理。', file=sys.stderr)
            return 2
    upstream_path = codex_bridge.socket_path()
    try:
        # Fail before starting a CLI. Never start/restart a global app server.
        with unix_connect(str(upstream_path), open_timeout=OPEN_TIMEOUT,
                          close_timeout=CLOSE_TIMEOUT, max_size=MAX_SIZE):
            pass
    except Exception:
        print('laowu chat: 无法连接现有 Codex Unix WebSocket 上游；请先启用 Codex App Server。',
              file=sys.stderr)
        return 2

    process = None
    previous_handlers = {}
    received_signal = []

    def interrupted(signum, frame):
        received_signal.append(signum)

    try:
        with _Proxy(store, upstream_path) as proxy:
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    previous_handlers[signum] = signal.signal(signum, interrupted)
            process = subprocess.Popen(['codex', '--remote', f'unix://{proxy.path}',
                                        '-C', str(proxy.root), *extra_args], cwd=str(proxy.root))
            while True:
                if received_signal:
                    return 128 + received_signal[0]
                if proxy.failed.is_set():
                    print('laowu chat: 连接代理已关闭（上游、订阅项目或租约校验失败）。', file=sys.stderr)
                    return 1
                try:
                    return process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    continue
    except KeyboardInterrupt:
        return 130
    except Exception:
        print('laowu chat: 无法启动或维持 Codex 连接入口。', file=sys.stderr)
        return 2
    finally:
        try:
            _stop_child(process)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
