"""Durable dispatch and notification ledger. No model calls in this module."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import math
import time

from .common import json_text, new_id, now, process_alive, process_identity


def initialize_background(store):
    # This is a separate, versioned additive migration; never change memory schema.
    with store._lock(), contextlib.closing(store.connect()) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='background_meta'").fetchone():
            version = db.execute("SELECT value FROM background_meta WHERE key='schema_version'").fetchone()
            if version and version[0] == '1':
                return
            raise ValueError("不支持的后台数据库版本")
        store._backup_locked(db)
        db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE background_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO background_meta VALUES('schema_version','1');
        CREATE TABLE async_tasks(
          task_id TEXT PRIMARY KEY REFERENCES tasks(id), origin_thread TEXT,
          context TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
          notified_generation INTEGER NOT NULL DEFAULT -1);
        CREATE TABLE connections(
          id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, mode TEXT NOT NULL,
          owner_pid INTEGER, owner_identity TEXT, connected_at TEXT NOT NULL,
          seen_at TEXT NOT NULL, expires_at REAL, state TEXT NOT NULL DEFAULT 'active');
        CREATE INDEX connections_thread ON connections(thread_id,state);
        CREATE TABLE notifications(
          id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
          origin_thread TEXT, target_thread TEXT, context TEXT NOT NULL,
          topic TEXT NOT NULL, message TEXT NOT NULL, task_id TEXT,
          status TEXT NOT NULL DEFAULT 'pending', turn_id TEXT,
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
          error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX notification_pending ON notifications(status,next_attempt);
        CREATE TABLE daemon_state(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), pid INTEGER,
          identity TEXT, heartbeat REAL, status TEXT NOT NULL, error TEXT);
        COMMIT;
        """)


def valid_context(context):
    if context not in ('normal', 'root'):
        raise ValueError("context 必须为 normal 或 root")
    return context


class Outbox:
    def __init__(self, store):
        self.store = store
        initialize_background(store)

    def publish(self, message, *, event_key, topic='event', origin_thread=None,
                context='normal', task_id=None, delay=0):
        from .service import text_check
        text_check(message, 16000)
        if not message.strip():
            raise ValueError("通知内容不能为空")
        if not isinstance(event_key, str) or not 1 <= len(event_key) <= 200:
            raise ValueError("event_key 须为 1–200 字符的稳定事件标识")
        if not isinstance(topic, str) or not 1 <= len(topic) <= 100:
            raise ValueError("topic 须为 1–100 字符")
        text_check(event_key, 200)
        text_check(topic, 100)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or not 0 <= delay <= 86400:
            raise ValueError('通知延迟须为 0–86400 秒')
        if origin_thread is not None and (not isinstance(origin_thread, str) or not 1 <= len(origin_thread) <= 200):
            raise ValueError("无效目标会话")
        valid_context(context)
        digest = hashlib.sha256(json_text([message, topic, origin_thread, context, task_id]).encode()).hexdigest()
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM notifications WHERE event_key=?", (event_key,)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise ValueError("此事件标识已用于不同的通知")
                return dict(row)
            ident = new_id('notice')
            db.execute("""INSERT INTO notifications
                (id,event_key,digest,origin_thread,context,topic,message,task_id,next_attempt,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (ident, event_key, digest, origin_thread, context, topic, message, task_id,
                 time.time() + max(0, float(delay)), now(), now()))
            return dict(db.execute("SELECT * FROM notifications WHERE id=?", (ident,)).fetchone())

    def list(self, status=None, limit=50):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM notifications WHERE (? IS NULL OR status=?) ORDER BY created_at DESC LIMIT ?",
                (status, status, max(1, min(200, int(limit)))))]

    def get(self, ident):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM notifications WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("通知不存在")
        return dict(row)

    def retry(self, ident):
        # Reconcile an uncertain submission first; never blindly send it again.
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM notifications WHERE id=?", (ident,)).fetchone()
            if not row or row['status'] in ('delivered', 'cancelled'):
                raise ValueError("仅待处理或失败通知可重试")
            status = 'submitted' if row['turn_id'] else (
                'uncertain' if row['target_thread'] or row['status'] in ('sending','submitted','uncertain') else 'pending')
            db.execute("UPDATE notifications SET status=?,next_attempt=0,error=NULL,updated_at=? WHERE id=?",
                       (status, now(), ident))
        return self.get(ident)

    def collect_tasks(self):
        with self.store.connect() as db:
            rows = db.execute("""SELECT t.*,a.origin_thread,a.context,a.generation FROM tasks t
                JOIN async_tasks a ON t.id=a.task_id WHERE a.notified_generation<a.generation
                AND t.status IN ('completed','failed','blocked','cancelled')""").fetchall()
        for row in rows:
            with self.store._lock(), contextlib.closing(self.store.connect()) as db:
                from .processing import status
                current = db.execute('SELECT generation,notified_generation FROM async_tasks WHERE task_id=?', (row['id'],)).fetchone()
                if (status(self.store, db)['paused'] or not current or current['generation'] != row['generation']
                        or current['notified_generation'] >= row['generation']):
                    continue
                self._collect_task(row)

    def _collect_task(self, row):
        # Read the result when presenting, rather than duplicating sensitive task output.
        from .acceptance import snapshot
        with self.store.connect() as db:
            acceptance = snapshot(db, row["id"])["state"]
        self.publish(f"验收状态：{acceptance}。执行结束不等于验收通过。后台任务 {row['id']} 已进入 {row['status']} 状态。请读取 task_status 的结果、验证与未完成事项后告知用户。",
                     event_key=f"task:{row['id']}:{row['generation']}", topic='task.' + row['status'],
                     origin_thread=row['origin_thread'], context=row['context'], task_id=row['id'])
        with self.store.transaction() as db:
            db.execute("UPDATE async_tasks SET notified_generation=? WHERE task_id=? AND generation=?",
                       (row['generation'], row['id'], row['generation']))


class Presence:
    """Explicit leases are exact; native MCP lifetimes have Codex's unload grace."""
    def __init__(self, store):
        self.store = store
        initialize_background(store)

    def connect(self, ident, thread_id, *, mode='native', pid=None, identity=None, ttl=None, connected_at=None):
        if not thread_id or len(thread_id) > 200 or mode not in ('native', 'lease'):
            raise ValueError("无效会话连接")
        pid = os.getpid() if pid is None else pid
        identity = process_identity(pid) if identity is None else identity
        with self.store.transaction(timeout=2) as db:
            db.execute("""INSERT INTO connections
                (id,thread_id,mode,owner_pid,owner_identity,connected_at,seen_at,expires_at,state)
                VALUES(?,?,?,?,?,?,?,?,'active') ON CONFLICT(id) DO UPDATE SET
                thread_id=excluded.thread_id,owner_pid=excluded.owner_pid,owner_identity=excluded.owner_identity,
                connected_at=CASE WHEN connections.state='closed' THEN excluded.connected_at ELSE connections.connected_at END,
                seen_at=excluded.seen_at,expires_at=excluded.expires_at,state='active'""",
                (ident, thread_id, mode, pid, identity, connected_at or now(), now(), time.time() + ttl if ttl else None))

    def disconnect(self, ident):
        with self.store.transaction(timeout=2) as db:
            db.execute("UPDATE connections SET state='closed',seen_at=? WHERE id=?", (now(), ident))

    def import_sessions(self):
        # Also supports an already-connected MCP process running the previous code version.
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM sessions WHERE host_session_id IS NOT NULL").fetchall()
        for row in rows:
            if row['status'] == 'active' and process_alive(row['owner_pid'], row['owner_identity']):
                self.connect(row['id'], row['host_session_id'], pid=row['owner_pid'], identity=row['owner_identity'], connected_at=row['opened_at'])
            else:
                self.disconnect(row['id'])

    def online(self, loaded):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM connections ORDER BY connected_at DESC,id DESC").fetchall()
        # If a thread uses our connection-aware CLI proxy, its lease overrides
        # the server's lingering native MCP connection even after it closes.
        leased = {}
        for row in rows:
            if row['mode'] == 'lease':
                leased[row['thread_id']] = max(leased.get(row['thread_id'], ''), row['seen_at'])
        seen = set()
        online = []
        for row in rows:
            thread = row['thread_id']
            if thread in seen or thread not in loaded or row['state'] != 'active':
                continue
            if (thread in leased and row['mode'] != 'lease'
                    and row['connected_at'] <= leased[thread]):
                continue
            if row['expires_at'] is not None and row['expires_at'] <= time.time():
                continue
            if not process_alive(row['owner_pid'], row['owner_identity']):
                continue
            seen.add(thread)
            online.append(thread)
        return online


def daemon_status(store):
    from .processing import status
    with store.connect() as db:
        processing = status(store, db)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='daemon_state'").fetchone():
            return {'running': False, 'status': 'not_installed', **processing}
        row = db.execute("SELECT * FROM daemon_state WHERE singleton=1").fetchone()
    result = dict(row) if row else {'status': 'stopped'}
    result.update(processing)
    result['running'] = bool(row and process_alive(row['pid'], row['identity'])
                             and row['heartbeat'] and time.time() - row['heartbeat'] < 60
                             and row['status'] == 'running')
    return result


def require_background_for_task(store, task_id):
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='async_tasks'").fetchone()
        detached = bool(exists and db.execute('SELECT 1 FROM async_tasks WHERE task_id=?', (task_id,)).fetchone())
    if detached and not daemon_status(store)['running']:
        raise RuntimeError('此任务属于后台服务，请先 daemon install/start 后恢复')
    return detached
