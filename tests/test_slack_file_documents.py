"""Document/image resource boundaries and terminal handoff delivery regression."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

from laowu.context import body_size, dumps
from laowu.slack_file_parser import MIMES, MAX_INPUT, parse_file
from laowu.slack_files import MAX_IMAGE, MAX_IMAGE_RESPONSE, file_content
from laowu.slack_read_proxy import SlackReadProxy
from slack_file_fixtures import docx, xlsx, pdf, png, replace_member, package
from test_slack_files import FileFixture, PNG, HAS_PILLOW

HAS_XML = importlib.util.find_spec('defusedxml') is not None
HAS_DOCX = HAS_XML and importlib.util.find_spec('docx') is not None
HAS_XLSX = HAS_XML and importlib.util.find_spec('openpyxl') is not None
HAS_PDF = importlib.util.find_spec('pypdf') is not None
HAS_ALL = HAS_DOCX and HAS_XLSX and HAS_PDF and HAS_PILLOW
MIME = {v: k for k, v in MIMES.items()}


class CompatibilityTests(FileFixture):
    def test_slack_native_snippet_and_remote_rejection(self):
        self.api.data = b't' * 160
        self.api.file.update(mode='snippet', file_access='visible', is_external=False, size=160)
        self.assertEqual(self.file_read()['text'], 't' * 160)
        for extra in ({'is_external': True}, {'mode': 'external'}, {'mode': 'remote'}, {'file_access': 'access_denied'}):
            old = self.api.file.copy()
            with self.subTest(extra=extra):
                self.api.file.update(extra)
                with self.assertRaisesRegex(PermissionError, 'file_unavailable'):
                    self.file_read()
                self.api.file = old
        self.assertEqual(len(self.api.downloads), 1)

    def artifact(self):
        path = self.root / 'artifacts' / 'terminal.png'
        path.parent.mkdir()
        path.write_bytes(PNG + b'\0' * 9_600_000)
        return self.task(path)

    def state_task(self, status, result=None):
        with self.state.store.transaction() as db:
            db.execute('UPDATE tasks SET status=? WHERE id=?', (status, 'task_synthetic'))
            if result is not None:
                db.execute('UPDATE tasks SET result=? WHERE id=?', (result, 'task_synthetic'))

    def test_blocked_valid_handoff_9_6mb_and_idempotency(self):
        args = self.artifact()
        self.state_task('blocked')
        self.assertEqual(self.send(**args)['status'], 'sent')
        self.assertEqual(self.send(**args)['status'], 'sent')
        self.assertEqual(len(self.api.writes), 3)
        self.assertGreater(self.api.writes[1][1]['size'], 9_600_000)
        with self.state.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM tasks WHERE id=?', (args['task_id'],)).fetchone()[0], 'blocked')
        with self.assertRaises(PermissionError):
            self.send(key='cross-origin', access=self.dm, **args)

    def test_running_queued_failed_checkpoint_and_malformed_handoff_denied(self):
        args = self.artifact()
        for status in ('running', 'queued', 'failed', 'interrupted', 'cancelled'):
            self.state_task(status)
            with self.subTest(status=status), self.assertRaisesRegex(PermissionError, 'artifact_grant'):
                self.send(**args)
        with self.state.store.transaction() as db:
            db.execute('UPDATE tasks SET checkpoint=?', (dumps({'artifacts': [args['artifact']]}),))
        for result in ('{}', 'null', '[]', '{bad', dumps({'artifacts': [args['artifact']]})):
            self.state_task('blocked', result)
            with self.subTest(result=result), self.assertRaisesRegex(PermissionError, 'artifact_grant'):
                self.send(**args)
        self.assertEqual(self.api.writes, [])

    def test_terminal_status_with_active_or_cleanup_pending_run_denied(self):
        args = self.artifact()
        self.state_task('blocked')
        with self.state.store.transaction() as db:
            db.execute('INSERT INTO runs(id,task_id,session_id,friend_id,friend_version,client,status,can_write,workspace,spec_json,token_hash,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('run_fixture', args['task_id'], 'session_fixture', 'engineer', 1, 'codex', 'running', 1, str(self.root), '{}', 'synthetic', 'now', 'now'))
        with self.assertRaisesRegex(PermissionError, 'artifact_grant'):
            self.send(**args)
        with self.state.store.transaction() as db:
            db.execute("UPDATE runs SET status='blocked'")
            db.execute("INSERT INTO run_cleanup VALUES('run_fixture')")
        with self.assertRaisesRegex(PermissionError, 'artifact_grant'):
            self.send(**args)
        self.assertEqual(self.api.writes, [])

    def test_resume_after_allocation_stops_without_upload_and_never_retries(self):
        args = self.artifact()
        self.state_task('blocked')
        self.api.on_write = lambda stage: self.state_task('running')
        self.assertEqual(self.send(**args)['status'], 'uncertain')
        self.state_task('blocked')
        self.assertEqual(self.send(**args)['status'], 'uncertain')
        self.assertEqual([m for m, _ in self.api.writes], ['files.getUploadURLExternal'])

    def test_missing_parsers_are_explicit_and_no_bytes_persisted(self):
        self.api.data = pdf()
        self.api.file.update(mimetype=MIME['pdf'], size=len(self.api.data))
        with patch('laowu.slack_files.parse_file', return_value={'error': 'parser_dependencies_missing: install .[slack]'}):
            result = self.file_read()
        self.assertEqual(result['status'], 'not_parsed')
        self.assertIn('parser_dependencies_missing', result['reason'])
        with self.state.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM context_refs').fetchone()[0], 0)


class ParserProcessTests(unittest.TestCase):
    def test_timeout_launch_failure_and_child_crash_are_explicit(self):
        for failure, reason in ((subprocess.TimeoutExpired('parser', 15), 'parser_timeout'),
                                (OSError('synthetic'), 'parser_unavailable')):
            with patch('subprocess.run', side_effect=failure) as run:
                self.assertIn(reason, parse_file(b'synthetic', 'pdf')['error'])
                self.assertEqual(run.call_args.kwargs['env'], {'PATH': os.defpath, 'LANG': 'C.UTF-8'})
                self.assertEqual(run.call_args.args[0][1], '-I')
                self.assertTrue(run.call_args.kwargs['close_fds'])
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], -9, b'')):
            self.assertIn('parser_resource_limit', parse_file(b'synthetic', 'pdf')['error'])

    def test_oversize_input_rejected_before_child(self):
        with patch('subprocess.run') as run:
            self.assertIn('input_limit', parse_file(b'x' * (MAX_INPUT + 1), 'pdf')['error'])
            run.assert_not_called()

    @unittest.skipIf(HAS_PDF, 'missing dependency branch only applies without pypdf')
    def test_real_child_missing_dependency(self):
        self.assertIn('parser_dependencies_missing', parse_file(pdf(), 'pdf')['error'])


class DocumentTests(FileFixture):
    def load(self, raw, kind):
        self.api.data = raw
        self.api.file.update(size=len(raw), mimetype=MIME.get(kind, kind), name='synthetic.' + kind)

    def full_text(self):
        page = self.file_read()
        self.assertEqual(page['status'], 'text', page)
        pieces = [page['text']]
        count = 1
        while page['next_offset'] is not None:
            self.assertLessEqual(body_size(page), 16384)
            page = self.file_read(text_offset=page['next_offset'], text_version=page['text_version'])
            pieces.append(page['text'])
            count += 1
            self.assertLess(count, 100)
        self.assertTrue(page['text_complete'])
        return ''.join(pieces), page, count

    @unittest.skipUnless(HAS_PDF, 'install .[slack] pypdf')
    def test_pdf_actual_text_pages_scans_page_limit_encrypted_and_bad_format(self):
        self.load(pdf(), 'pdf')
        text, page, _ = self.full_text()
        self.assertIn('PDF evidence alpha', text)
        self.assertIn('[Page 2]', text)
        self.assertIn('PDF evidence omega', text)
        self.assertEqual(page['document']['pages'], 2)
        self.load(pdf(('',)), 'pdf')
        self.assertEqual(self.file_read()['document']['pages_without_text'], [1])
        self.assertEqual(self.file_read()['document']['ocr'], 'not_performed')
        self.load(pdf(('too many',) * 201), 'pdf')
        self.assertIn('pdf_limit', self.file_read()['reason'])
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        writer.add_page(PdfReader(io.BytesIO(pdf())).pages[0])
        writer.encrypt('synthetic-password')
        buffer = io.BytesIO()
        writer.write(buffer)
        self.load(buffer.getvalue(), 'pdf')
        self.assertIn('pdf_encrypted', self.file_read()['reason'])
        self.load(b'%PDF-1.4\ninvalid', 'pdf')
        self.assertEqual(self.file_read()['status'], 'not_parsed')

    @unittest.skipUnless(HAS_DOCX, 'install .[slack] python-docx/defusedxml')
    def test_docx_real_body_table_full_pagination_and_version(self):
        # Distinct text keeps compression ratio within the documented limit.
        body = ''.join(f'{i}:中文paragraph-{i * 77};\n' for i in range(1500))
        self.load(docx(body), 'docx')
        text, _, count = self.full_text()
        self.assertIn(body, text)
        self.assertIn('DOCX表格 omega', text)
        self.assertGreater(count, 1)
        first = self.file_read()
        self.load(docx('changed'), 'docx')
        with self.assertRaisesRegex(ValueError, 'file_version'):
            self.file_read(text_offset=first['next_offset'], text_version=first['text_version'])

    @unittest.skipUnless(HAS_XLSX, 'install .[slack] openpyxl/defusedxml')
    def test_xlsx_all_sheets_coordinates_formula_and_untrusted_dimensions(self):
        self.load(xlsx(), 'xlsx')
        text, page, _ = self.full_text()
        for evidence in ('XLSX正文证据 alpha', 'B2 (value): 42', 'C2 (formula): "=SUM(B2,1)"',
                         'tail omega', 'hidden evidence', '[Sheet "Hidden"; hidden]'):
            self.assertIn(evidence, text)
        self.assertEqual(page['document']['sheets'], 2)
        self.assertIn('cached_values_not_used', page['document']['formulas'])

    @unittest.skipUnless(HAS_XLSX, 'install .[slack] openpyxl/defusedxml')
    def test_xlsx_full_text_pagination_across_all_rows(self):
        rows = ''.join(f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>row-{i}-evidence</t></is></c></row>' for i in range(4, 700))
        self.load(xlsx(extra=rows), 'xlsx')
        text, _, count = self.full_text()
        self.assertGreater(count, 1)
        for i in range(4, 700):
            self.assertEqual(text.count(f'"row-{i}-evidence"'), 1)
        self.assertIn('hidden evidence', text)

    @unittest.skipUnless(HAS_PDF, 'install .[slack] pypdf')
    def test_pdf_full_text_pagination(self):
        body = ''.join(f'{i}-PDF-evidence;' for i in range(1800))
        self.load(pdf((body,)), 'pdf')
        text, _, count = self.full_text()
        self.assertIn(body, text)
        self.assertGreater(count, 1)

    @unittest.skipUnless(HAS_XLSX, 'install .[slack] openpyxl/defusedxml')
    def test_xlsx_row_column_limits_and_false_dimensions_do_not_truncate(self):
        for coordinate in ('A10001', 'IW3', 'ZZZ3'):
            self.load(xlsx(coordinate=coordinate), 'xlsx')
            self.assertEqual(self.file_read()['status'], 'not_parsed')
            self.assertIn('sheet_limit', self.file_read()['reason'])

    @unittest.skipUnless(HAS_XLSX, 'install .[slack] openpyxl/defusedxml')
    def test_malformed_duplicate_or_misordered_rows_cannot_hide_text(self):
        raw = xlsx()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            body = archive.read('xl/worksheets/sheet1.xml')
        for damaged in (body.replace(b'<c r="C2">', b'<c r="B2">'),
                        body.replace(b'<row r="3">', b'<row r="2">'),
                        body.replace(b'<c r="A3"', b'<c r="A2"')):
            self.load(replace_member(raw, 'xl/worksheets/sheet1.xml', damaged), 'xlsx')
            result = self.file_read()
            self.assertEqual(result['status'], 'not_parsed')
            self.assertIn('sheet_format', result['reason'])

    @unittest.skipUnless(HAS_XML, 'install .[slack] defusedxml')
    def test_zip_bombs_traversal_macros_entities_and_invalid_format(self):
        good = docx()
        candidates = [replace_member(good, 'word/bomb.xml', b'x' * 1000000),
                      replace_member(good, '../outside', 'bad'),
                      replace_member(good, 'word/vbaProject.bin', 'macro'),
                      replace_member(good, 'word/document.xml', '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'),
                      replace_member(good, 'word/document.xml', '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE x [<!ENTITY e "expanded">]><x>&e;</x>'.encode('utf-16')),
                      b'not zip', xlsx()]
        for raw in candidates:
            with self.subTest(size=len(raw)):
                self.load(raw, 'docx')
                result = self.file_read()
                self.assertEqual(result['status'], 'not_parsed', result)
        many = {f'item{i}': '' for i in range(1025)}
        self.load(package(many), 'docx')
        self.assertIn('zip_limit', self.file_read()['reason'])

    @unittest.skipUnless(HAS_XML, 'install .[slack] defusedxml')
    def test_duplicate_zip_member_and_decompression_size_budgets(self):
        import warnings
        from laowu.slack_file_parser import checked_zip, Rejected
        buffer = io.BytesIO(docx())
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(buffer, 'a') as archive:
                archive.writestr('word/document.xml', b'duplicate')
        self.load(buffer.getvalue(), 'docx')
        self.assertIn('zip_limit', self.file_read()['reason'])
        # Exercise separate member/total bounds without allocating 64MiB fixtures.
        for setting in ('MAX_ZIP_MEMBER', 'MAX_ZIP_TOTAL'):
            with patch('laowu.slack_file_parser.' + setting, 100):
                with self.assertRaisesRegex(Rejected, 'zip_limit'):
                    checked_zip(docx(), 'docx')

    @unittest.skipUnless(HAS_DOCX, 'install .[slack] python-docx/defusedxml')
    def test_extracted_text_limit_is_explicit_without_partial_body(self):
        self.load(docx(os.urandom(2 * 1024 * 1024 + 1).hex()), 'docx')
        result = self.file_read()
        self.assertEqual(result['status'], 'not_parsed')
        self.assertIn('text_limit', result['reason'])
        self.assertNotIn('text', result)

    @unittest.skipUnless(HAS_DOCX, 'install .[slack] python-docx/defusedxml')
    def test_external_relationship_is_never_opened(self):
        raw = replace_member(docx(), 'word/_rels/document.xml.rels',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="http://127.0.0.1:1/private" TargetMode="External"/></Relationships>')
        self.load(raw, 'docx')
        # Local unreachable relationship must neither fail nor inject content.
        self.assertIn('DOCX正文证据', self.file_read()['text'])

    @unittest.skipUnless(HAS_XLSX, 'install .[slack] openpyxl/defusedxml')
    def test_external_formula_is_literal_and_never_evaluated(self):
        raw = xlsx()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            body = archive.read('xl/worksheets/sheet1.xml').replace(b'SUM(B2,1)', b'WEBSERVICE(&quot;http://127.0.0.1:1/private&quot;)')
        self.load(replace_member(raw, 'xl/worksheets/sheet1.xml', body), 'xlsx')
        result = self.file_read()
        self.assertEqual(result['status'], 'text', result)
        self.assertIn('WEBSERVICE', result['text'])
        self.assertIn('literal_not_evaluated', result['document']['formulas'])

    @unittest.skipUnless(HAS_DOCX and HAS_XLSX, 'install .[slack] office parsers')
    def test_office_artifact_sending_secret_scan_and_source(self):
        directory = self.root / 'artifacts'
        directory.mkdir()
        for kind, make in (('docx', docx), ('xlsx', xlsx)):
            path = directory / ('generated.' + kind)
            path.write_bytes(make())
            args = self.task(path, task_id='task_' + kind)
            self.assertEqual(self.send(key=kind, **args)['status'], 'sent')
            self.assertEqual(self.send(key=kind, **args)['status'], 'sent')
            with self.assertRaises(PermissionError):
                self.send(key='cross-' + kind, access=self.dm, **args)
            secret = directory / ('credential.' + kind)
            secret.write_bytes(make('xoxb-this-is-a-synthetic-secret'))
            args = self.task(secret, task_id='task_secret_' + kind)
            with self.assertRaisesRegex(PermissionError, 'file_secret'):
                self.send(key='secret-' + kind, **args)
        self.assertEqual(len(self.api.writes), 6)

    @unittest.skipUnless(HAS_DOCX, 'install .[slack] python-docx/defusedxml')
    def test_compressed_utf16_attributes_and_split_runs_cannot_hide_credentials(self):
        directory = self.root / 'artifacts'
        directory.mkdir()
        raw = docx()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            document = archive.read('word/document.xml')
        cases = [replace_member(raw, 'word/secret.xml',
                    '<?xml version="1.0" encoding="utf-16"?><x value="xoxb-this-is-synthetic-secret"/>'.encode('utf-16')),
                 replace_member(raw, 'word/document.xml', document.replace('DOCX正文证据 alpha'.encode(),
                    b'xoxb-</w:t></w:r><w:r><w:t>this-is-synthetic-secret'))]
        for i, data in enumerate(cases):
            path = directory / f'case{i}.docx'
            path.write_bytes(data)
            args = self.task(path, task_id='task_case' + str(i))
            with self.assertRaisesRegex(PermissionError, 'file_secret'):
                self.send(key='case' + str(i), **args)
        self.assertEqual(self.api.writes, [])

    @unittest.skipUnless(HAS_PILLOW, 'install .[slack] Pillow')
    def test_real_large_image_safe_decode_budget_dimensions_and_pixel_bomb(self):
        raw = png()
        self.assertGreater(len(raw), MAX_IMAGE)
        self.load(raw, 'image/png')
        result = self.file_read()
        self.assertEqual(result['status'], 'image', result)
        image = base64.b64decode(result['image']['data'])
        from PIL import Image
        decoded = Image.open(io.BytesIO(image))
        decoded.load()
        info = result['image_info']
        self.assertEqual((info['original_width'], info['original_height']), (1300, 1200))
        self.assertEqual(decoded.size, (info['output_width'], info['output_height']))
        self.assertLessEqual(len(image), MAX_IMAGE)
        self.assertLessEqual(len(dumps(file_content(self.state.store, result)).encode()), MAX_IMAGE_RESPONSE)
        # A valid original at the full 20MiB boundary is decoded, never echoed.
        self.load(PNG + b'\0' * (MAX_INPUT - len(PNG)), 'image/png')
        boundary = self.file_read()
        self.assertEqual(boundary['status'], 'image', boundary)
        self.assertEqual(boundary['image_info']['input_bytes'], MAX_INPUT)
        self.assertLess(boundary['image_info']['output_bytes'], 1000)
        self.api.file['size'] = MAX_INPUT + 1
        self.assertEqual(self.file_read()['status'], 'not_read')
        for bad in (png(10000, 10000, pixels=False), b'not image', PNG[:40]):
            self.load(bad, 'image/png')
            self.assertEqual(self.file_read()['status'], 'not_parsed')

    @unittest.skipUnless(HAS_PDF, 'install .[slack] pypdf')
    def test_share_revoked_during_parsing_is_rechecked(self):
        self.load(pdf(), 'pdf')
        actual = parse_file
        def revoked(*args, **kwargs):
            result = actual(*args, **kwargs)
            self.api.file.update(channels=[], shares={})
            return result
        with patch('laowu.slack_files.parse_file', side_effect=revoked):
            with self.assertRaises(PermissionError):
                self.file_read()

    def mcp_roundtrip(self, process, ident):
        message = {'jsonrpc': '2.0', 'id': ident, 'method': 'tools/call',
                   'params': {'name': 'slack_file_read', 'arguments': {'file_id': 'FREAD'}}}
        process.stdin.write((dumps(message) + '\n').encode())
        process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(25), 'MCP response timed out')
            result = json.loads(process.stdout.readline())['result']
        self.assertFalse(result['isError'], result)
        return result

    @unittest.skipUnless(HAS_ALL, 'install .[slack] all file parsers')
    def test_real_stdio_documents_without_socket_transport(self):
        # Socket-restricted hosts can still verify actual MCP JSON-RPC content.
        # The gateway results are real parses, only local transport is substituted.
        results = []
        for raw, kind in ((pdf(), 'pdf'), (docx(), 'docx'), (xlsx(), 'xlsx')):
            self.load(raw, kind)
            results.append(self.file_read())
        token = self.state.issue_binding('TA', 'CA')
        code = ('import sys,json; from unittest.mock import patch; from laowu.mcp import serve; '
                'values=iter(json.loads(sys.stdin.buffer.readline())); '
                'stub=patch("laowu.slack_read_proxy.request_context", side_effect=lambda *a,**k: next(values)); stub.start(); '
                'serve(sys.argv[1],slack_token_file=sys.argv[2])')
        process = subprocess.Popen([sys.executable, '-c', code, str(self.root), str(token)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.stdin.write((dumps(results) + '\n').encode())
            process.stdin.flush()
            for index, proof in enumerate(('PDF evidence alpha', 'DOCX正文证据', 'hidden evidence'), 1):
                result = self.mcp_roundtrip(process, index)
                content = json.loads(result['content'][0]['text'])
                self.assertEqual(content['status'], 'text', content)
                self.assertIn(proof, content['text'])
                self.assertNotIn('ref', content)
        finally:
            process.terminate()
            _, error = process.communicate(timeout=10)
            self.assertFalse(error, error)

    @unittest.skipUnless(HAS_ALL, 'install .[slack] all file parsers before end-to-end acceptance')
    def test_real_proxy_and_stdio_pdf_docx_xlsx_and_large_image(self):
        proxy = SlackReadProxy(self.reader)
        try:
            proxy.start()
        except PermissionError:
            self.skipTest('host sandbox forbids Unix socket bind; run on deployment host')
        self.addCleanup(proxy.close)
        token = self.state.issue_binding('TA', 'CA')
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'laowu.py'
        process = subprocess.Popen([sys.executable, str(script), '--root', str(self.root), 'mcp', '--slack-token-file', str(token)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for ident, (raw, kind, proof) in enumerate(((pdf(), 'pdf', 'PDF evidence alpha'),
                                                      (docx(), 'docx', 'DOCX正文证据'),
                                                      (xlsx(), 'xlsx', 'hidden evidence'),
                                                      (png(), 'image/png', None)), 1):
                self.load(raw, kind)
                result = self.mcp_roundtrip(process, ident)
                self.assertLessEqual(len(dumps(result).encode()), MAX_IMAGE_RESPONSE)
                meta = json.loads(result['content'][0]['text'])
                self.assertNotIn('ref', meta)
                if proof:
                    self.assertEqual(meta['status'], 'text', meta)
                    self.assertIn(proof, meta['text'])
                else:
                    self.assertEqual([c['type'] for c in result['content']], ['text', 'image'])
                    self.assertTrue(base64.b64decode(result['content'][1]['data']).startswith(b'\x89PNG'))
            with self.state.store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM context_refs').fetchone()[0], 0)
        finally:
            process.terminate()
            _, error = process.communicate(timeout=10)
            self.assertFalse(error, error)
