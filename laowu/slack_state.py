"""Durable Slack metadata and queues. Credentials never enter the database."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path

from .common import atomic_write, json_text, new_id, now


def conversation_id(channel):
    return f"slack:{channel['team_id']}:{channel['channel_id']}:{channel['generation']}"


def initialize_slack(store):
    from .runtime import initialize_runtime
    from .background import initialize_background
    initialize_runtime(store)
    initialize_background(store)
    with store._lock(), contextlib.closing(store.connect()) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='slack_meta'").fetchone():
            if db.execute("SELECT value FROM slack_meta WHERE key='schema_version'").fetchone()[0] != '1':
                raise ValueError('不支持的 Slack 数据库版本')
            return
        store._backup_locked(db)
        db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE slack_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO slack_meta VALUES('schema_version','1');
        INSERT INTO slack_meta VALUES('config','{"enabled":false}');
        CREATE TABLE slack_channels(
          team_id TEXT NOT NULL,channel_id TEXT NOT NULL,mode TEXT NOT NULL DEFAULT 'mention',
          personal INTEGER NOT NULL DEFAULT 0,private_approved INTEGER NOT NULL DEFAULT 0,eligible INTEGER NOT NULL DEFAULT 1,
          thread_id TEXT,generation INTEGER NOT NULL DEFAULT 0,summary TEXT NOT NULL DEFAULT '',
          last_activity REAL NOT NULL DEFAULT 0,last_ts TEXT,member_digest TEXT,error TEXT,
          PRIMARY KEY(team_id,channel_id));
        CREATE TABLE slack_bindings(token_hash TEXT PRIMARY KEY,team_id TEXT NOT NULL,
          channel_id TEXT NOT NULL,generation INTEGER NOT NULL,personal INTEGER NOT NULL,
          revoked INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
        CREATE TABLE slack_task_grants(task_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,
          audience TEXT NOT NULL,reply_thread TEXT,input_id TEXT);
        CREATE TABLE slack_schedule_grants(schedule_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,
          audience TEXT NOT NULL);
        CREATE TABLE slack_result_grants(task_id TEXT NOT NULL,conversation_id TEXT NOT NULL,
          audience TEXT NOT NULL,notification_id TEXT NOT NULL,
          PRIMARY KEY(task_id,conversation_id,notification_id));
        CREATE TABLE slack_inbox(id TEXT PRIMARY KEY,event_id TEXT UNIQUE,team_id TEXT NOT NULL,
          channel_id TEXT NOT NULL,message_ts TEXT NOT NULL,reply_ts TEXT,body TEXT NOT NULL,
          context TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',turn_id TEXT,thread_id TEXT,
          error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,origin_notice TEXT UNIQUE,
          generation INTEGER NOT NULL DEFAULT 0,UNIQUE(team_id,channel_id,message_ts));
        CREATE INDEX slack_pending ON slack_inbox(status,created_at);
        CREATE TABLE slack_threads(team_id TEXT NOT NULL,channel_id TEXT NOT NULL,root_ts TEXT NOT NULL,
          PRIMARY KEY(team_id,channel_id,root_ts));
        CREATE TABLE slack_outbox(id TEXT PRIMARY KEY,input_id TEXT NOT NULL,part INTEGER NOT NULL,
          body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',slack_ts TEXT,error TEXT,
          next_attempt REAL NOT NULL DEFAULT 0,blocks TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(input_id,part));
        CREATE TABLE slack_approvals(id TEXT PRIMARY KEY,input_id TEXT NOT NULL,request_id TEXT NOT NULL,
          epoch TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
          expires_at REAL NOT NULL,created_at TEXT NOT NULL);
        COMMIT;
        """)


class SlackState:
    def __init__(self, store):
        self.store = store
        initialize_slack(store)
        self.directory = store.private / 'slack'
        store._safe(self.directory)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)

    def config(self):
        with self.store.connect() as db:
            return json.loads(db.execute("SELECT value FROM slack_meta WHERE key='config'").fetchone()[0])

    def set_config(self, config):
        allowed = {'team_id', 'owner_user_id', 'bot_user_id', 'enabled'}
        if set(config) - allowed:
            raise ValueError('未知 Slack 配置字段')
        previous = self.config()
        for key in ('team_id', 'owner_user_id', 'bot_user_id'):
            if key in config and not re.fullmatch(r'[A-Z][A-Z0-9]{1,63}', config[key]):
                raise ValueError('Slack 身份须为有效 ID')
        if 'enabled' in config and not isinstance(config['enabled'], bool):
            raise ValueError('enabled 须为布尔值')
        if previous.get('team_id') and any(key in config and previous.get(key) != config[key]
                                                   for key in ('team_id', 'owner_user_id')):
            raise ValueError('不能在现有项目中直接更换 Slack 工作区或用户身份')
        previous.update(config)
        with self.store.transaction() as db:
            db.execute("UPDATE slack_meta SET value=? WHERE key='config'", (json_text(previous),))
        return previous

    def set_enabled(self, enabled):
        if enabled:
            config = self.config()
            if not config.get('team_id') or not config.get('owner_user_id'):
                raise ValueError('请先 slack setup 配置工作区和本人 ID')
            self.credentials()
        return self.set_config({'enabled': bool(enabled)})

    def credentials(self):
        path = self.directory / 'credentials.json'
        self.store._safe(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise PermissionError('Slack 凭据文件须归当前用户所有且权限为 0600')
            with os.fdopen(fd, encoding='utf-8') as stream:
                fd = -1
                data = json.loads(stream.read(16385))
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(data, dict) or set(data) != {'app_token', 'bot_token'}:
            raise ValueError('Slack 凭据须含 app_token、bot_token')
        if not isinstance(data['app_token'], str) or not data['app_token'].startswith('xapp-'):
            raise ValueError('app_token 类型不正确')
        if not isinstance(data['bot_token'], str) or not data['bot_token'].startswith('xoxb-'):
            raise ValueError('bot_token 类型不正确')
        return data

    def save_credentials(self, credentials):
        path = self.directory / 'credentials.json'
        self.store._safe(path)
        # Validate before replacing a working credential file.
        if not isinstance(credentials, dict) or set(credentials) != {'app_token', 'bot_token'} or not all(
                isinstance(credentials.get(k), str) and credentials[k].startswith(prefix)
                for k, prefix in [('app_token', 'xapp-'), ('bot_token', 'xoxb-')]):
            raise ValueError('凭据须含有效类型的 app_token、bot_token')
        atomic_write(path, json_text(credentials))
        return {'saved': True, 'path': str(path)}

    def channel(self, team, channel, create=False):
        if create:
            if not re.fullmatch(r'[A-Z][A-Z0-9]{1,63}', team) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,63}', channel):
                raise ValueError('无效 Slack 频道 ID')
            with self.store.transaction() as db:
                db.execute('INSERT OR IGNORE INTO slack_channels(team_id,channel_id) VALUES(?,?)', (team, channel))
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM slack_channels WHERE team_id=? AND channel_id=?', (team, channel)).fetchone()
        return dict(row) if row else None

    def channels(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM slack_channels ORDER BY last_activity DESC,channel_id')]

    def update_channel(self, team, channel, **fields):
        allowed = {'mode', 'personal', 'private_approved', 'eligible', 'thread_id', 'generation', 'summary', 'last_activity',
                   'last_ts', 'member_digest', 'error'}
        if not fields or set(fields) - allowed:
            raise ValueError('无效频道更新字段')
        if 'mode' in fields and fields['mode'] not in ('mention', 'direct'):
            raise ValueError('频道模式须为 mention 或 direct')
        self.channel(team, channel, create=True)
        with self.store.transaction() as db:
            db.execute('UPDATE slack_channels SET ' + ','.join(k + '=?' for k in fields)
                       + ' WHERE team_id=? AND channel_id=?', (*fields.values(), team, channel))
        return self.channel(team, channel)

    def bind_thread(self, team, channel, thread_id):
        return self.update_channel(team, channel, thread_id=thread_id)

    def reset(self, team, channel):
        sync_ids = []
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM slack_channels WHERE team_id=? AND channel_id=?', (team, channel)).fetchone()
            if not row:
                raise ValueError('频道尚未登记')
            previous = conversation_id(dict(row))
            following = f"slack:{team}:{channel}:{row['generation'] + 1}"
            sync_ids = [r[0] for r in db.execute('SELECT task_id FROM slack_task_grants WHERE conversation_id=? AND task_id NOT IN (SELECT task_id FROM async_tasks)', (previous,))]
            # Explicit asynchronous work and schedules belong to the channel,
            # independently of the reset model context. Their original Slack
            # reply address remains immutable in async_tasks/task grants.
            db.execute('UPDATE slack_task_grants SET conversation_id=? WHERE conversation_id=? AND task_id IN (SELECT task_id FROM async_tasks)', (following, previous))
            db.execute('UPDATE slack_schedule_grants SET conversation_id=? WHERE conversation_id=?', (following, previous))
            db.execute('UPDATE slack_channels SET generation=generation+1,thread_id=NULL,summary=\'\' WHERE team_id=? AND channel_id=?', (team, channel))
            db.execute('UPDATE slack_bindings SET revoked=1 WHERE team_id=? AND channel_id=?', (team, channel))
            db.execute("UPDATE slack_approvals SET status='expired' WHERE input_id IN (SELECT id FROM slack_inbox WHERE team_id=? AND channel_id=?) AND status='pending'", (team, channel))
            # A queued notification has not called a model yet. Return it to
            # routing so the new audience is checked before it is re-armed.
            db.execute("UPDATE notifications SET status='pending',target_thread=NULL,turn_id=NULL,error=NULL WHERE id IN (SELECT origin_notice FROM slack_inbox WHERE team_id=? AND channel_id=? AND origin_notice IS NOT NULL AND status='pending')", (team, channel))
            db.execute("UPDATE slack_inbox SET status='cancelled',error='conversation_reset_before_start' WHERE team_id=? AND channel_id=? AND origin_notice IS NOT NULL AND status='pending'", (team, channel))
            # Old user inputs may have relied on previous private context; never replay.
            db.execute("UPDATE slack_inbox SET status='cancelled',error='conversation_reset',body='' WHERE team_id=? AND channel_id=? AND status IN ('pending','running','processing','uncertain') AND origin_notice IS NULL", (team, channel))
            db.execute("UPDATE slack_outbox SET status='cancelled',body='',blocks='[]',error='conversation_reset' WHERE input_id IN (SELECT id FROM slack_inbox WHERE team_id=? AND channel_id=?) AND status!='sent'", (team, channel))
        from .runtime import cancel_task
        for ident in sync_ids:
            cancel_task(self.store, ident)
        return self.channel(team, channel)

    def issue_binding(self, team, channel):
        row = self.channel(team, channel)
        if not row or not row['eligible']:
            raise PermissionError('频道不可用')
        token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.store.transaction() as db:
            db.execute('INSERT INTO slack_bindings VALUES(?,?,?,?,?,0,?)',
                       (token_hash, team, channel, row['generation'], row['personal'], now()))
        path = self.directory / ('binding-' + token_hash[:24])
        self.store._safe(path)
        atomic_write(path, token)
        return path

    def engage(self, team, channel, root_ts):
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO slack_threads VALUES(?,?,?)', (team, channel, root_ts))

    def engaged(self, team, channel, root_ts):
        with self.store.connect() as db:
            return bool(db.execute('SELECT 1 FROM slack_threads WHERE team_id=? AND channel_id=? AND root_ts=?', (team, channel, root_ts)).fetchone())

    def enqueue(self, team, channel, message_ts, body, reply_ts=None, context='normal', event_id=None, origin_notice=None):
        if not isinstance(body, str) or len(body) > 32000:
            raise ValueError('Slack 输入最多 32000 字符')
        if context not in ('normal', 'root'):
            raise ValueError('无效语境')
        row = self.channel(team, channel, create=True)
        with self.store.transaction() as db:
            existing = db.execute('SELECT * FROM slack_inbox WHERE (team_id=? AND channel_id=? AND message_ts=?) OR (? IS NOT NULL AND event_id=?) OR (? IS NOT NULL AND origin_notice=?)',
                                  (team, channel, message_ts, event_id, event_id, origin_notice, origin_notice)).fetchone()
            if existing:
                if origin_notice and existing['status'] == 'cancelled' and existing['error'] == 'conversation_reset_before_start':
                    db.execute("DELETE FROM slack_outbox WHERE input_id=? AND status='cancelled'", (existing['id'],))
                    db.execute("UPDATE slack_inbox SET status='pending',body=?,generation=?,turn_id=NULL,thread_id=NULL,error=NULL,updated_at=? WHERE id=?",
                               (body, row['generation'], now(), existing['id']))
                    return dict(db.execute('SELECT * FROM slack_inbox WHERE id=?', (existing['id'],)).fetchone(), duplicate=True)
                return dict(existing, duplicate=True)
            ident = new_id('slackin')
            db.execute('INSERT INTO slack_inbox(id,event_id,team_id,channel_id,message_ts,reply_ts,body,context,created_at,updated_at,origin_notice,generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, event_id, team, channel, message_ts, reply_ts, body, context, now(), now(), origin_notice, row['generation']))
            db.execute("INSERT INTO read_sources VALUES('input',?,'pending')", (ident,))
            if not origin_notice:
                activity = float(message_ts)
                db.execute('UPDATE slack_channels SET last_activity=max(last_activity,?),last_ts=CASE WHEN last_ts IS NULL OR CAST(last_ts AS REAL)<CAST(? AS REAL) THEN ? ELSE last_ts END WHERE team_id=? AND channel_id=?', (activity, message_ts, message_ts, team, channel))
            return dict(db.execute('SELECT * FROM slack_inbox WHERE id=?', (ident,)).fetchone(), duplicate=False)

    def inbox(self, status=None):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM slack_inbox WHERE (? IS NULL OR status=?) ORDER BY created_at,rowid', (status, status))]

    def update_input(self, ident, **fields):
        allowed = {'status', 'turn_id', 'thread_id', 'error', 'body', 'generation', 'reply_ts'}
        if not fields or set(fields) - allowed:
            raise ValueError('无效输入更新字段')
        fields['updated_at'] = now()
        with self.store.transaction() as db:
            if fields.get('status') == 'uncertain':
                db.execute("INSERT OR REPLACE INTO read_sources VALUES('input',?,'uncertain')", (ident,))
            db.execute('UPDATE slack_inbox SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?', (*fields.values(), ident))

    def read_source(self, kind, ident):
        with self.store.connect() as db:
            row = db.execute('SELECT state FROM read_sources WHERE kind=? AND id=?', (kind,ident)).fetchone()
            return row[0] if row else 'uncertain'

    def submitting(self, ident):
        # Persist uncertainty BEFORE the external action, including lost replies
        # with no turn_id. A process restart cannot turn this into a fresh input.
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO read_sources VALUES('input',?,'uncertain')", (ident,))

    def queue_output(self, input_id, text, blocks=None, key=None):
        with self.store.transaction() as db:
            # A stable key yields a stable part, including after a response was lost.
            part = int(hashlib.sha256(str(key).encode()).hexdigest()[:12], 16) if key is not None else 0
            existing = db.execute('SELECT * FROM slack_outbox WHERE input_id=? AND part=?', (input_id, part)).fetchone()
            if existing:
                if existing['body'] != text or existing['blocks'] != json_text(blocks or []):
                    raise ValueError('同一发送标识已用于不同内容')
                return dict(existing)
            ident = new_id('slackout')
            db.execute('INSERT INTO slack_outbox(id,input_id,part,body,blocks,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                       (ident, input_id, part, text, json_text(blocks or []), now(), now()))
            db.execute("INSERT INTO read_sources VALUES('output',?,'pending')", (ident,))
            return dict(db.execute('SELECT * FROM slack_outbox WHERE id=?', (ident,)).fetchone())

    def outputs(self, status=None):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM slack_outbox WHERE (? IS NULL OR status=?) ORDER BY created_at,rowid', (status, status))]

    def update_output(self, ident, **fields):
        allowed = {'status', 'slack_ts', 'error', 'next_attempt', 'body', 'blocks'}
        if not fields or set(fields) - allowed:
            raise ValueError('无效发送更新字段')
        fields['updated_at'] = now()
        with self.store.transaction() as db:
            if fields.get('status') in ('uncertain','sending'):
                db.execute("INSERT OR REPLACE INTO read_sources VALUES('output',?,'uncertain')", (ident,))
            db.execute('UPDATE slack_outbox SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?', (*fields.values(), ident))

    def recover(self):
        with self.store.transaction() as db:
            from .slack_files import recover_sends
            recover_sends(db)
            db.execute("UPDATE slack_bindings SET revoked=1")
            db.execute("UPDATE slack_approvals SET status='expired' WHERE status='pending'")
            db.execute("UPDATE slack_inbox SET status='uncertain',error='restart_needs_reconciliation' WHERE status IN ('running','processing')")
            db.execute("UPDATE slack_outbox SET status='uncertain',error='restart_needs_reconciliation' WHERE status='sending'")
            db.execute("INSERT OR REPLACE INTO read_sources SELECT 'input',id,'uncertain' FROM slack_inbox WHERE status='uncertain'")
            db.execute("INSERT OR REPLACE INTO read_sources SELECT 'output',id,'uncertain' FROM slack_outbox WHERE status='uncertain'")

    def status(self):
        from .processing import status as processing_status
        config = self.config()
        with self.store.connect() as db:
            counts = {row[0]: row[1] for row in db.execute('SELECT status,count(*) FROM slack_inbox GROUP BY status')}
            sends = {row[0]: row[1] for row in db.execute('SELECT status,count(*) FROM slack_outbox GROUP BY status')}
            file_sends = {}
            for row in db.execute("SELECT value FROM slack_meta WHERE key LIKE 'file-send:%'"):
                status = json.loads(row[0]).get('status', 'unknown')
                file_sends[status] = file_sends.get(status, 0) + 1
            runtime = db.execute("SELECT value FROM slack_meta WHERE key='runtime'").fetchone()
            errors = [dict(r) for r in db.execute('SELECT id,channel_id,status,error,updated_at FROM slack_inbox WHERE error IS NOT NULL ORDER BY updated_at DESC LIMIT 10')]
        runtime = json.loads(runtime[0]) if runtime else {'status': 'stopped'}
        if runtime.get('pid'):
            from .common import process_alive
            runtime['running'] = process_alive(runtime['pid'], runtime.get('identity')) and time.time() - runtime.get('heartbeat', 0) < 60
            if not runtime['running'] and runtime.get('status') == 'connected':
                runtime['status'] = 'stale_or_stopped'
        return {'configured': bool(config.get('team_id') and config.get('owner_user_id')),
                'processing': {**processing_status(self.store), 'enabled': bool(config.get('enabled'))},
                'config': config, 'channels': self.channels(), 'inbox': counts, 'outbox': sends,
                'file_sends': file_sends,
                'file_capabilities': {'read': 'UTF-8 text <=1MiB; PDF/DOCX/XLSX and images <=20MiB; decoded image output <=4MiB',
                                      'send': 'generated text <=8KiB; registered task artifacts <=20MiB',
                                      'required_scopes': ['files:read', 'files:write'],
                                      'effective_token_scopes': 'not_checked',
                                      'binary_parsing': 'PDF text layer <=200 pages; DOCX body; XLSX cells/formulas without evaluation; no OCR or legacy DOC/XLS; requires .[slack] parsers',
                                      'parser_limits': '15s wall/8s CPU/768MiB memory; 40MP image; ZIP 1024 entries/16MiB member/64MiB total/200 ratio; 4MiB extracted text; XLSX 100 sheets/10000 rows/256 columns/100000 cells'},
                'recent_errors': errors,
                'runtime': runtime,
                'live_slack_test': 'not_performed_by_status', 'live_model_test': 'not_performed_by_status'}

    def runtime(self, **value):
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO slack_meta VALUES('runtime',?)", (json_text({**value, 'heartbeat': time.time()}),))
