"""Trusted Slack identities and grants. Tool arguments never select an identity.

Slack workers currently have no filesystem tools: read-only/workspace-write
do not protect secrets from reads. Shell delegation is refused until an exact deny
profile has been independently verified on the installed Codex client.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path


def conversation_parts(value):
    match = re.fullmatch(r"slack:([A-Za-z0-9_-]+):([A-Za-z0-9_-]+):(0|[1-9][0-9]*)", value or "")
    if not match:
        raise PermissionError("无效 Slack 会话")
    team, channel, generation = match.groups()
    return team, channel, int(generation)


def channel_access(db, conversation_id, audience=None):
    team, channel, generation = conversation_parts(conversation_id)
    row = db.execute("SELECT * FROM slack_channels WHERE team_id=? AND channel_id=?", (team, channel)).fetchone()
    if not row or not row['eligible'] or row['generation'] != generation:
        raise PermissionError("Slack 频道授权已失效")
    expected = 'personal' if row['personal'] else 'shared'
    if audience is not None and audience != expected:
        raise PermissionError("Slack 受众与频道不符")
    return dict(row)


def task_access(store, task_id, *, db=None):
    if db is None:
        with store.connect() as conn:
            return task_access(store, task_id, db=conn)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='slack_task_grants'").fetchone():
        return None
    grant = db.execute("SELECT * FROM slack_task_grants WHERE task_id=?", (task_id,)).fetchone()
    if not grant:
        # A lost grant must not turn an existing Slack task into an unrestricted run.
        origin = db.execute("SELECT s.host_session_id FROM tasks t JOIN sessions s ON s.id=t.session_id WHERE t.id=?", (task_id,)).fetchone()
        if origin and (origin[0] or '').startswith('slack:'):
            raise PermissionError("Slack 任务缺少授权")
        return None
    result = dict(grant)
    channel_access(db, result['conversation_id'], result['audience'])
    # A durable task grant outlives the main stdio credential (revoked at gateway
    # restart). Channel revocation must change eligible/generation; worker-run
    # credentials themselves are checked against the live runs row.
    return result


def restrict_friend(friend, context, *, root=None, workspace=None):
    if friend.get('id') == 'laowu':
        raise PermissionError('laowu 不能作为 worker 身份')
    if not context:
        return friend
    if friend.get('mcp_servers'):
        raise PermissionError("Slack 朋友尚不允许额外 MCP 服务")
    if friend.get('can_write') or {'write', 'shell'} & set(friend.get('tools', [])):
        from .slack_permissions import probe
        if not root or not workspace or not probe(root, workspace):
            raise PermissionError("Slack 朋友文件隔离探针未通过，拒绝工程能力")
        return dict(friend, _slack_scoped=True, _slack_shell=True)
    # Even a private origin cannot give a researcher personal memory via shell.
    # 'read' enables the Codex shell tool in the legacy adapter.
    return dict(friend, tools=[t for t in friend.get('tools', []) if t != 'read'], _slack_scoped=True)


def worker_environment(environment):
    """Never forward connector credentials or principal paths to a worker."""
    return {k: v for k, v in environment.items()
            if not k.upper().startswith(('SLACK_', 'LAOWU_SLACK_'))
            and k not in {'CODEX_REMOTE_PAYLOAD', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
                          'LAOWU_SESSION_ID', 'CODEX_SANDBOX_PERMISSION_CONTEXT'}
            and not v.startswith(('xapp-', 'xoxb-', 'xoxp-', 'xoxs-'))}


def schedule_access(db, occurrence_id):
    """Existing daemon uses schedule:<occurrence> as its stable dispatch key."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='slack_schedule_grants'").fetchone():
        return None
    row = db.execute('''SELECT g.* FROM slack_schedule_grants g JOIN schedule_occurrences o
        ON o.schedule_id=g.schedule_id WHERE o.id=?''', (occurrence_id,)).fetchone()
    if not row:
        return None
    channel_access(db, row['conversation_id'], row['audience'])
    return dict(conversation_id=row['conversation_id'], audience=row['audience'],
                reply_thread=None, input_id='schedule:' + occurrence_id)


class SlackAccess:
    def __init__(self, store, token_file):
        self.store = store
        path = Path(token_file).absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise PermissionError("Slack 凭证路径不能包含符号链接")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) not in (0o400, 0o600):
                    raise PermissionError("Slack 凭证文件须由本用户持有且权限为 0600/0400")
                token = os.read(fd, 4097).decode().strip()
            finally:
                os.close(fd)
        except (OSError, UnicodeError) as exc:
            raise PermissionError("无法读取 Slack 凭证文件") from exc
        self._bind_token(token)

    @classmethod
    def from_token(cls, store, token):
        """Authenticate an opaque principal delivered over the local read socket."""
        instance = cls.__new__(cls)
        instance.store = store
        instance._bind_token(token)
        return instance

    def _bind_token(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 4096:
            raise PermissionError("Slack 凭证无效")
        self._token = token
        self.token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.conversation_id = None
        self.validate()

    def validate(self, db=None):
        if db is None:
            with self.store.connect() as conn:
                return self.validate(conn)
        from .processing import require
        require(self.store, db, background=False)
        row = db.execute("SELECT * FROM slack_bindings WHERE token_hash=?", (self.token_hash,)).fetchone()
        if not row or row['revoked']:
            raise PermissionError("Slack 凭证已撤销或无效")
        ident = f"slack:{row['team_id']}:{row['channel_id']}:{row['generation']}"
        if self.conversation_id is not None and self.conversation_id != ident:
            raise PermissionError("Slack 身份不能重新绑定")
        channel = channel_access(db, ident, 'personal' if row['personal'] else 'shared')
        self.conversation_id = ident
        self.audience = 'personal' if channel['personal'] else 'shared'
        return channel

    def scopes(self, db, actor=None, task_id=None):
        self.validate(db)
        return allowed_scopes(db, self.conversation_id, self.audience, actor, task_id)

    def require_task(self, task_id, db=None):
        if db is None:
            with self.store.connect() as conn:
                return self.require_task(task_id, conn)
        self.validate(db)
        row = task_access(self.store, task_id, db=db)
        if not row or row['conversation_id'] != self.conversation_id:
            raise PermissionError("任务不属于当前 Slack 会话")
        return row

    def require_result(self, task_id):
        """A routed event grants this result only, never task mutation."""
        with self.store.connect() as db:
            self.validate(db)
            own = db.execute('SELECT * FROM slack_task_grants WHERE task_id=?', (task_id,)).fetchone()
            if own:
                before = conversation_parts(own['conversation_id'])
                current = conversation_parts(self.conversation_id)
                if before == current and own['audience'] == self.audience:
                    return
            rows = db.execute('''SELECT v.* FROM slack_result_grants g JOIN notifications n ON n.id=g.notification_id
                JOIN slack_result_versions v ON v.task_id=g.task_id AND v.notification_id=g.notification_id
                WHERE g.task_id=? AND g.conversation_id=? AND g.audience=? AND n.status!='cancelled'
                AND n.target_thread=?''', (task_id, self.conversation_id, self.audience, self.conversation_id)).fetchall()
            import json
            from .acceptance import result_version
            task = db.execute('SELECT result FROM tasks WHERE id=?',(task_id,)).fetchone()
            run = db.execute('SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1',(task_id,)).fetchone()
            version = result_version(json.loads(task[0]) if task and task[0] else None)
            if not any(row['result_version'] == version and row['run_id'] == (run[0] if run else None) for row in rows):
                raise PermissionError('任务结果不属于当前 Slack 会话')

    def input_context(self):
        with self.store.connect() as db:
            channel = self.validate(db)
            row = db.execute("SELECT id,reply_ts FROM slack_inbox WHERE team_id=? AND channel_id=? AND generation=? AND status IN ('processing','running') ORDER BY created_at DESC LIMIT 1",
                             (channel['team_id'], channel['channel_id'], channel['generation'])).fetchone()
        if not row:
            raise PermissionError("当前 Slack 输入未绑定，不能委派任务")
        return dict(conversation_id=self.conversation_id, audience=self.audience,
                    reply_thread=row['reply_ts'], input_id=row['id'])


def allowed_scopes(db, conversation_id, audience, actor=None, task_id=None, *, personal=False):
    channel_access(db, conversation_id, audience)
    scopes = {'shared'}
    if audience == 'personal' and (actor is None or personal):
        scopes.add('personal')
    rows = db.execute("SELECT task_id FROM slack_task_grants WHERE conversation_id=? AND audience=?",
                      (conversation_id, audience))
    scopes.update('task:' + r[0] for r in rows if actor is None or r[0] == task_id)
    return scopes


def worker_scopes(store, db, run):
    """One authorization path for search and context continuation.

    The run policy is written by the scheduler, separately from model role text.
    Legacy runs keep only the shipped confidant's historical personal grant.
    """
    import json
    actor, task = run['friend_id'], run['task_id']
    if actor == 'laowu':
        raise PermissionError('worker 不能使用主身份')
    policy = json.loads(run['memory_policy_json'])
    personal = policy.get('personal', actor == 'confidant') is True
    scopes = {'shared', 'tasks', 'friend:' + actor, 'task:' + task}
    if personal:
        scopes.add('personal')
    grant = task_access(store, task, db=db)
    if grant:
        scopes &= allowed_scopes(db, grant['conversation_id'], grant['audience'], actor, task, personal=personal)
    return scopes


def require_memory_ids(db, ids, scopes):
    for ident in ids:
        row = db.execute("SELECT scope FROM memory_records WHERE id=?", (ident,)).fetchone()
        if not row or row[0] not in scopes:
            raise PermissionError("记忆不存在或不可见")


def cancel_input_tasks(store, conversation_id, input_id):
    """Transport stop: cancel only synchronous work issued by this exact input."""
    from .runtime import cancel_task
    with store.connect() as db:
        channel_access(db, conversation_id)
        has_async = db.execute("SELECT 1 FROM sqlite_master WHERE name='async_tasks'").fetchone()
        extra = " AND task_id NOT IN (SELECT task_id FROM async_tasks)" if has_async else ''
        ids = [r[0] for r in db.execute("SELECT task_id FROM slack_task_grants WHERE conversation_id=? AND input_id=?" + extra,
                                       (conversation_id, input_id))]
    return [cancel_task(store, ident) for ident in ids]
