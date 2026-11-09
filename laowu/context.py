"""MCP-only byte budgets and revocable references to existing stored objects.

No response bodies are cached. A reference contains typed database selectors and
field paths; a read reconstructs the object under the current principal.
"""
import hashlib
import json
import time
from .common import new_id
from .features import enabled
from .policy import settings, DEFAULTS

VERSION = 1
LIMIT = DEFAULTS['response_bytes']
BOOTSTRAP_LIMIT = DEFAULTS['bootstrap_bytes']
PAGE_LIMIT = DEFAULTS['page_bytes']
TTL = DEFAULTS['reference_ttl_seconds']


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def body_size(value):
    return len(dumps({'content': [{'type': 'text', 'text': dumps(value)}], 'isError': False}).encode())


def result_only(record):
    return {k: record[k] for k in ('id','friend_id','status','result','error','updated_at','runs','acceptance') if k in record}


class Context:
    def __init__(self, service, clock=time.time):
        self.service, self.store, self.clock = service, service.store, clock

    def principal(self):
        s = self.service
        if s.worker_run:
            s._worker()
            return 'worker:' + s.worker_run + ':' + digest(s.worker_token)
        if s.access:
            s.access.validate()
            return 'slack:' + s.access.token_hash + ':' + s.access.conversation_id
        return 'main:' + s.manager.id

    def object(self, kind, ident):
        ledger = self.store._ledger()
        if ledger.get('pending') or (kind == 'memory' and ident in ledger['ids']):
            raise PermissionError('遗忘已撤销引用；清理完成后重新检索')
        s = self.service
        from .runtime import task_record
        with self.store.connect() as db:
            if kind == 'memory':
                row = db.execute('SELECT * FROM memory_records WHERE id=?', (ident,)).fetchone()
                if not row:
                    raise ValueError('引用已失效，请重新检索')
                scopes = None
                if s.worker_run:
                    from .access import worker_scopes
                    scopes = worker_scopes(self.store, db, s._worker())
                elif s.access:
                    scopes = s.access.scopes(db)
                if scopes is not None and row['scope'] not in scopes:
                    raise PermissionError('引用记忆不可见')
                return self.store._record(db, row, scopes)
            if kind in ('task','result','checkpoint','review'):
                if s.worker_run:
                    if s._worker()['task_id'] != ident or kind != 'checkpoint':
                        raise PermissionError('朋友不能读取其他任务或主会话结果')
                elif s.access:
                    if kind == 'result':
                        s.access.require_result(ident)
                    else:
                        s.access.require_task(ident, db)
                task = task_record(self.store, ident)
                if kind == 'checkpoint':
                    return task['checkpoint']
                if kind == 'review':
                    return task['acceptance']
                return result_only(task) if kind == 'result' else task
            # These roots are only issued to main; Slack checks ownership again.
            if s.worker_run:
                raise PermissionError('朋友引用类型不可见')
            if kind == 'session':
                if s.access:
                    raise PermissionError('Slack 不可读取原生会话摘要')
                row = db.execute('SELECT id,summary,memory_ids,updated_at FROM sessions WHERE id=?', (ident,)).fetchone()
                if row:
                    return dict(row)
            elif kind == 'channel':
                if not s.access or ident != s.access.conversation_id:
                    raise PermissionError('频道引用不可见')
                return {'summary': s.access.validate(db)['summary']}
            elif kind == 'friend_definition':
                return s.registry.describe(ident)
            elif kind == 'friend':
                friends = s._slack_friends() if s.access else s.registry.list()
                for friend in friends:
                    if friend['id'] == ident:
                        return friend
            elif kind == 'notification':
                row = db.execute('SELECT * FROM notifications WHERE id=?', (ident,)).fetchone()
                if row:
                    if s.access and row['origin_thread'] != s.access.conversation_id:
                        raise PermissionError('通知引用不可见')
                    return dict(row)
            elif kind == 'schedule':
                if s.access and not db.execute('SELECT 1 FROM slack_schedule_grants WHERE schedule_id=? AND conversation_id=?', (ident,s.access.conversation_id)).fetchone():
                    raise PermissionError('计划引用不可见')
                from .scheduling import ScheduleBook
                for row in ScheduleBook(self.store).list():
                    if row['id'] == ident:
                        return row
        raise ValueError('引用对象已失效，请重新检索')

    def descriptor(self, value, tool, args):
        def obj(kind, ident):
            current = self.object(kind, ident)
            # Values may have changed since the read; issue only an exact binding.
            binding = {'object': kind, 'id': ident, 'revision': digest(current)}
            if kind in ('task','result'):
                # Snapshot only numerical/routing metadata. Streaming counters
                # cannot invalidate paging of an unchanged stored handoff.
                binding['frozen'] = {k:current[k] for k in ('runs','monitoring','updated_at') if k in current}
                binding['latest_run'] = current['runs'][-1]['id'] if current.get('runs') else None
            return binding
        if tool in ('task_status','task_wait','task_start','task_resume','task_cancel'):
            kind = 'result' if self.service.access and 'request' not in value else 'task'
            return obj(kind, value['id'])
        if tool == 'task_checkpoint':
            return obj('checkpoint', args.get('task_id') or self.service._worker()['task_id'])
        if tool == 'task_review':
            return obj('review', args['task_id'])
        if tool in ('friend_create', 'friend_get', 'friend_update', 'friend_set_active'):
            return obj('friend_definition', value['id'])
        if tool == 'memory_upsert':
            return obj('memory', value['id'])
        def tree(node, key=None):
            if isinstance(node, list):
                return {'list': [tree(v,key) for v in node]}
            if isinstance(node, dict):
                if 'id' in node:
                    if 'scope' in node and 'revision' in node:
                        return obj('memory', node['id'])
                    if key == 'recent_summaries':
                        return obj('session', node['id'])
                    if key == 'friends' or (tool in ('friend_list','friend_ensure') and key != 'templates'):
                        return obj('friend', node['id'])
                    if key == 'tasks':
                        # Bootstrap is a metadata projection, not a request grant.
                        return {'projection': list(node), 'source': obj('task',node['id'])}
                    if 'event_key' in node:
                        return obj('notification', node['id'])
                    if tool.startswith('schedule_') and 'trigger' in node:
                        return obj('schedule', node['id'])
                if key == 'recent_summaries' and self.service.access:
                    return obj('channel',self.service.access.conversation_id)
                return {'dict': {k: tree(v,k) for k,v in node.items()}}
            # Only bounded non-content metadata/literals are permitted in refs.
            if isinstance(node, str) and len(node.encode()) > 1024:
                raise ValueError('长对象无持久引用；请从所属存储对象重新读取')
            return {'literal': node}
        return tree(value)

    def resolve(self, desc):
        if 'object' in desc:
            value = self.object(desc['object'],desc['id'])
            if 'frozen' in desc:
                latest = value['runs'][-1]['id'] if value.get('runs') else None
                if latest != desc['latest_run']:
                    raise ValueError('引用运行已变化，请重新检索')
                value.update(desc['frozen'])
            if digest(value) != desc['revision']:
                raise ValueError('引用版本已变化，请重新检索')
            return value
        if 'projection' in desc:
            value = self.resolve(desc['source'])
            return {k: (value['acceptance']['state'] if k == 'acceptance_state' else value[k]) for k in desc['projection']}
        if 'list' in desc:
            return [self.resolve(v) for v in desc['list']]
        if 'dict' in desc:
            return {k:self.resolve(v) for k,v in desc['dict'].items()}
        return desc['literal']

    def present(self, tool, args, value):
        # The deletion intent survives a rolled-back database cleanup/epoch.
        # Hold the same lock as forgetting until the response is reconstructed.
        with self.store._lock():
            if self.store._ledger().get('pending'):
                raise PermissionError('遗忘清理尚未完成，停止展示上下文')
            return self._present(tool, args, value)

    def _present(self, tool, args, value):
        from .slack_files import TOOLS as FILE_TOOLS, file_content
        if tool in FILE_TOOLS:
            if not self.service.access or self.service.worker_run:
                raise PermissionError('文件响应仅供认证Slack主会话')
            self.service.access.validate()
            response = file_content(self.store, value)
            from .metrics import sample
            from .telemetry import best_effort
            best_effort(sample, self.store, self.service.manager.id, 'mcp',
                        mcp_bytes=len(dumps(response).encode()), previews=0, pages=1)
            return value
        if tool in {'slack_channels', 'slack_history', 'slack_thread'}:
            # External Slack pages have their own revocable, versioned cursors.
            # Never turn them into local context references or persist bodies.
            policy = settings(self.store)
            field = 'channels' if tool == 'slack_channels' else 'messages'
            if (body_size(value) > min(policy['response_bytes'], 16 * 1024)
                    or len(dumps(value.get(field, [])).encode()) > policy['page_bytes']):
                raise ValueError('Slack 响应超过当前数值策略预算，请重新读取')
            self.measure(tool, value, 0, 1)
            return value
        if tool == 'context_read':
            return value
        if not enabled(self.store,'mcp_budget'):
            self.measure(tool,value,0,0)
            return value
        policy = settings(self.store)
        limit = policy['bootstrap_bytes' if tool == 'bootstrap' else 'response_bytes']
        envelope = {'version': VERSION, 'data': value}
        if body_size(envelope) <= limit:
            self.measure(tool, envelope, 0, 0)
            return envelope
        with self.store._lock():
            principal = self.principal()
            desc = self.descriptor(value, tool, args)
            if digest(self.resolve(desc)) != digest(value):
                raise ValueError('响应对象版本已变化，请重新查询')
            references = []
            def preview(node, path):
                ref = new_id('ctx')
                references.append((ref,path))
                encoded = dumps(node)
                # Critical fields stay visible even when the entire root is long.
                critical = {}
                if isinstance(node, dict):
                    if isinstance(node.get('result'),dict) and 'unfinished' in node['result']:
                        unfinished=node['result']['unfinished']
                        critical['unfinished'] = {'preview':dumps(unfinished)[:180],'length':len(dumps(unfinished).encode()),'ref':ref,'path':['result','unfinished']}
                    if isinstance(node.get('acceptance'),dict):
                        critical['acceptance_state']=node['acceptance'].get('state')
                    for k in ('id','status','error','unfinished','acceptance','state','run_id'):
                        if k in node:
                            x = node[k]
                            critical[k] = x if len(dumps(x).encode()) <= 512 else {'preview': dumps(x)[:100], 'length':len(dumps(x).encode()), 'ref':ref, 'path':[k]}
                return {'preview': encoded[:180], 'length':len(encoded.encode()), 'encoding':'json',
                        'ref':ref, 'complete':False, **({'critical':critical} if critical else {})}
            def compact(node, path=()):
                if isinstance(node, str) and len(node.encode()) > 700:
                    return preview(node,path)
                if isinstance(node,list):
                    if len(node) > 12:
                        return preview(node,path)
                    return [compact(v,path+(i,)) for i,v in enumerate(node)]
                if isinstance(node,dict):
                    if len(node) > 40:
                        return preview(node,path)
                    return {k:compact(v,path+(k,)) for k,v in node.items()}
                return node
            envelope['data'] = compact(value)
            if body_size(envelope) > limit:
                references.clear()
                envelope['data'] = preview(value,())
            if body_size(envelope) > limit:
                raise ValueError('展示预算不足以容纳引用元数据，请调整数值策略')
            with self.store.transaction() as db:
                stamp = self.clock()
                db.execute('DELETE FROM context_refs WHERE expires<=?', (stamp,))
                epoch = db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone()[0]
                for ref,path in references:
                    db.execute('INSERT INTO context_refs VALUES(?,?,?,?,?,?)',
                               (ref,principal,epoch,dumps({'root':desc,'path':path}),digest(value),stamp+policy['reference_ttl_seconds']))
            self.measure(tool,envelope,len(references),0)
            return envelope

    def read(self, ref, cursor=None):
        policy = settings(self.store)
        limit, page_limit = policy['response_bytes'], policy['page_bytes']
        if not enabled(self.store,'mcp_budget'):
            raise ValueError('MCP 分页尚未启用')
        with self.store.transaction() as db:
            if self.store._ledger().get('pending'):
                raise PermissionError('遗忘已撤销引用，清理尚未完成')
            principal = self.principal()
            row = db.execute('SELECT * FROM context_refs WHERE id=?',(ref,)).fetchone()
            epoch = db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone()[0]
            if not row or row['principal'] != principal or row['epoch'] != epoch or row['expires'] <= self.clock():
                raise PermissionError('引用失效或不属于本会话，请重新检索')
            spec = json.loads(row['descriptor'])
            value = self.resolve(spec['root'])
            if digest(value) != row['digest']:
                raise ValueError('引用版本已变化，请重新检索')
            for part in spec['path']:
                value = value[part]
            encoded = dumps(value)
            offset = 0
            if cursor:
                page = db.execute('SELECT * FROM context_cursors WHERE id=? AND ref_id=?',(cursor,ref)).fetchone()
                if not page or page['expires'] <= self.clock():
                    raise PermissionError('分页游标已失效，请重新检索')
                offset = page['offset']
            low,high = 0,min(len(encoded)-offset,page_limit)
            while low < high:
                mid = (low+high+1)//2
                if len(dumps(encoded[offset:offset+mid]).encode()) <= page_limit and body_size({'version':VERSION,'ref':ref,'encoding':'json-fragment','offset':offset,'data':encoded[offset:offset+mid],'length':len(encoded.encode()),'next_cursor':'page_'+'x'*32,'complete':False}) <= limit:
                    low = mid
                else:
                    high = mid-1
            end = offset+low
            next_cursor = new_id('page') if end < len(encoded) else None
            if next_cursor:
                db.execute('INSERT INTO context_cursors VALUES(?,?,?,?,?)', (next_cursor,ref,'[]',end,row['expires']))
            result = {'version':VERSION,'ref':ref,'encoding':'json-fragment','offset':offset,
                      'data':encoded[offset:end],'length':len(encoded.encode()),'next_cursor':next_cursor,'complete':next_cursor is None}
            if body_size(result) > limit:
                raise ValueError('分页响应预算错误')
        self.measure('context_read',result,0,1)
        return result

    def measure(self, tool, value, previews, pages):
        from .metrics import sample
        from .telemetry import best_effort
        best_effort(sample, self.store, self.service.worker_run or self.service.manager.id, 'mcp',
               mcp_bytes=body_size(value),previews=previews,pages=pages)
