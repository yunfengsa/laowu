"""File authorization, upload crash boundaries and real MCP image transport.

All network/file fixtures are synthetic and live only in temporary workspaces.
"""
import base64
import copy
import io
import importlib.util
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import struct
import zlib
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from test_slack_context import ContextFixture, ReadAPI
from laowu.access import SlackAccess
from laowu.context import Context, body_size, dumps
from laowu.service import Service, visible_tools
from laowu.slack_client import SlackAPI, SlackAPIError
from laowu.slack_context import SlackContextReader
from laowu.slack_files import (MAX_IMAGE, MAX_IMAGE_RESPONSE, MAX_TEXT, MAX_FILE,
                               PREFIX, TOOLS, SlackFiles, file_content, metadata)
from laowu.slack_read_proxy import SlackReadProxy, request_context
from laowu.store import Store
from laowu.slack_state import SlackState

def png_chunk(name, body):
    return struct.pack('>I', len(body)) + name + body + struct.pack('>I', zlib.crc32(name + body))


PNG = (b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
       + png_chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + png_chunk(b'IEND', b''))
HAS_PILLOW = importlib.util.find_spec('PIL') is not None


class FileAPI(ReadAPI):
    def __init__(self):
        super().__init__()
        self.data = '附件正文：测试\n'.encode()
        self.file = dict(id='FREAD', name='example.txt', mimetype='text/plain', size=len(self.data),
                         mode='hosted', channels=['CA'],
                         url_private='https://files.slack.com/files-pri/TA-FREAD/example.txt',
                         shares={'public': {'CA': [{'ts': '1.001'}]}})
        self.writes, self.downloads = [], []
        self.fail_at = None
        self.failure_uncertain = True
        self.shared = False
        self.on_write = None
        self.on_download = None

    def read_get(self, method, **args):
        if method != 'files.info':
            return super().read_get(method, **args)
        self.calls.append((method, args))
        if self.hook:
            self.hook(method, args)
        if args['file'] == 'FREAD':
            return {'file': copy.deepcopy(self.file)}
        if args['file'] == 'FOUT':
            return {'file': dict(id='FOUT', mode='hosted',
                                 permalink='https://example.slack.com/files/UBOT/FOUT/result.txt',
                                 shares={'public': {'CA': [{'thread_ts': '1.000001'}]}} if self.shared else {})}
        raise SlackAPIError('file_not_found')

    def download_file(self, url, *, limit):
        self.downloads.append((url, limit))
        if self.on_download:
            self.on_download()
        return self.data

    def upload_file(self, url, data):
        self._write('upload', {'url': url, 'size': len(data)})

    def _write(self, method, args):
        self.writes.append((method, args))
        if self.on_write:
            self.on_write(method)
        if method == self.fail_at:
            raise SlackAPIError('network_error' if self.failure_uncertain else 'missing_scope',
                                uncertain=self.failure_uncertain)

    def call(self, method, **args):
        self._write(method, args)
        if method == 'files.getUploadURLExternal':
            return {'ok': True, 'file_id': 'FOUT', 'upload_url': 'https://files.slack.com/upload/v1/synthetic'}
        if method == 'files.completeUploadExternal':
            self.shared = True
            return {'ok': True, 'files': [{'id': 'FOUT'}]}
        raise AssertionError(method)


class FileFixture(ContextFixture):
    def setUp(self):
        super().setUp()
        self.api = FileAPI()
        self.reader = SlackContextReader(self.state, self.api, clock=lambda: self.clock[0])

    def file_read(self, access=None, **args):
        return self.read('slack_file_read', access=access, file_id='FREAD', **args)

    def send(self, key='example', access=None, **args):
        return self.read('slack_file_send', access=access, idempotency_key=key,
                         **(args or {'filename': '结果.txt', 'text': '新的结果'}))

    def status(self, key='example', access=None):
        return self.read('slack_file_status', access=access, idempotency_key=key)

    def task(self, artifact, task_id='task_synthetic', access=None):
        access = access or self.access
        with self.state.store.transaction() as db:
            db.execute('INSERT INTO tasks(id,session_id,friend_id,client,request,workspace,status,result,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (task_id, 'session_fixture', 'engineer', 'codex', '', str(self.root), 'completed',
                        dumps({'result': 'synthetic final handoff', 'artifacts': [str(artifact)],
                               'validation': [], 'unfinished': [], 'memory_candidates': []}), 'hash', '2026-01-01', '2026-01-01'))
            db.execute('INSERT INTO slack_task_grants VALUES(?,?,?,?,?)',
                       (task_id, access.conversation_id, access.audience, '1.000001', 'input_fixture'))
        return {'task_id': task_id, 'artifact': str(artifact)}


class ReadTests(FileFixture):
    def test_text_and_bounded_metadata_no_urls_or_token(self):
        value = self.file_read()
        self.assertEqual(value['status'], 'text')
        self.assertEqual(value['text'], self.api.data.decode())
        self.assertEqual(value['file']['file_id'], 'FREAD')
        self.assertEqual(value['trust'], 'untrusted_reference_only')
        self.assertNotIn('https://', dumps(value))
        self.api.file['name'] = 'xoxb-synthetic-token'
        self.api.data = b'xoxb-synthetic-token'
        self.api.file['size'] = len(self.api.data)
        self.assertNotIn('xoxb-', dumps(self.file_read()))

    def test_metadata_history_discovery_and_limit(self):
        self.api.messages[0]['files'] = [self.api.file] * 12
        message = self.read()['messages'][0]
        self.assertEqual(len(message['files']), 10)
        self.assertTrue(message['files_truncated'])
        self.assertEqual(message['files'][0]['file_id'], 'FREAD')
        self.assertNotIn('url_private', dumps(message))
        self.assertEqual(metadata([{}, None, {'id': 'https://bad'}]), [])

    def test_private_or_other_dm_id_is_not_a_capability(self):
        self.api.file.update(channels=[], groups=['CP'], shares={'private': {'CP': [{'ts': '1.1'}]}})
        with self.assertRaisesRegex(PermissionError, 'file_share'):
            self.file_read()
        with self.assertRaisesRegex(PermissionError, 'audience_boundary'):
            self.file_read(channel_id='CP')
        self.assertEqual(self.api.downloads, [])
        self.assertEqual(self.file_read(access=self.dm, channel_id='CP')['status'], 'text')
        self.api.info['DX'] = dict(id='DX', is_im=True, user='UOTHER')
        self.api.file.update(ims=['DX'], groups=[], shares={})
        with self.assertRaises(PermissionError):
            self.file_read(access=self.dm, channel_id='DX')

    def test_revoke_membership_sharing_and_generation_during_read(self):
        actions = [lambda: self.api.members['CA'].discard('UOWNER'),
                   lambda: self.api.file.update(channels=[], shares={}),
                   lambda: self.state.update_channel('TA', 'CA', generation=1)]
        for action in actions:
            self.api.on_download = action
            with self.assertRaises(PermissionError):
                self.file_read()
            self.api.members['CA'].add('UOWNER')
            self.api.file.update(channels=['CA'])
            self.state.update_channel('TA', 'CA', generation=0)

    def test_unsupported_legacy_not_downloaded_and_sizes_not_silently_truncated(self):
        for changes, reason in [({'mimetype': 'application/msword'}, 'not_parsed'),
                                ({'size': MAX_TEXT + 1}, 'not_read'),
                                ({'size': MAX_FILE + 1}, 'not_read'),
                                ({'size': None}, 'not_read')]:
            old = dict(self.api.file)
            self.api.file.update(changes)
            self.assertEqual(self.file_read()['status'], reason)
            self.api.file = old
        self.assertEqual(self.api.downloads, [])
        self.api.data = b'\xff\xfeinvalid'
        self.api.file['size'] = len(self.api.data)
        self.assertEqual(self.file_read()['status'], 'not_parsed')
        self.api.file['size'] = 1
        with self.assertRaisesRegex(ValueError, 'file_changed'):
            self.file_read()

    def test_text_paging_actual_budget_and_version(self):
        self.api.data = ('中文\\"\n' * 8000).encode()
        self.api.file['size'] = len(self.api.data)
        value = self.file_read()
        self.assertLessEqual(body_size(value), 16384)
        self.assertFalse(value['text_complete'])
        self.assertNotIn('ref', value)
        combined = value['text']
        while value['next_offset'] is not None:
            value = self.file_read(text_offset=value['next_offset'], text_version=value['text_version'])
            combined += value['text']
        self.assertEqual(combined, self.api.data.decode())
        with self.assertRaisesRegex(ValueError, 'file_version'):
            self.file_read(text_offset=1)
        with self.assertRaisesRegex(ValueError, 'file_version'):
            self.file_read(text_offset=1, text_version='0' * 64)

    @unittest.skipUnless(HAS_PILLOW, 'install .[slack] Pillow to verify real image decoding')
    def test_image_real_content_and_distinct_budget(self):
        self.api.data = PNG + b'\x00' * 18000
        self.api.file.update(size=len(self.api.data), mimetype='image/png')
        value = self.file_read()
        content = file_content(self.state.store, value)
        self.assertEqual([v['type'] for v in content['content']], ['text', 'image'])
        self.assertTrue(base64.b64decode(content['content'][1]['data']).startswith(b'\x89PNG'))
        self.assertEqual(value['image_info']['original_width'], 1)
        self.assertLess(value['image_info']['output_bytes'], len(self.api.data))
        self.assertNotIn('image', json.loads(content['content'][0]['text']))
        self.assertLessEqual(len(dumps(content).encode()), MAX_IMAGE_RESPONSE)
        self.api.file['size'] = MAX_FILE + 1
        self.assertEqual(self.file_read()['status'], 'not_read')
        self.api.data = b'not png'
        self.api.file['size'] = len(self.api.data)
        self.assertEqual(self.file_read()['status'], 'not_parsed')

    @unittest.skipUnless(HAS_PILLOW, 'install .[slack] Pillow to verify real image decoding')
    def test_image_response_budget_rechecked_at_presentation(self):
        self.api.data = PNG
        self.api.file.update(size=len(PNG), mimetype='image/png')
        value = self.file_read()
        value['image']['data'] = 'A' * ((MAX_IMAGE + 2) // 3 * 4 + 1)
        with self.assertRaisesRegex(ValueError, 'file_image_budget'):
            file_content(self.state.store, value)
        value = self.file_read()
        value['reason'] = 'x' * 20000
        with self.assertRaisesRegex(ValueError, 'file_response_budget'):
            file_content(self.state.store, value)

    def test_worst_case_metadata_stays_bounded(self):
        self.api.file.update(name='😀' * 160, mimetype='a' * 40 + '/' + 'b' * 40)
        self.api.messages[0]['files'] = [self.api.file] * 10
        page = self.read()
        self.assertEqual(len(page['messages'][0]['files']), 10)
        self.assertLessEqual(body_size(page), 16384)


class SendTests(FileFixture):
    def test_external_upload_sequence_thread_receipt_and_no_payload_persistence(self):
        sent = self.send()
        self.assertEqual(sent['status'], 'sent')
        self.assertEqual(sent['file_id'], 'FOUT')
        self.assertTrue(sent['permalink'].startswith('https://example.slack.com/'))
        self.assertEqual([m for m, _ in self.api.writes],
                         ['files.getUploadURLExternal', 'upload', 'files.completeUploadExternal'])
        self.assertEqual(self.api.writes[-1][1]['channel_id'], 'CA')
        self.assertEqual(self.api.writes[-1][1]['thread_ts'], '1.000001')
        with self.state.store.connect() as db:
            ledger = ''.join(r[0] for r in db.execute('SELECT value FROM slack_meta WHERE key LIKE ?', (PREFIX + '%',)))
        for forbidden in ('新的结果', '结果.txt', 'upload/v1', 'xoxb-', self.access._token):
            self.assertNotIn(forbidden, ledger)
        self.assertEqual(self.send()['status'], 'sent')
        self.assertEqual(len(self.api.writes), 3)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.send(text='different', filename='结果.txt')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.send(key='different-key')

    def test_each_uncertain_write_stage_never_retries_even_after_restart(self):
        for stage in ('files.getUploadURLExternal', 'upload', 'files.completeUploadExternal'):
            self.api.fail_at = stage
            key = stage.replace('.', '-')
            args = dict(filename='result.txt', text=stage)
            value = self.send(key=key, **args)
            self.assertEqual(value['status'], 'uncertain')
            count = len(self.api.writes)
            self.assertEqual(self.send(key=key, **args)['status'], 'uncertain')
            self.reader = SlackContextReader(self.state, self.api)
            self.state.recover()
            self.access = self.binding('CA')
            self.assertEqual(self.status(key)['status'], 'uncertain')
            self.assertEqual(self.send(key=key, **args)['status'], 'uncertain')
            self.assertEqual(len(self.api.writes), count)
            # Restore a processing input only for the next, independent intent.
            self.state.update_input(self.state.inbox()[0]['id'], status='processing')

    def test_status_reconciles_only_matching_channel_and_thread_without_writes(self):
        self.api.fail_at = 'files.completeUploadExternal'
        self.assertEqual(self.send()['status'], 'uncertain')
        self.assertEqual(self.status()['status'], 'uncertain')
        self.api.shared = True  # server committed but response was lost
        count = len(self.api.writes)
        self.assertEqual(self.status()['status'], 'sent')
        self.assertEqual(len(self.api.writes), count)

    def test_wrong_thread_never_reconciles_and_confirmed_status_cannot_regress(self):
        self.api.fail_at = 'files.completeUploadExternal'
        self.send()
        original = self.api.read_get
        def wrong_thread(method, **args):
            value = original(method, **args)
            if method == 'files.info' and args['file'] == 'FOUT':
                value['file']['shares'] = {'public': {'CA': [{'thread_ts': '999.0'}]}}
            return value
        self.api.read_get = wrong_thread
        self.assertEqual(self.status()['status'], 'uncertain')
        self.api.read_get = original
        self.api.shared = True
        self.assertEqual(self.status()['status'], 'sent')
        handler = SlackFiles(self.reader)
        key = handler._key(self.access.validate(), 'example')
        self.assertEqual(handler._update(self.access, key, status='uncertain', error='late_timeout')['status'], 'sent')

    def test_generated_text_size_encoding_name_and_rename_dedup(self):
        with self.assertRaisesRegex(PermissionError, 'file_secret'):
            self.send(filename='sk-proj-' + 'a' * 30 + '.txt', text='ordinary content')
        for name, text in (('../file.txt', 'x'), ('.env', 'x'), ('a.txt', '中' * 3000),
                           ('a.py', 'print(1)'), ('a.txt', 'x\x00y')):
            with self.assertRaises(ValueError):
                self.send(filename=name, text=text)
        self.send()
        with self.assertRaisesRegex(ValueError, 'file_duplicate'):
            self.send(key='renamed', filename='renamed.txt', text='新的结果')

    def test_known_rejection_retained_and_new_principal_same_generation(self):
        self.api.fail_at = 'files.getUploadURLExternal'
        self.api.failure_uncertain = False
        self.assertEqual(self.send()['status'], 'failed')
        self.access = self.binding('CA')
        self.assertEqual(self.status()['error'], 'missing_scope')
        self.assertEqual(self.send()['status'], 'failed')
        self.assertEqual(len(self.api.writes), 1)

    def test_missing_completion_receipt_never_claims_sent(self):
        original = self.api.call
        def malformed(method, **args):
            value = original(method, **args)
            return {'ok': True} if method == 'files.completeUploadExternal' else value
        self.api.call = malformed
        self.assertEqual(self.send()['status'], 'uncertain')
        self.assertEqual(self.send()['error'], 'invalid_complete_response')
        self.assertEqual(len(self.api.writes), 3)

    def test_revoke_and_stop_before_complete_never_shares(self):
        def stop(method):
            if method == 'upload':
                self.state.update_input(self.state.inbox()[0]['id'], status='cancelled')
        self.api.on_write = stop
        self.assertEqual(self.send()['status'], 'uncertain')
        self.assertNotIn('files.completeUploadExternal', [m for m, _ in self.api.writes])

    def test_cross_source_generation_and_missing_active_input(self):
        self.send()
        self.assertEqual(self.status(access=self.dm)['status'], 'not_found')
        self.state.reset('TA', 'CA')
        self.access = self.binding('CA')
        with self.assertRaisesRegex(PermissionError, 'file_source'):
            self.status()
        with self.assertRaises(PermissionError):
            self.send(key='new')

    def test_concurrent_claim_one_upload(self):
        entered, release = threading.Event(), threading.Event()
        def wait(method):
            if method == 'files.getUploadURLExternal':
                entered.set()
                self.assertTrue(release.wait(3))
        self.api.on_write = wait
        result = []
        thread = threading.Thread(target=lambda: result.append(self.send()))
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            self.assertEqual(self.send()['status'], 'allocating')
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]['status'], 'sent')
        self.assertEqual(len(self.api.writes), 3)

    def test_registered_9_6mb_image_works_and_task_grant_is_required(self):
        folder = self.root / 'artifacts' / 'paintings'
        folder.mkdir(parents=True)
        art = folder / 'synthetic.png'
        art.write_bytes(PNG + b'\x00' * 9_600_000)
        args = self.task(art)
        self.assertEqual(self.send(**args)['status'], 'sent')
        self.assertEqual(self.api.writes[1][1]['size'], art.stat().st_size)
        with self.assertRaises(PermissionError):
            self.send(key='another-source', access=self.dm, **args)

    def test_path_allowlist_symlink_hardlink_traversal_and_non_artifacts(self):
        directory = self.root / 'artifacts'
        directory.mkdir()
        real = directory / 'file.txt'
        real.write_text('synthetic')
        link = directory / 'link.txt'
        link.symlink_to(real)
        hard = directory / 'hard.txt'
        os.link(real, hard)
        elsewhere = self.root / 'readme.txt'
        elsewhere.write_text('code')
        folder_link = directory / 'dir'
        folder_link.symlink_to(self.root, target_is_directory=True)
        candidates = [link, hard, real, elsewhere, str(directory / '..' / 'readme.txt'), folder_link / 'readme.txt']
        for i, path in enumerate(candidates):
            with self.subTest(path=path):
                args = self.task(path, task_id='task_' + str(i))
                with self.assertRaises(PermissionError):
                    self.send(key=str(i), **args)
        self.assertEqual(self.api.writes, [])

    def test_code_database_secrets_unregistered_and_cross_task_denied(self):
        directory = self.root / 'artifacts'
        directory.mkdir()
        for i, name in enumerate(('credentials.json', 'state.sqlite3', 'source.py', 'secret.txt')):
            path = directory / name
            path.write_text('xoxb-this-is-a-synthetic-secret')
            args = self.task(path, task_id='task_' + str(i))
            with self.assertRaises(PermissionError):
                self.send(key=str(i), **args)
        path = directory / 'good.txt'
        path.write_text('good')
        args = self.task(path, task_id='task_good')
        wrong = dict(args, artifact=str(directory / 'unknown.txt'))
        with self.assertRaisesRegex(PermissionError, 'artifact_grant'):
            self.send(**wrong)
        self.task(path, task_id='task_other')
        with self.assertRaisesRegex(PermissionError, 'artifact_owner'):
            self.send(**args)
        with self.assertRaisesRegex(PermissionError, 'file_secret'):
            self.send(text='xoxb-this-is-synthetic', filename='secret.txt')
        self.assertEqual(self.api.writes, [])

    def test_no_arbitrary_arguments_and_worker_or_native_denied(self):
        for args in ({'url': 'https://evil.test'}, {'path': '/etc/passwd'}, {'channel_id': 'CB'}, {'thread_ts': '2.0'}):
            with self.assertRaises(ValueError):
                self.send(**args)
        for worker, access in ((None, None), ('run', self.access)):
            service = Service.__new__(Service)
            service.access, service.worker_run = access, worker
            for method in TOOLS:
                with self.assertRaises(PermissionError):
                    service.call(method, {})
        self.assertTrue(TOOLS.isdisjoint({v[0] for v in visible_tools()}))


class RecoveryTests(FileFixture):
    def test_restore_snapshot_predating_slack_keeps_anonymous_send_tombstone(self):
        earlier = Store(self.root / 'before-slack')
        snapshot = earlier.backup()['path']
        self.send()
        self.state.update_input(self.state.inbox()[0]['id'], status='completed')
        self.state.store.restore(snapshot)
        self.state = SlackState(self.state.store)
        self.state.set_config(dict(enabled=True, team_id='TA', owner_user_id='UOWNER', bot_user_id='UBOT'))
        self.reader = SlackContextReader(self.state, self.api)
        self.access = self.binding('CA')
        with self.assertRaisesRegex(PermissionError, 'file_forgotten'):
            self.send()
        self.assertEqual(len(self.api.writes), 3)

    def test_imported_forgetting_tombstone_dominates_older_live_sent_state(self):
        from laowu.slack_files import merge_sends
        self.send()
        with self.state.store.connect() as db:
            row = db.execute('SELECT key,value FROM slack_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchone()
        imported = SlackState(Store(self.root / 'imported'))
        with imported.store.transaction() as db, self.state.store.connect() as current:
            db.execute('INSERT INTO slack_meta VALUES(?,?)', (row['key'], dumps({'status': 'forgotten'})))
            merge_sends(db, current)
            self.assertEqual(json.loads(db.execute('SELECT value FROM slack_meta WHERE key=?', (row['key'],)).fetchone()[0]),
                             {'status': 'forgotten'})

    def test_forget_erases_metadata_keeps_tombstone_and_no_resurrection(self):
        self.send()
        self.state.update_input(self.state.inbox()[0]['id'], status='completed')
        self.state.store.memory_forget('synthetic_memory')
        with self.state.store.connect() as db:
            records = [json.loads(r[0]) for r in db.execute('SELECT value FROM slack_meta WHERE key LIKE ?', (PREFIX + '%',))]
        self.assertEqual(records, [{'status': 'forgotten'}])
        self.access = self.binding('CA')
        with self.assertRaisesRegex(PermissionError, 'file_forgotten'):
            self.send()
        self.assertEqual(len(self.api.writes), 3)

    def test_restore_old_snapshot_retains_new_delivery_and_requires_new_source(self):
        self.state.update_input(self.state.inbox()[0]['id'], status='completed')
        snapshot = self.state.store.backup()['path']
        self.state.update_input(self.state.inbox()[0]['id'], status='processing')
        self.send()
        self.state.update_input(self.state.inbox()[0]['id'], status='completed')
        self.state.store.restore(snapshot)
        self.state.set_config({'enabled': True})
        self.access = self.binding('CA')
        with self.assertRaisesRegex(PermissionError, 'file_source'):
            self.send()
        self.assertEqual(self.state.status()['file_sends'], {'sent': 1})
        self.assertEqual(len(self.api.writes), 3)

    def test_restart_converts_inflight_to_uncertain_and_scopes_not_claimed(self):
        self.send()
        with self.state.store.transaction() as db:
            row = db.execute('SELECT key,value FROM slack_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchone()
            value = json.loads(row['value'])
            value['status'] = 'completing'
            db.execute('UPDATE slack_meta SET value=? WHERE key=?', (dumps(value), row['key']))
        self.api.shared = False
        self.state.recover()
        self.access = self.binding('CA')
        self.assertEqual(self.status()['status'], 'uncertain')
        self.assertEqual(self.state.status()['file_capabilities']['effective_token_scopes'], 'not_checked')


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.api = SlackAPI(client=SimpleNamespace(token='xoxb-synthetic', ssl=None))

    def response(self, data, headers=None, status=200):
        value = io.BytesIO(data)
        value.status = status
        value.headers = headers or {}
        return value

    def test_https_host_path_redirect_no_proxy_and_bearer_only_on_download(self):
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value = self.response(b'abc')
            self.assertEqual(self.api.download_file('https://files.slack.com/files-pri/TA-F/x.txt', limit=10), b'abc')
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header('Authorization'), 'Bearer xoxb-synthetic')
            self.assertIsNone(build.call_args.args[0].redirect_request(None, None, None, None, None, None))
            self.assertEqual(build.call_args.args[2].proxies, {})
            build.return_value.open.return_value = self.response(b'ok')
            self.api.upload_file('https://files.slack.com/upload/v1/opaque', b'abc')
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.method, 'POST')
            self.assertIsNone(request.get_header('Authorization'))
            self.assertEqual(request.data, b'abc')
        for url in ('http://files.slack.com/files-pri/x', 'https://127.0.0.1/files-pri/x',
                    'https://files.slack.com.evil.test/files-pri/x', 'https://user@files.slack.com/files-pri/x',
                    'https://files.slack.com:444/files-pri/x', 'https://files.slack.com/api/x',
                    'file:///etc/passwd', 'https://files.slack.com/files-pri/x#fragment'):
            with self.subTest(url=url), self.assertRaisesRegex(SlackAPIError, 'file_url_denied'):
                self.api.download_file(url, limit=10)

    def test_stream_bound_declared_length_encoding_and_redirect_fail_closed(self):
        with patch('urllib.request.build_opener') as build:
            for data, headers in ((b'abcd', {}), (b'abc', {'Content-Length': '100'}),
                                  (b'abc', {'Content-Encoding': 'gzip'})):
                build.return_value.open.return_value = self.response(data, headers)
                with self.assertRaises(SlackAPIError):
                    self.api.download_file('https://files.slack.com/files-pri/x', limit=3)
            build.return_value.open.side_effect = HTTPError('https://internal', 302, 'secret', {}, None)
            with self.assertRaisesRegex(SlackAPIError, 'file_http_302') as caught:
                self.api.download_file('https://files.slack.com/files-pri/x', limit=3)
            self.assertNotIn('internal', str(caught.exception))
            build.return_value.open.side_effect = URLError('secret')
            with self.assertRaises(SlackAPIError) as caught:
                self.api.upload_file('https://files.slack.com/upload/v1/x', b'data')
            self.assertTrue(caught.exception.uncertain)

    def test_upload_allocation_form_and_complete_5xx_uncertainty(self):
        client = MagicMock()
        client.api_call.return_value = {'ok': True}
        api = SlackAPI(client=client)
        api.call('files.getUploadURLExternal', filename='a.txt', length=1)
        self.assertEqual(client.api_call.call_args.kwargs, {'http_verb': 'POST', 'params': {'filename': 'a.txt', 'length': 1}})
        client.api_call.return_value = {'ok': False, 'error': 'internal_error'}
        with self.assertRaises(SlackAPIError) as caught:
            api.call('files.completeUploadExternal', files=[{'id': 'FOUT'}], channel_id='CA')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(client.api_call.call_args.kwargs,
                         {'json': {'files': [{'id': 'FOUT'}], 'channel_id': 'CA'}})


class MCPTests(FileFixture):
    @unittest.skipUnless(HAS_PILLOW, 'install .[slack] Pillow to verify real image decoding')
    def test_real_stdio_image_via_authenticated_socket_and_budget_no_context_ref(self):
        self.api.data = PNG + b'\x00' * 18000  # exceeds old 16KiB JSON transport
        self.api.file.update(size=len(self.api.data), mimetype='image/png')
        proxy = SlackReadProxy(self.reader)
        try:
            proxy.start()
        except PermissionError:
            self.skipTest('host sandbox forbids Unix socket bind; run on deployment host')
        self.addCleanup(proxy.close)
        token = self.state.issue_binding('TA', 'CA')
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'laowu.py'
        process = subprocess.Popen([sys.executable, str(script), '--root', str(self.root),
                                    'mcp', '--slack-token-file', str(token)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': 'slack_file_read', 'arguments': {'file_id': 'FREAD'}}}
            process.stdin.write((dumps(message) + '\n').encode())
            process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(10), 'MCP response timed out')
                result = json.loads(process.stdout.readline())['result']
            self.assertFalse(result['isError'], result)
            self.assertEqual([c['type'] for c in result['content']], ['text', 'image'])
            self.assertTrue(base64.b64decode(result['content'][1]['data']).startswith(b'\x89PNG'))
            self.assertEqual(json.loads(result['content'][0]['text'])['image_info']['original_width'], 1)
            self.assertNotIn('ref', json.loads(result['content'][0]['text']))
            self.assertLessEqual(len(dumps(result).encode()), MAX_IMAGE_RESPONSE)
        finally:
            process.terminate()
            process.communicate(timeout=10)
        with self.state.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM context_refs').fetchone()[0], 0)

    @unittest.skipUnless(HAS_PILLOW, 'install .[slack] Pillow to verify real image decoding')
    def test_real_stdio_image_content_without_socket_transport(self):
        # The managed test sandbox forbids bind(). Exercise actual JSON-RPC,
        # Service authorization and Context presentation with a stub gateway.
        self.api.data = PNG + b'\x00' * 18000
        self.api.file.update(size=len(self.api.data), mimetype='image/png')
        result = self.file_read()
        token = self.state.issue_binding('TA', 'CA')
        payload = self.root / 'synthetic-image-response.json'
        payload.write_text(dumps(result))
        code = ('import sys,json; from pathlib import Path; from unittest.mock import patch; '
                'from laowu.mcp import serve; '
                'result=json.loads(Path(sys.argv[3]).read_text()); '
                'stub=patch("laowu.slack_read_proxy.request_context", return_value=result); stub.start(); '
                'serve(sys.argv[1],slack_token_file=sys.argv[2])')
        process = subprocess.Popen([sys.executable, '-c', code, str(self.root), str(token), str(payload)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            request = dict(jsonrpc='2.0', id=1, method='tools/call',
                           params=dict(name='slack_file_read', arguments={'file_id': 'FREAD'}))
            process.stdin.write((dumps(request) + '\n').encode())
            process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(10))
                reply = json.loads(process.stdout.readline())['result']
            self.assertFalse(reply['isError'], reply)
            self.assertEqual([v['type'] for v in reply['content']], ['text', 'image'])
            self.assertTrue(base64.b64decode(reply['content'][1]['data']).startswith(b'\x89PNG'))
            self.assertLessEqual(len(dumps(reply).encode()), MAX_IMAGE_RESPONSE)
        finally:
            process.terminate()
            _, error = process.communicate(timeout=10)
            self.assertFalse(error, error)


if __name__ == '__main__':
    unittest.main()
