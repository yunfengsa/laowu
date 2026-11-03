"""Real local Unix WebSocket transports; no Codex daemon or model calls."""
import contextlib
import io
import json
import os
import queue
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

from laowu import session_proxy
from laowu.background import Presence
from laowu.store import Store


def eventually(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('local transport did not settle before deadline')


class LocalUpstream:
    def __init__(self, path, root, handler=None):
        self.path = str(path)
        self.root = str(root)
        self.handler = handler
        self.received = queue.Queue()
        self.condition = threading.Condition()
        self.connections = set()
        self.closed = False
        self.server = unix_serve(self.accept, self.path, open_timeout=1, close_timeout=0.2)
        self.listener = threading.Thread(target=self.server.serve_forever)
        self.listener.start()

    def accept(self, ws):
        with self.condition:
            if self.closed:
                ws.close_socket()
                return
            self.connections.add(ws)
        try:
            if self.handler:
                self.handler(ws)
                return
            for raw in ws:
                self.received.put(raw)
                message = json.loads(raw)
                method = message.get('method')
                if 'id' not in message or not method:
                    continue
                params = message.get('params', {})
                if params.get('fail'):
                    response = {'id': message['id'], 'error': {'code': -32000}}
                elif method in session_proxy.SUBSCRIBE | {'thread/read'}:
                    thread = params.get('threadId', 'thread-started')
                    response = {'id': message['id'], 'result': {'thread': {
                        'id': thread, 'cwd': params.get('cwd', self.root)}}}
                elif method == 'thread/loaded/list':
                    response = {'id': message['id'], 'result': {'data': ['loaded-only']}}
                else:
                    response = {'id': message['id'], 'result': {}}
                ws.send(json.dumps(response))
        except ConnectionClosed:
            pass
        finally:
            with self.condition:
                self.connections.discard(ws)
                self.condition.notify_all()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.server.shutdown()
        self.listener.join(timeout=2)
        with self.condition:
            connections = list(self.connections)
        for ws in connections:
            ws.close_socket()
        with self.condition:
            if not self.condition.wait_for(lambda: not self.connections, timeout=4):
                raise AssertionError('fake upstream connection leaked')
        if self.listener.is_alive():
            raise AssertionError('fake upstream listener leaked')


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / ('中文项目' * 12) / '老五'
        self.store = Store(self.root)
        self.upstream = LocalUpstream(self.base / 'upstream.sock', self.root)
        self.addCleanup(self.upstream.close)
        self.proxy = session_proxy._Proxy(self.store, self.upstream.path).__enter__()
        self.addCleanup(self.proxy.close)

    def client(self, proxy=None):
        ws = unix_connect((proxy or self.proxy).path, open_timeout=2, close_timeout=0.2)
        self.addCleanup(ws.close)
        return ws

    def call(self, ws, method, ident=1, **params):
        ws.send(json.dumps({'id': ident, 'method': method, 'params': params}))
        return json.loads(ws.recv(timeout=3))

    def rows(self, active=False):
        with contextlib.closing(self.store.connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM connections' +
                    (" WHERE state='active'" if active else '') + ' ORDER BY rowid')]

    def test_handshake_and_raw_bidirectional_json_including_approval(self):
        self.proxy.close()
        self.upstream.close()
        request = ' { "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "中文 CLI"}} } '
        response = ' { "id" : 1, "result" : {"userAgent":"unchanged"} } '
        approval = '{ "id":"approval-1", "method":"item/commandExecution/requestApproval", "params":{} }'
        approved = '{ "id":"approval-1", "result":{"decision":"decline"} }'
        seen = queue.Queue()

        def handshake(ws):
            try:
                seen.put(ws.recv(timeout=3))
                ws.send(response)
                ws.send(approval)
                seen.put(ws.recv(timeout=3))
                seen.put(ws.recv(timeout=3))
                ws.send(b'{"method":"binary-notice"}')
                for _ in ws:
                    pass
            except ConnectionClosed:
                pass

        replacement = LocalUpstream(self.base / 'raw.sock', self.root, handshake)
        self.addCleanup(replacement.close)
        proxy = session_proxy._Proxy(self.store, replacement.path).__enter__()
        self.addCleanup(proxy.close)
        ws = self.client(proxy)
        ws.send(request)
        self.assertEqual(ws.recv(timeout=3), response)
        self.assertEqual(ws.recv(timeout=3), approval)
        ws.send(' {"method":"initialized"} ')
        ws.send(approved)
        self.assertEqual(ws.recv(timeout=3), b'{"method":"binary-notice"}')
        self.assertEqual([seen.get(timeout=2) for _ in range(3)],
                         [request, ' {"method":"initialized"} ', approved])
        self.assertEqual(self.rows(), [])

    def test_successful_start_resume_fork_only_register_response_thread(self):
        ws = self.client()
        for ident, method in enumerate(sorted(session_proxy.SUBSCRIBE), 1):
            thread = f'thread-{ident}'
            self.call(ws, method, ident, threadId=thread)
            self.assertEqual({r['thread_id'] for r in self.rows(True)},
                             {f'thread-{n}' for n in range(1, ident + 1)})
        for row in self.rows(True):
            self.assertEqual(row['mode'], 'lease')
            self.assertTrue(28 <= row['expires_at'] - time.time() <= 30)

    def test_failed_subscription_loaded_read_and_unsolicited_response_do_not_register(self):
        ws = self.client()
        self.call(ws, 'thread/start', fail=True)
        self.call(ws, 'thread/loaded/list', 2)
        self.call(ws, 'thread/read', 3, threadId='read-only')
        # Receiving a matching-looking result without its request isn't presence.
        with self.proxy.condition:
            session = next(iter(self.proxy.sessions))
        session.response(json.dumps({'id': 9, 'result': {'thread': {
            'id': 'unsolicited', 'cwd': str(self.root)}}}))
        self.assertEqual(self.rows(), [])

    def test_unsubscribe_success_only_and_repeated_subscription_new_time(self):
        ws = self.client()
        self.call(ws, 'thread/start', threadId='same')
        first = self.rows(True)[0]
        self.call(ws, 'thread/unsubscribe', 2, threadId='same', fail=True)
        self.assertEqual(len(self.rows(True)), 1)
        self.call(ws, 'thread/unsubscribe', 3, threadId='same')
        self.assertEqual(self.rows(True), [])
        self.call(ws, 'thread/resume', 4, threadId='same')
        second = self.rows(True)[0]
        self.assertNotEqual(first['id'], second['id'])
        self.assertGreaterEqual(second['connected_at'], first['connected_at'])
        self.assertEqual(self.rows()[0]['state'], 'closed')

    def test_heartbeat_extends_lease_without_refreshing_connection_time(self):
        with patch.object(session_proxy, 'REFRESH_SECONDS', 0.05):
            ws = self.client()
            self.call(ws, 'thread/start', threadId='heartbeat')
            first = self.rows(True)[0]
            eventually(lambda: self.rows(True)[0]['expires_at'] > first['expires_at'])
            second = self.rows(True)[0]
            self.assertEqual(second['connected_at'], first['connected_at'])
            self.assertEqual(second['id'], first['id'])
            self.assertEqual(len(self.rows()), 1)

    def test_disconnect_clears_all_owned_leases(self):
        ws = self.client()
        self.call(ws, 'thread/start', threadId='a')
        self.call(ws, 'thread/resume', 2, threadId='b')
        ws.close()
        eventually(lambda: not self.rows(True))
        self.assertEqual(len(self.rows()), 2)

    def test_parallel_clients_same_thread_and_native_presence_are_isolated(self):
        Presence(self.store).connect('native-unrelated', 'other-thread')
        other_proxy = session_proxy._Proxy(self.store, self.upstream.path).__enter__()
        self.addCleanup(other_proxy.close)
        first, second = self.client(), self.client(other_proxy)
        self.call(first, 'thread/resume', threadId='shared-thread')
        self.call(second, 'thread/resume', threadId='shared-thread')
        self.assertNotEqual(self.proxy.path, other_proxy.path)
        self.assertEqual(len(self.rows(True)), 3)
        self.call(first, 'thread/unsubscribe', 2, threadId='shared-thread')
        self.assertEqual(len(self.rows(True)), 2)
        self.proxy.close()
        self.assertEqual(len(self.rows(True)), 2)
        self.call(second, 'thread/resume', 2, threadId='still-connected')
        second.close()
        eventually(lambda: len(self.rows(True)) == 1)
        self.assertEqual(self.rows(True)[0]['id'], 'native-unrelated')

    def test_multiple_websockets_on_one_proxy_are_isolated(self):
        first, second = self.client(), self.client()
        self.call(first, 'thread/start', threadId='shared')
        self.call(second, 'thread/resume', threadId='shared')
        self.assertEqual(len(self.rows(True)), 2)
        first.close()
        eventually(lambda: len(self.rows(True)) == 1)
        self.call(second, 'thread/unsubscribe', 2, threadId='shared')
        self.assertEqual(self.rows(True), [])

    def test_long_chinese_root_and_tmpdir_use_short_private_socket(self):
        self.assertGreater(len(os.fsencode(self.root)), 108)
        with patch.dict(os.environ, {'TMPDIR': str(self.root)}):
            with session_proxy._Proxy(self.store, self.upstream.path) as proxy:
                path = Path(proxy.path)
                self.assertLess(len(os.fsencode(path)), 108)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                ws = self.client(proxy)
                self.call(ws, 'thread/start', threadId='long-root')
                self.assertEqual(len(self.rows(True)), 1)
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
            self.assertEqual(self.rows(True), [])

    def test_foreign_missing_relative_cwd_fail_closed(self):
        for cwd in (str(self.root.parent), str(self.root) + '-other', None, '.', ''):
            with self.subTest(cwd=cwd):
                ws = self.client()
                ws.send(json.dumps({'id': 1, 'method': 'thread/resume',
                                    'params': {'threadId': 'foreign', 'cwd': cwd}}))
                with self.assertRaises(ConnectionClosed):
                    ws.recv(timeout=3)
                eventually(lambda: not self.rows(True))
        self.assertEqual(self.rows(), [])

    def test_out_of_order_results_and_server_request_id_collision(self):
        ws = self.client()
        eventually(lambda: bool(self.proxy.sessions))
        with self.proxy.condition:
            session = next(iter(self.proxy.sessions))
        # The observation methods are also exercised by real transport tests.
        session.request('{"id":10,"method":"thread/start"}')
        session.request('{"id":"11","method":"thread/fork"}')
        session.response('{"id":10,"method":"server/request","params":{}}')
        self.assertEqual(self.rows(), [])
        session.response(json.dumps({'id': '11', 'result': {'thread': {
            'id': 'fork-result-id', 'cwd': str(self.root)}}}))
        session.response('{"id":10,"error":{"code":-1}}')
        self.assertEqual([r['thread_id'] for r in self.rows(True)], ['fork-result-id'])

    def test_upstream_disconnect_closes_client_and_leases(self):
        ws = self.client()
        self.call(ws, 'thread/start', threadId='upstream-closed')
        self.upstream.close()
        with self.assertRaises(ConnectionClosed):
            ws.recv(timeout=3)
        eventually(lambda: not self.rows(True))

    def test_shutdown_is_bounded_for_unresponsive_clients_and_cleans_threads(self):
        ws = self.client()
        self.call(ws, 'thread/start', threadId='idle')
        path = Path(self.proxy.path)
        start = time.monotonic()
        self.proxy.close()
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual(self.rows(True), [])
        self.assertFalse(path.parent.exists())
        eventually(lambda: not any(t.name.startswith('laowu-chat-')
                                    for t in threading.enumerate()))


class RunChatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store = Store(self.base / '老五')
        self.upstream = LocalUpstream(self.base / 'upstream.sock', self.store.root)
        self.addCleanup(self.upstream.close)
        self.socket_patch = patch.object(session_proxy.codex_bridge, 'socket_path',
                                         return_value=Path(self.upstream.path))
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)

    def rows(self):
        with contextlib.closing(self.store.connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM connections')]

    def test_cli_arguments_defaults_real_child_exit_and_lease_cleanup(self):
        # A supervised Python child acts as the CLI, using the real proxy socket.
        child = '''
import json,sys,time
from websockets.sync.client import unix_connect
ws = unix_connect(sys.argv[1].removeprefix('unix://'), close_timeout=0.2)
ws.send(json.dumps({'id':1,'method':'initialize','params':{}}))
ws.recv(timeout=3)
ws.send(json.dumps({'id':2,'method':'thread/start','params':{'threadId':'child'}}))
ws.recv(timeout=3)
time.sleep(0.1)
sys.exit(17)
'''
        actual_popen = subprocess.Popen
        launched = []

        def launch(argv, **kwargs):
            launched.append((argv, kwargs))
            return actual_popen([sys.executable, '-c', child, argv[2]], **kwargs)

        extra = ['--model', 'user-selected', '--', '中文提问']
        with patch.object(session_proxy.subprocess, 'Popen', side_effect=launch):
            result = session_proxy.run_chat(self.store, extra)
        self.assertEqual(result, 17)
        argv, kwargs = launched[0]
        self.assertEqual(argv[:2], ['codex', '--remote'])
        self.assertEqual(argv[3:], ['-C', str(self.store.root), *extra])
        self.assertEqual(kwargs, {'cwd': str(self.store.root)})
        self.assertFalse(Path(argv[2].removeprefix('unix://')).parent.exists())
        self.assertEqual([(r['thread_id'], r['state']) for r in self.rows()], [('child', 'closed')])
        self.assertEqual(extra, ['--model', 'user-selected', '--', '中文提问'])

    def test_missing_upstream_is_explicit_and_never_launches_cli(self):
        with patch.object(session_proxy.codex_bridge, 'socket_path', return_value=self.base / 'absent.sock'):
            with patch.object(session_proxy.subprocess, 'Popen') as launch, contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(session_proxy.run_chat(self.store, []), 2)
        launch.assert_not_called()
        self.assertIn('现有 Codex Unix WebSocket 上游', error.getvalue())

    def test_missing_codex_cleans_proxy_and_reports_error(self):
        with patch.object(session_proxy.subprocess, 'Popen', side_effect=FileNotFoundError) as launch:
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(session_proxy.run_chat(self.store, []), 2)
        path = Path(launch.call_args.args[0][2].removeprefix('unix://'))
        self.assertFalse(path.parent.exists())
        self.assertIn('无法启动', error.getvalue())

    def test_transport_and_cwd_overrides_cannot_bypass_proxy(self):
        for args in (['--remote', 'unix:///other'], ['--remote=unix:///other'],
                     ['-C', '/other'], ['-C/other'], ['--cd=/other']):
            with self.subTest(args=args), patch.object(session_proxy.subprocess, 'Popen') as launch:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(session_proxy.run_chat(self.store, args), 2)
                launch.assert_not_called()

    def test_sigterm_stops_and_reaps_only_owned_child_and_restores_handlers(self):
        actual_popen = subprocess.Popen
        children = []
        previous = signal.getsignal(signal.SIGTERM)
        child = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'

        def launch(argv, **kwargs):
            process = actual_popen([sys.executable, '-c', child], **kwargs)
            children.append(process)
            # Call the installed handler without sending a signal to the test runner.
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return process

        started = time.monotonic()
        with patch.object(session_proxy.subprocess, 'Popen', side_effect=launch):
            self.assertEqual(session_proxy.run_chat(self.store, []), 128 + signal.SIGTERM)
        self.assertLess(time.monotonic() - started, 6)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


class LeaseStateTests(unittest.TestCase):
    """SQLite state checks that don't require permission to bind sockets."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / '中文项目')
        self.proxy = session_proxy._Proxy(self.store, '/unused-upstream.sock')
        self.session = self.new_session()

    def new_session(self):
        session = session_proxy._Session(self.proxy, Mock())
        self.addCleanup(session.close)
        return session

    def rows(self, active=False):
        with contextlib.closing(self.store.connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM connections' +
                    (" WHERE state='active'" if active else '') + ' ORDER BY rowid')]

    def subscribe(self, session, method='thread/start', ident=1, thread='thread', cwd=None):
        session.request(json.dumps({'id': ident, 'method': method, 'params': {}}))
        session.response(json.dumps({'id': ident, 'result': {'thread': {
            'id': thread, 'cwd': str(self.store.root) if cwd is None else cwd}}}))

    def test_three_subscription_methods_require_matching_success(self):
        for ident, method in enumerate(sorted(session_proxy.SUBSCRIBE), 1):
            self.subscribe(self.session, method, ident, str(ident))
        self.assertEqual(len(self.rows(True)), 3)
        self.session.request('{"id":9,"method":"thread/start"}')
        self.session.response('{"id":9,"error":{"code":-1}}')
        self.session.response(json.dumps({'id': 100, 'result': {'thread': {
            'id': 'unsolicited', 'cwd': str(self.store.root)}}}))
        self.assertEqual(len(self.rows(True)), 3)

    def test_response_thread_id_and_out_of_order_server_id_collision(self):
        self.session.request('{"id":1,"method":"thread/fork","params":{"threadId":"parent"}}')
        self.session.request('{"id":"1","method":"thread/resume"}')
        self.session.response('{"id":1,"method":"approval","params":{}}')
        self.assertEqual(self.rows(), [])
        self.session.response(json.dumps({'id': '1', 'result': {'thread': {
            'id': 'resumed', 'cwd': str(self.store.root)}}}))
        self.session.response(json.dumps({'id': 1, 'result': {'thread': {
            'id': 'forked-child', 'cwd': str(self.store.root)}}}))
        self.assertEqual([r['thread_id'] for r in self.rows(True)], ['resumed', 'forked-child'])

    def test_loaded_read_and_notification_are_not_subscription(self):
        for ident, method in enumerate(('thread/loaded/list', 'thread/read', 'initialized'), 1):
            self.subscribe(self.session, method, ident)
        self.session.response('{"method":"thread/started","params":{"thread":{"id":"notice"}}}')
        self.assertEqual(self.rows(), [])

    def test_invalid_project_reply_cannot_create_lease(self):
        for index, cwd in enumerate((str(self.store.root.parent), '.', '', '/other-project'), 1):
            with self.subTest(cwd=cwd), self.assertRaises(ValueError):
                self.subscribe(self.session, ident=index, cwd=cwd)
        self.assertEqual(self.rows(), [])

    def test_heartbeat_preserves_connected_at_and_renews_expiry(self):
        self.subscribe(self.session)
        first = self.rows(True)[0]
        with patch('laowu.background.time.time', return_value=first['expires_at']):
            self.session.refresh()
        second = self.rows(True)[0]
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['connected_at'], second['connected_at'])
        self.assertEqual(second['expires_at'], first['expires_at'] + 30)
        self.assertEqual(session_proxy.REFRESH_SECONDS, 10)
        self.assertEqual(session_proxy.LEASE_TTL, 30)

    def test_resubscribe_changes_time_only_on_subscription_event(self):
        self.subscribe(self.session)
        first = self.rows(True)[0]
        with patch('laowu.background.now', return_value='2099-01-01T00:00:00+00:00'):
            self.subscribe(self.session, 'thread/resume', 2)
        second = self.rows(True)[0]
        self.assertNotEqual(first['id'], second['id'])
        self.assertNotEqual(first['connected_at'], second['connected_at'])
        self.assertEqual(self.rows()[0]['state'], 'closed')

    def test_successful_unsubscribe_and_close_only_touch_owned_leases(self):
        other = self.new_session()
        self.subscribe(self.session)
        self.subscribe(other)
        Presence(self.store).connect('native', 'another-thread')
        self.session.request('{"id":2,"method":"thread/unsubscribe","params":{"threadId":"thread"}}')
        self.session.response('{"id":2,"error":{"code":-1}}')
        self.assertEqual(len(self.rows(True)), 3)
        self.session.request('{"id":3,"method":"thread/unsubscribe","params":{"threadId":"thread"}}')
        self.session.response('{"id":3,"result":{}}')
        self.assertEqual(len(self.rows(True)), 2)
        self.session.close()
        other.refresh()
        self.assertEqual(len(self.rows(True)), 2)
        other.close()
        self.assertEqual([r['id'] for r in self.rows(True)], ['native'])

    def test_transport_overrides_rejected_before_connect(self):
        with patch.object(session_proxy, 'unix_connect') as connect, contextlib.redirect_stderr(io.StringIO()):
            for args in (['--remote=unix:///elsewhere'], ['-C', '/elsewhere'], ['--cd=/elsewhere']):
                self.assertEqual(session_proxy.run_chat(self.store, args), 2)
        connect.assert_not_called()

    def test_unavailable_upstream_returns_error_without_starting_child(self):
        with patch.object(session_proxy, 'unix_connect', side_effect=PermissionError), \
                patch.object(session_proxy.subprocess, 'Popen') as child, \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(session_proxy.run_chat(self.store, []), 2)
        child.assert_not_called()
        self.assertIn('现有 Codex Unix WebSocket 上游', error.getvalue())

    def test_failed_server_start_removes_private_temp_directory(self):
        with patch.object(session_proxy, 'unix_serve', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.proxy.__enter__()
        self.assertFalse(Path(self.proxy.path).parent.exists())

    def test_child_cleanup_escalates_and_waits_for_owned_process(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('owned-child', 2), 0]
        session_proxy._stop_child(child)
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_count, 2)

    def test_run_chat_preserves_explicit_arguments_and_inherited_defaults(self):
        proxy = SimpleNamespace(root=self.store.root, path='/tmp/test-proxy.sock', failed=threading.Event())
        extra = ['resume', '--last', '--', '--remote is prompt text']
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        with patch.object(session_proxy, 'unix_connect'), patch.object(session_proxy, '_Proxy') as factory, \
                patch.object(session_proxy.subprocess, 'Popen') as launch:
            factory.return_value.__enter__.return_value = proxy
            launch.return_value.wait.return_value = 23
            self.assertEqual(session_proxy.run_chat(self.store, extra), 23)
        launch.assert_called_once_with(['codex', '--remote', 'unix:///tmp/test-proxy.sock',
                                        '-C', str(self.store.root), *extra], cwd=str(self.store.root))
        factory.return_value.__exit__.assert_called_once()
        self.assertEqual(previous, {sig: signal.getsignal(sig) for sig in previous})

    def test_run_chat_signal_cleans_child_and_restores_handlers(self):
        proxy = SimpleNamespace(root=self.store.root, path='/tmp/test-proxy.sock', failed=threading.Event())
        child = Mock()
        child.poll.return_value = None
        previous = signal.getsignal(signal.SIGTERM)

        def launch(*args, **kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return child

        with patch.object(session_proxy, 'unix_connect'), patch.object(session_proxy, '_Proxy') as factory, \
                patch.object(session_proxy.subprocess, 'Popen', side_effect=launch):
            factory.return_value.__enter__.return_value = proxy
            self.assertEqual(session_proxy.run_chat(self.store, []), 143)
        child.terminate.assert_called_once_with()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_real_supervised_child_ignoring_sigterm_is_killed_and_reaped(self):
        child = subprocess.Popen([sys.executable, '-u', '-c',
            'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready"); time.sleep(30)'],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(session_proxy._stop_child, child)
        self.addCleanup(child.stdout.close)
        readable, _, _ = select.select([child.stdout], [], [], 3)
        self.assertTrue(readable, 'supervised child did not initialize')
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        start = time.monotonic()
        session_proxy._stop_child(child)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(child.returncode, -signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
