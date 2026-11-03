"""Bounded local transport; only the gateway possesses Slack API credentials."""
import errno
import json
import os
import socket
import socketserver
import stat
import threading

from .access import SlackAccess
from .context import dumps
from .slack_context import ContextReadError, MAX_RESPONSE, REQUEST_SECONDS

MAX_REQUEST = 12 * 1024


def socket_path(store):
    return store.private / 'slack' / 'context.sock'


def _receive(stream, limit):
    data = stream.readline(limit + 1)
    if len(data) > limit or not data.endswith(b'\n'):
        raise ValueError('read_frame: 请求/响应过长或不完整')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('read_frame: 只接受 JSON 对象')
    return value


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 4

    def __init__(self, path, reader):
        self.reader = reader
        self.slots = threading.BoundedSemaphore(2)
        super().__init__(str(path), _Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.settimeout(1)
            try:
                request.sendall(b'{"error":"read_busy: local read concurrency exceeded; retry later"}\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # No traceback/request/opaque principal logging.
        pass


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            request = _receive(self.rfile, MAX_REQUEST)
            if set(request) != {'principal', 'method', 'arguments'}:
                raise ValueError('read_frame: 不允许身份、URL、执行或其他附加参数')
            result = self.server.reader.read(request['principal'], request['method'], request['arguments'])
            SlackAccess.from_token(self.server.reader.store, request['principal']).validate()
            reply = dumps({'data': result}).encode() + b'\n'
            if len(reply) > MAX_RESPONSE:
                raise ValueError('read_frame: 响应预算超限')
        except Exception as exc:
            message = str(exc) if isinstance(exc, (PermissionError, ValueError, ContextReadError)) else 'read_failed: 网关读取失败，请检查状态'
            reply = dumps({'error': message[:2000]}).encode() + b'\n'
        try:
            self.wfile.write(reply)
        except OSError:
            pass


class SlackReadProxy:
    def __init__(self, reader):
        self.reader = reader
        self.path = socket_path(reader.store)
        self.server = None
        self.thread = None
        self.identity = None

    def start(self):
        self.reader.store._safe(self.path)
        if self.path.exists():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise PermissionError('read_socket: 既有路径不是本用户 socket')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as check:
                check.settimeout(1)
                try:
                    check.connect(str(self.path))
                except OSError as exc:
                    if exc.errno != errno.ECONNREFUSED:
                        raise
                else:
                    raise RuntimeError('read_socket: 已有网关读代理运行')
            self.path.unlink()
        self.server = _Server(self.path, self.reader)
        info = self.path.lstat()
        self.identity = (info.st_dev, info.st_ino)
        try:
            self.path.chmod(0o600)
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.1},
                                           name='laowu-slack-context')
            self.thread.start()
        except Exception:
            self.server.server_close()
            self.server = None
            self.path.unlink()
            self.identity = None
            raise

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
            self.server = None
        if self.identity:
            try:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self.identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self.identity = None


def request_context(access, method, arguments):
    """MCP knows only its own opaque principal, never xoxb/xapp or a URL."""
    access.validate()
    path = socket_path(access.store)
    access.store._safe(path)
    try:
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('read_socket: 读代理权限无效')
        message = dumps({'principal': access._token, 'method': method, 'arguments': arguments}).encode() + b'\n'
        if len(message) > MAX_REQUEST:
            raise ValueError('read_frame: 请求过大')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(REQUEST_SECONDS + 5)
            connection.connect(str(path))
            connection.sendall(message)
            with connection.makefile('rb') as stream:
                result = _receive(stream, MAX_RESPONSE)
    except OSError:
        raise ContextReadError('read_proxy_unavailable: 网关只读代理未运行、超时或不可达；请由本机维护会话检查/重载网关。尚未读取历史。') from None
    access.validate()
    if 'error' in result:
        raise ContextReadError(result['error'])
    return result['data']
