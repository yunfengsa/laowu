"""Gateway-owned file reads and at-most-once upload attempts.

Only metadata lives in slack_meta. Neither file bodies nor bearer/upload URLs
are persisted. File IDs alone are never a permission to download a file.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .context import body_size, digest, dumps
from .policy import settings
from .retries import _READ_DEADLINE
from .slack_client import SlackAPIError
from .slack_file_parser import MIMES, parse_file

TOOLS = frozenset({'slack_file_read', 'slack_file_send', 'slack_file_status'})
MAX_FILE = 20 * 1024 * 1024
MAX_TEXT = 1024 * 1024
MAX_IMAGE = 4 * 1024 * 1024
MAX_IMAGE_RESPONSE = 6 * 1024 * 1024
FILE_SECONDS = 55
PREFIX = 'file-send:'
FILE_ID = re.compile(r'F[A-Z0-9]{1,63}\Z')
TOKEN = re.compile(r'\b(?:xox[baprs]-|xapp-)[A-Za-z0-9-]+')
TEXT_TYPES = {'.txt', '.md', '.csv'}
IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}


def metadata(files):
    """Bounded untrusted names, never URLs, previews, tokens or full file objects."""
    result = []
    for item in files[:10] if isinstance(files, list) else []:
        if not isinstance(item, dict) or not FILE_ID.fullmatch(str(item.get('id', ''))):
            continue
        size = item.get('size')
        name = TOKEN.sub('[redacted]', str(item.get('name') or item.get('title') or ''))
        name = ''.join(c for c in name if ord(c) >= 32).encode('utf-8')[:160].decode('utf-8', errors='ignore')
        mime = str(item.get('mimetype', ''))
        mime = mime if re.fullmatch(r'[a-zA-Z0-9.+_-]{1,40}/[a-zA-Z0-9.+_-]{1,40}', mime) else ''
        result.append({'file_id': item['id'],
                       'name': name,
                       'size': size if type(size) is int and size >= 0 else None,
                       'mimetype': mime})
    return result


def image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _filename(name):
    if (not isinstance(name, str) or not 1 <= len(name) <= 160 or name.startswith('.')
            or any(c in name for c in '/\\\x00') or any(ord(c) < 32 for c in name)
            or TOKEN.search(name)):
        raise ValueError('file_name: 须为不含路径、控制字符或令牌的文件名（最多160字符）')
    return name


def _secret(data):
    from .store import _SECRET
    text = data.decode('utf-8', errors='replace')
    if _SECRET.search(text) or TOKEN.search(text) or re.search(r'-----BEGIN [A-Z ]*PRIVATE KEY-----', text):
        raise PermissionError('file_secret: 拒绝发送含凭据或私钥的文件')


def _artifact_grant(store, access, task_id, artifact, db=None):
    if db is None:
        with store.connect() as connection:
            return _artifact_grant(store, access, task_id, artifact, connection)
    else:
        access.require_task(task_id, db)
        task = db.execute('SELECT status,result FROM tasks WHERE id=?', (task_id,)).fetchone()
        try:
            result = json.loads(task['result'] or '{}') if task else {}
            from .worker import handoff
            handoff(dumps(result))
        except (ValueError, TypeError):
            raise PermissionError('artifact_grant: 需要有效最终JSON交接，检查点不授予发送权限') from None
        if (not task or task['status'] not in {'completed', 'blocked'}
                or artifact not in result['artifacts']):
            raise PermissionError('artifact_grant: 只允许当前来源completed/blocked任务最终交接登记的产物')
        if db.execute("""SELECT 1 FROM runs WHERE task_id=? AND
                      (status IN ('starting','running') OR id IN (SELECT run_id FROM run_cleanup))""",
                      (task_id,)).fetchone():
            raise PermissionError('artifact_grant: 任务仍有运行实例，不能发送产物')
        # An output already owned by another task cannot be re-registered here.
        for row in db.execute('SELECT result,checkpoint FROM tasks WHERE id!=?', (task_id,)):
            for value in row:
                parsed = json.loads(value or '{}')
                if isinstance(parsed, dict) and artifact in parsed.get('artifacts', []):
                    raise PermissionError('artifact_owner: 产物同时属于其他任务，拒绝外发')


def _read_artifact(store, access, task_id, artifact):
    """Exact final-handoff allowlist; no checkpoint-only/path/link exfiltration."""
    _artifact_grant(store, access, task_id, artifact)
    path = Path(artifact)
    if not path.is_absolute() or '..' in path.parts or str(path) != artifact:
        raise PermissionError('artifact_path: 需要交接中登记的规范绝对路径')
    try:
        parts = path.relative_to(store.root / 'artifacts').parts
    except ValueError:
        raise PermissionError('artifact_path: 只允许项目 artifacts 内的登记产物') from None
    if not parts or any(p.startswith('.') for p in parts):
        raise PermissionError('artifact_path: 不允许隐藏路径')
    name = _filename(parts[-1])
    suffix = Path(name).suffix.lower()
    if suffix not in TEXT_TYPES | IMAGE_TYPES | {'.pdf', '.docx', '.xlsx'}:
        raise PermissionError('artifact_type: 仅允许图片、PDF/DOCX/XLSX、txt/md/csv产物；不发送代码、配置或数据库')
    descriptor = os.open(store.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in ('artifacts', *parts[:-1]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != os.getuid() or before.st_size > MAX_FILE):
                raise PermissionError('artifact_file: 需为本用户单链接普通文件，最多20MiB')
            data = stream.read(MAX_FILE + 1)
            after = os.fstat(stream.fileno())
            if (len(data) > MAX_FILE or after.st_nlink != 1 or
                    (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise PermissionError('artifact_changed: 文件读取期间改变')
    except OSError:
        raise PermissionError('artifact_file: 文件不存在或包含不安全链接/路径') from None
    finally:
        os.close(descriptor)
    if suffix in IMAGE_TYPES and not image_type(data):
        raise PermissionError('artifact_type: 图片签名与允许格式不符')
    if suffix == '.pdf' and not data.startswith(b'%PDF-'):
        raise PermissionError('artifact_type: PDF签名不符')
    if suffix in {'.docx', '.xlsx'}:
        from .store import _SECRET
        parsed = parse_file(data, suffix[1:], secret_pattern=_SECRET.pattern + '|' + TOKEN.pattern)
        if 'error' in parsed:
            raise PermissionError('artifact_document: ' + parsed['error'])
    if suffix in TEXT_TYPES:
        if len(data) > MAX_TEXT:
            raise ValueError('artifact_size: 文本产物最多1MiB')
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            raise ValueError('artifact_encoding: 文本须为UTF-8') from None
        if '\x00' in text:
            raise PermissionError('artifact_type: 文本包含二进制数据')
    _secret(data)
    return name, data


def scrub_sends(db):
    """Keep only irreversible anonymous dedup tombstones on forgetting."""
    db.execute("UPDATE slack_meta SET value=? WHERE key LIKE ?",
               (dumps({'status': 'forgotten'}), PREFIX + '%'))


def merge_sends(source, current, *, scrub=False):
    """A restored snapshot cannot roll back a newer external write decision."""
    def exists(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE name='slack_meta'").fetchone()
    if not exists(source):
        # A snapshot predating Slack retains anonymous keys in memory_meta, an
        # existing table. No incomplete Slack schema is manufactured on restore.
        if exists(current):
            for row in current.execute('SELECT key FROM slack_meta WHERE key LIKE ?', (PREFIX + '%',)):
                source.execute('INSERT OR REPLACE INTO memory_meta VALUES(?,?)',
                               (row[0], dumps({'status': 'forgotten'})))
        for row in current.execute('SELECT key,value FROM memory_meta WHERE key LIKE ?', (PREFIX + '%',)):
            source.execute('INSERT OR REPLACE INTO memory_meta VALUES(?,?)', tuple(row))
        return
    for row in current.execute('SELECT key,value FROM memory_meta WHERE key LIKE ?', (PREFIX + '%',)):
        source.execute('INSERT OR REPLACE INTO slack_meta VALUES(?,?)', tuple(row))
    if exists(current):
        for row in current.execute('SELECT key,value FROM slack_meta WHERE key LIKE ?', (PREFIX + '%',)):
            previous = source.execute('SELECT value FROM slack_meta WHERE key=?', (row['key'],)).fetchone()
            saved, live = json.loads(previous[0]) if previous else {}, json.loads(row['value'])
            # Imported forgetting must also dominate an older live delivery.
            if saved.get('status') == 'forgotten' or live.get('status') == 'forgotten':
                merged = {'status': 'forgotten'}
            elif saved.get('status') == 'sent' and live.get('status') != 'sent':
                merged = saved
            else:
                merged = live
            source.execute('INSERT OR REPLACE INTO slack_meta VALUES(?,?)', (row['key'], dumps(merged)))
    if scrub:
        scrub_sends(source)
    else:
        recover_sends(source)


def recover_sends(db):
    for row in db.execute('SELECT key,value FROM slack_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchall():
        value = json.loads(row['value'])
        if value.get('status') in {'allocating', 'allocated', 'uploading', 'uploaded', 'completing'}:
            value.update(status='uncertain', error='restart_needs_reconciliation')
            db.execute('UPDATE slack_meta SET value=? WHERE key=?', (dumps(value), row['key']))


def file_content(store, value):
    """Real MCP content blocks; a separate, explicit binary budget, no DB refs."""
    image = value.get('image')
    plain = {k: v for k, v in value.items() if k != 'image'}
    if body_size(plain) > min(settings(store)['response_bytes'], 16 * 1024):
        raise ValueError('file_response_budget: 文本响应超过当前预算')
    content = [{'type': 'text', 'text': dumps(plain)}]
    if image:
        if (set(image) != {'type', 'data', 'mimeType'} or image['type'] != 'image'
                or image['mimeType'] not in {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
                or len(image['data']) > (MAX_IMAGE + 2) // 3 * 4):
            raise ValueError('file_image_budget: 图片响应无效或超限')
        content.append(image)
    response = {'content': content, 'isError': False}
    if len(dumps(response).encode()) > MAX_IMAGE_RESPONSE:
        raise ValueError('file_image_budget: MCP图片响应超限')
    return response


class SlackFiles:
    def __init__(self, reader):
        self.reader, self.state, self.store, self.api = reader, reader.state, reader.store, reader.api

    def call(self, access, method, args):
        # No generic read retry wrapper around any external write operation.
        mark = _READ_DEADLINE.set((self.reader.clock, self.reader.clock() + FILE_SECONDS))
        try:
            config = self.state.config()
            auth = self.reader._get('auth.test')
            if auth.get('team_id') != config.get('team_id') or auth.get('user_id') != config.get('bot_user_id'):
                raise PermissionError('workspace_mismatch: bot与工作区绑定不符')
            source, info = self.reader._source(access, config)
            if method == 'slack_file_read':
                value = self.read(access, source, info, config, args)
                self.reader._target(args.get('channel_id', source['channel_id']), source, info, config)
            elif method == 'slack_file_send':
                value = self.send(access, source, config, args)
            else:
                value = self.status(access, source, config, args)
            self.reader._source(access, config)
            return value
        except SlackAPIError as exc:
            hint = '请核对files:read/files:write及重新安装后的有效授权。' if exc.code == 'missing_scope' else '未取得文件内容或确认投递；发送后请用原幂等键查询状态，勿更换键重发。'
            raise ValueError('Slack=' + exc.code + '。' + hint) from None
        finally:
            _READ_DEADLINE.reset(mark)

    def _info(self, file_id):
        if not FILE_ID.fullmatch(file_id):
            raise ValueError('file_id: 须为真实Slack文件ID')
        value = self.reader._get('files.info', file=file_id).get('file', {})
        if (value.get('id') != file_id or value.get('is_external') or value.get('mode') not in (None, 'hosted', 'snippet')
                or value.get('file_access') in {'check_file_info', 'access_denied'}):
            raise PermissionError('file_unavailable: 不支持外部/远程/不可访问文件')
        return value

    @staticmethod
    def _shares(value, channel):
        shares = value.get('shares', {})
        return [item for visibility in ('public', 'private')
                for item in shares.get(visibility, {}).get(channel, []) if isinstance(item, dict)]

    def _proof(self, value, channel):
        # files.info's current channel membership is required, not a public URL.
        return bool(self._shares(value, channel) or any(channel in value.get(k, []) for k in ('channels', 'groups', 'ims')))

    def read(self, access, source, source_info, config, args):
        from .slack_context import _ID
        channel = args.get('channel_id', source['channel_id'])
        if not _ID.fullmatch(channel):
            raise ValueError('channel_id: 无效频道ID')
        self.reader._target(channel, source, source_info, config)
        value = self._info(args['file_id'])
        if not self._proof(value, channel):
            raise PermissionError('file_share: 文件未实际分享至指定可读频道')
        item = metadata([value])[0]
        result = {'version': 1, 'file': item, 'channel_id': channel, 'trust': 'untrusted_reference_only'}
        size = value.get('size')
        if type(size) is not int or not 0 <= size <= MAX_FILE:
            return {**result, 'status': 'not_read', 'reason': '文件大小未知或超过20MiB'}
        mime = str(value.get('mimetype', ''))
        text_type = mime.startswith('text/') or mime in {'application/json', 'application/xml', 'application/javascript'}
        image = mime in {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
        document = MIMES.get(mime)
        if not text_type and not image and not document:
            return {**result, 'status': 'not_parsed', 'reason': '支持UTF-8文本、PNG/JPEG/GIF/WebP、PDF/DOCX/XLSX；旧DOC/XLS、宏文档、音频及其他二进制不支持，未解析或保存'}
        if text_type and size > MAX_TEXT:
            return {**result, 'status': 'not_read', 'reason': 'UTF-8文本输入限制1MiB'}
        raw = self.api.download_file(value.get('url_private_download') or value.get('url_private'),
                                     limit=MAX_TEXT if text_type else MAX_FILE)
        if len(raw) != size:
            raise ValueError('file_changed: 实际大小与Slack元数据不同，请重新读取')
        parsed = parse_file(raw, mime if image else document) if image or document else None
        # Recheck AFTER download AND potentially expensive parsing, before disclosure.
        latest = self._info(args['file_id'])
        if not self._proof(latest, channel) or any(latest.get(k) != value.get(k) for k in ('size', 'updated', 'timestamp')):
            raise PermissionError('file_changed: 文件分享或版本已改变')
        self.reader._target(channel, source, source_info, config)
        if parsed and 'error' in parsed:
            return {**result, 'status': 'not_parsed', 'reason': parsed['error']}
        if image:
            if args.get('text_offset') or args.get('text_version'):
                raise ValueError('file_image: 图片不使用文本分页')
            return {**result, 'status': 'image', **parsed}
        try:
            text = parsed['text'] if document else raw.decode('utf-8-sig')
        except UnicodeError:
            return {**result, 'status': 'not_parsed', 'reason': '文件不是有效UTF-8文本'}
        if '\x00' in text:
            return {**result, 'status': 'not_parsed', 'reason': '文件包含二进制NUL，未作为文本解析'}
        text = TOKEN.sub('[Slack令牌已移除]', text)
        if document:
            result['document'] = parsed['document']
        # Bind offsets to extraction as well as bytes, including parser upgrades.
        version = hashlib.sha256(raw + text.encode('utf-8') if document else raw).hexdigest()
        offset = args.get('text_offset', 0)
        if offset and args.get('text_version') != version:
            raise ValueError('file_version: 从非零offset续读须保留原text_version；版本改变需重读')
        if args.get('text_version', version) != version or offset > len(text):
            raise ValueError('file_version: 文件版本或offset无效')
        policy = settings(self.store)
        def page(end):
            return {**result, 'status': 'text', 'text': text[offset:end], 'text_offset': offset,
                    'text_length': len(text), 'text_version': version, 'text_complete': end == len(text),
                    'next_offset': end if end < len(text) else None, 'offset_unit': 'unicode_codepoints'}
        low, high = offset, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if (body_size(page(mid)) <= min(policy['response_bytes'], 16 * 1024)
                    and len(dumps(text[offset:mid]).encode()) <= policy['page_bytes']):
                low = mid
            else:
                high = mid - 1
        if low == offset and offset < len(text):
            raise ValueError('file_response_budget: 当前预算不足以容纳文本片段')
        return page(low)

    def _key(self, source, key):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', key):
            raise ValueError('idempotency_key: 使用1至128位字母/数字/_.:-，重试保持原键')
        # Stable across binding renewal/reset; old generations remain inaccessible
        # but cannot reuse a delivery key and blindly repeat an old write.
        return PREFIX + digest([source['team_id'], source['channel_id'], key])

    def _get_record(self, key):
        with self.store.connect() as db:
            row = db.execute('SELECT value FROM slack_meta WHERE key=?', (key,)).fetchone()
            tombstone = db.execute('SELECT value FROM memory_meta WHERE key=?', (key,)).fetchone()
            if tombstone:
                return {'status': 'forgotten'}
        return json.loads(row[0]) if row else None

    def _bound(self, access, value):
        if value.get('status') == 'forgotten':
            raise PermissionError('file_forgotten: 此幂等键已撤销，不能再次投递')
        if value.get('source') != access.conversation_id or value.get('audience') != access.audience:
            raise PermissionError('file_source: 投递不属于当前来源/代际')
        access.validate()
        if value.get('task_id'):
            access.require_task(value['task_id'])

    def _update(self, access, key, **fields):
        with self.store.transaction() as db:
            row = db.execute('SELECT value FROM slack_meta WHERE key=?', (key,)).fetchone()
            value = json.loads(row[0]) if row else {}
            self._bound(access, value)
            if value.get('status') == 'sent' and fields.get('status') != 'sent':
                # A concurrent status lookup can confirm a commit while the
                # original complete call is still waiting or timing out.
                return value
            value.update(fields)
            db.execute('UPDATE slack_meta SET value=? WHERE key=?', (dumps(value), key))
        return value

    @staticmethod
    def _public(value):
        return {k: value.get(k) for k in ('status', 'phase', 'file_id', 'permalink', 'channel_id', 'thread_ts', 'error')} | {
            'retry': '仅用原idempotency_key查询slack_file_status；不确定/失败均不自动重新上传'}

    def _live_send(self, access, config, input_id, args):
        self.reader._source(access, config)
        if access.input_context()['input_id'] != input_id:
            raise PermissionError('file_input: 原发送请求已结束或改变，停止发送')
        if 'task_id' in args:
            _artifact_grant(self.store, access, args['task_id'], args['artifact'])

    def send(self, access, source, config, args):
        if ('text' in args) == ('task_id' in args or 'artifact' in args):
            raise ValueError('file_send: 二选一：filename+text，或task_id+artifact')
        key = self._key(source, args['idempotency_key'])
        request = digest({k: v for k, v in args.items() if k != 'idempotency_key'})
        old = self._get_record(key)
        if old:
            self._bound(access, old)
            if old.get('request') != request:
                raise ValueError('file_idempotency_conflict: 同一幂等键不能更换内容或任务')
            return self._public(old)
        context = access.input_context()
        if 'text' in args:
            if set(args) != {'idempotency_key', 'filename', 'text'}:
                raise ValueError('file_send: 生成文本只接受filename/text/idempotency_key')
            name = _filename(args['filename'])
            data = args['text'].encode('utf-8')
            if Path(name).suffix.lower() not in TEXT_TYPES or len(data) > 8 * 1024 or '\x00' in args['text']:
                raise ValueError('file_text: 生成文本仅支持txt/md/csv，最多8KiB UTF-8')
            _secret(data)
        else:
            if set(args) != {'idempotency_key', 'task_id', 'artifact'}:
                raise ValueError('file_send: 产物只接受task_id/artifact/idempotency_key')
            name, data = _read_artifact(self.store, access, args['task_id'], args['artifact'])
        _secret(name.encode('utf-8'))
        intent = digest([access.conversation_id, context['reply_thread'], args.get('task_id'),
                         hashlib.sha256(data).hexdigest()])
        record = dict(source=access.conversation_id, audience=access.audience,
                      input_id=context['input_id'], channel_id=source['channel_id'], thread_ts=context['reply_thread'],
                      task_id=args.get('task_id'), request=request, intent=intent,
                      status='allocating', phase='allocate', file_id=None, permalink=None, error=None)
        self._live_send(access, config, context['input_id'], args)
        with self.store.transaction() as db:
            access.validate(db)
            if 'task_id' in args:
                _artifact_grant(self.store, access, args['task_id'], args['artifact'], db)
            for row in db.execute('SELECT key,value FROM slack_meta WHERE key LIKE ?', (PREFIX + '%',)):
                other = json.loads(row['value'])
                if row['key'] == key or other.get('intent') == intent:
                    raise ValueError('file_duplicate: 已存在相同内容投递，请查询原幂等键状态，不创建新上传')
            db.execute('INSERT INTO slack_meta VALUES(?,?)', (key, dumps(record)))
        try:
            upload = self.api.call('files.getUploadURLExternal', filename=name, length=len(data))
            file_id = upload.get('file_id', '')
            if not FILE_ID.fullmatch(file_id):
                raise SlackAPIError('invalid_upload_response', uncertain=True)
            self._update(access, key, file_id=file_id, status='allocated')
            self._live_send(access, config, context['input_id'], args)
            self._update(access, key, status='uploading', phase='upload')
            self.api.upload_file(upload.get('upload_url'), data)
            self._update(access, key, status='uploaded')
            self._live_send(access, config, context['input_id'], args)
            self._update(access, key, status='completing', phase='complete')
            completed = self.api.call('files.completeUploadExternal', files=[{'id': file_id, 'title': name}],
                                      channel_id=source['channel_id'],
                                      **({'thread_ts': context['reply_thread']} if context['reply_thread'] else {}))
            if not any(isinstance(item, dict) and item.get('id') == file_id for item in completed.get('files', [])):
                raise SlackAPIError('invalid_complete_response', uncertain=True)
            record = self._update(access, key, status='sent', error=None)
        except Exception as exc:
            uncertain = not isinstance(exc, SlackAPIError) or exc.uncertain
            code = exc.code if isinstance(exc, SlackAPIError) else 'interrupted_or_revoked'
            self._update(access, key, status='uncertain' if uncertain else 'failed', error=code)
            return self._public(self._get_record(key))
        # Failure to fetch a permalink must never turn confirmed completion into
        # a new upload attempt. Status can fetch it later without any writes.
        try:
            return self.status(access, source, config, {'idempotency_key': args['idempotency_key']})
        except (SlackAPIError, ValueError):
            return self._public(record)

    def status(self, access, source, config, args):
        key = self._key(source, args['idempotency_key'])
        value = self._get_record(key)
        if not value:
            return {'status': 'not_found', 'retry': '没有此来源的投递记录'}
        self._bound(access, value)
        if value.get('file_id') and value['status'] in {'sent', 'uncertain', 'completing'}:
            try:
                file = self._info(value['file_id'])
            except SlackAPIError as exc:
                # Absence/rejection never proves a timed-out share did not occur.
                return {**self._public(value), 'lookup_error': exc.code}
            shares = self._shares(file, source['channel_id'])
            shared = any((item.get('thread_ts') or None) == value['thread_ts'] for item in shares)
            if shared or value['status'] == 'sent':
                from urllib.parse import urlsplit
                link = file.get('permalink')
                parsed = urlsplit(link) if isinstance(link, str) else None
                if not (parsed and len(link) <= 2048 and parsed.scheme == 'https' and parsed.hostname and
                        (parsed.hostname == 'slack.com' or parsed.hostname.endswith('.slack.com'))
                        and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment):
                    link = None
                value = self._update(access, key, status='sent', permalink=link, error=None)
        return self._public(value)
