"""Independent rules, fixed enums and hashes only; never restarts/cancels work."""
import contextlib
import hashlib
import json
import time
from .common import new_id, now
from .features import enabled
from .policy import settings
from .metrics import seconds, usage

PHASES = {'working','tool','long_tool','approval_wait','user_wait'}
TOOL_TYPES = {'command_execution','mcp_tool_call','web_search','file_change','commandExecution','mcpToolCall','webSearch','fileChange','toolCall','image_generation','imageGeneration'}
LONG_TOOLS = {'imagegen','image_gen','image_gen__imagegen','imagegen_generate','deep_research'}


def event_metadata(value):
    kind = value.get('type') or value.get('method')
    params = value.get('params')
    params = params if isinstance(params,dict) else {}
    item = value.get('item') or params.get('item') or {}
    result = {'client_event': True}
    if isinstance(item,dict) and item.get('type') in TOOL_TYPES:
        if isinstance(item.get('id'),str) and kind in ('item.started','item/started','item.completed','item/completed'):
            result['tool_id']=hashlib.sha256(item['id'].encode()).hexdigest()
            result['tool_state']='started' if kind in ('item.started','item/started') else 'completed'
        if kind in ('item.started','item/started'):
            tool = item.get('tool') or item.get('name') or item.get('type')
            result['phase'] = 'long_tool' if isinstance(tool,str) and any(name in tool.lower() for name in LONG_TOOLS) else 'tool'
        elif kind in ('item.completed','item/completed'):
            ident = item.get('id')
            if isinstance(ident,str) and ident:
                result['progress_key'] = 'tool:' + hashlib.sha256(ident.encode()).hexdigest()
            result['phase'] = 'working'
    if kind in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
        result['phase'] = 'approval_wait'
    elif kind == 'item/tool/requestUserInput':
        result['phase'] = 'user_wait'
    raw_usage = value.get('usage')
    if kind == 'thread/tokenUsage/updated':
        token_usage=params.get('tokenUsage')
        raw_usage = token_usage.get('last') if isinstance(token_usage,dict) else None
    result['usage'] = usage(raw_usage)
    return result


@contextlib.contextmanager
def transaction(store):
    # Rules never wait behind the dispatcher's advisory lock held during IO.
    # SQLite transactions still serialize forgetting/restoring and rule writes.
    with contextlib.closing(store.connect(timeout=2)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def checkpoint_progress(store, task_id, content):
    if not (enabled(store,'monitoring') or enabled(store,'metrics')):
        return
    with transaction(store) as db:
        run = db.execute("SELECT id FROM runs WHERE task_id=? AND status IN ('starting','running') ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
        if run:
            key = 'checkpoint:' + hashlib.sha256(json.dumps(content,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            _progress(db,run[0],key,time.time())
            db.execute('UPDATE monitor_instances SET progress=? WHERE id=? AND finished IS NULL',(time.time(),run[0]))


def slack_checkpoint_progress(store, access, input_id):
    if not input_id or not (enabled(store,'monitoring') or enabled(store,'metrics')):
        return
    with transaction(store) as db:
        channel = access.validate(db)
        # The summary belongs to the input captured at commit, even if another
        # input has started or the channel has been revoked in the meantime.
        source = db.execute("SELECT id FROM slack_inbox WHERE id=? AND team_id=? AND channel_id=? AND generation=? AND status IN ('processing','running')",
                            (input_id,channel['team_id'],channel['channel_id'],channel['generation'])).fetchone()
        if source:
            db.execute("UPDATE monitor_instances SET progress=? WHERE id=? AND input_id=? AND origin=? AND generation=? AND kind='slack' AND finished IS NULL",
                       (time.time(),'slack:'+input_id,input_id,access.conversation_id,channel['generation']))


def _progress(db, ident, key, stamp):
    if db.execute('INSERT OR IGNORE INTO monitor_progress VALUES(?,?)',(ident,hashlib.sha256(key.encode()).hexdigest())).rowcount:
        db.execute('UPDATE monitor_instances SET progress=? WHERE id=? AND finished IS NULL',(stamp,ident))


class Monitor:
    def __init__(self,store,clock=time.time):
        self.store,self.clock=store,clock

    def start(self,ident,kind,*,task_id=None,origin=None,input_id=None,context='normal',audience=None,generation=None,started=None,db=None):
        if not (enabled(self.store,'monitoring') or enabled(self.store,'metrics')):
            return
        if db is None:
            with transaction(self.store) as conn:
                return self.start(ident,kind,task_id=task_id,origin=origin,input_id=input_id,context=context,
                                  audience=audience,generation=generation,started=started,db=conn)
        stamp=self.clock() if started is None else started
        db.execute('INSERT OR IGNORE INTO monitor_instances(id,kind,task_id,origin,input_id,context,audience,generation,started,progress,phase_since) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (ident,kind,task_id,origin,input_id,context,audience,generation,stamp,stamp,stamp))

    def observe(self,ident,*,heartbeat=False,client_event=False,phase=None,progress_key=None,finished=False,tool_id=None,tool_state=None,**unused):
        if not (enabled(self.store,'monitoring') or enabled(self.store,'metrics')):
            return
        stamp=self.clock()
        with transaction(self.store) as db:
            if heartbeat:
                db.execute('UPDATE monitor_instances SET heartbeat=? WHERE id=? AND finished IS NULL',(stamp,ident))
            if client_event:
                db.execute('UPDATE monitor_instances SET client_event=? WHERE id=? AND finished IS NULL',(stamp,ident))
            if tool_id and tool_state in ('started','completed'):
                db.execute('INSERT OR IGNORE INTO monitor_tools VALUES(?,?,?,?,?)',(ident,tool_id,phase or 'tool',stamp,stamp if tool_state=='completed' else None))
                if tool_state=='completed':
                    db.execute('UPDATE monitor_tools SET finished=? WHERE instance_id=? AND tool_id=?',(stamp,ident,tool_id))
                active=db.execute('SELECT phase,min(started) AS started FROM monitor_tools WHERE instance_id=? AND finished IS NULL GROUP BY phase',(ident,)).fetchall()
                phase='long_tool' if any(t['phase']=='long_tool' for t in active) else 'tool' if active else 'working'
            if phase in PHASES:
                db.execute('UPDATE monitor_instances SET phase=?,phase_since=? WHERE id=? AND phase!=? AND finished IS NULL',(phase,stamp,ident,phase))
            if progress_key:
                _progress(db,ident,progress_key,stamp)
            if finished:
                db.execute("UPDATE monitor_instances SET finished=?,state='finished' WHERE id=? AND finished IS NULL",(stamp,ident))
                db.execute('UPDATE monitor_alerts SET cleared=? WHERE instance_id=? AND cleared IS NULL',(stamp,ident))

    def _discover(self,db):
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'runs' in tables:
            for r in db.execute("SELECT r.*,s.host_session_id FROM runs r LEFT JOIN sessions s ON s.id=r.session_id WHERE r.status IN ('starting','running')").fetchall():
                origin,context,input_id,audience,generation=r['host_session_id'],'normal',None,None,None
                if 'execution_routes' in tables:
                    route=db.execute('SELECT context FROM execution_routes WHERE task_id=?',(r['task_id'],)).fetchone()
                    if route: context=route[0]
                if 'async_tasks' in tables:
                    route=db.execute('SELECT * FROM async_tasks WHERE task_id=?',(r['task_id'],)).fetchone()
                    if route: origin,context=route['origin_thread'],route['context']
                if 'slack_task_grants' in tables:
                    grant=db.execute('SELECT * FROM slack_task_grants WHERE task_id=?',(r['task_id'],)).fetchone()
                    if grant:
                        origin,input_id,audience=grant['conversation_id'],grant['input_id'],grant['audience']
                        generation=int(origin.rsplit(':',1)[1])
                self.start(r['id'],'worker',task_id=r['task_id'],origin=origin,input_id=input_id,context=context,audience=audience,generation=generation,started=seconds(r['started_at']),db=db)
        if 'slack_inbox' in tables:
            for r in db.execute("SELECT i.*,c.personal FROM slack_inbox i JOIN slack_channels c USING(team_id,channel_id) WHERE i.status IN ('processing','running','uncertain')").fetchall():
                self.start('slack:'+r['id'],'slack',origin=f"slack:{r['team_id']}:{r['channel_id']}:{r['generation']}",input_id=r['id'],context=r['context'],audience='personal' if r['personal'] else 'shared',generation=r['generation'],started=seconds(r['updated_at']),db=db)

    def scan(self):
        if not (enabled(self.store,'monitoring') or enabled(self.store,'metrics')):
            return []
        stamp=self.clock()
        policy=settings(self.store)
        created=[]
        with transaction(self.store) as db:
            self._discover(db)
            if not enabled(self.store,'monitoring'):
                return []
            for row in db.execute('SELECT * FROM monitor_instances WHERE finished IS NULL').fetchall():
                if row['kind']=='worker':
                    source=db.execute('SELECT status FROM runs WHERE id=?',(row['id'],)).fetchone()
                else:
                    source=db.execute('SELECT status FROM slack_inbox WHERE id=?',(row['input_id'],)).fetchone()
                if not source or source[0] not in ('starting','running','processing','uncertain'):
                    db.execute("UPDATE monitor_instances SET finished=?,state='finished' WHERE id=?",(stamp,row['id']))
                    db.execute('UPDATE monitor_alerts SET cleared=? WHERE instance_id=? AND cleared IS NULL',(stamp,row['id']))
                    continue
                phase=row['phase']
                elapsed=stamp-row['progress']
                state=None
                if phase in ('approval_wait','user_wait'):
                    state=phase if stamp-row['phase_since'] >= policy['waiting_seconds'] else None
                elif phase in ('tool','long_tool'):
                    active=db.execute('SELECT min(started) FROM monitor_tools WHERE instance_id=? AND finished IS NULL',(row['id'],)).fetchone()[0]
                    since=active if active is not None else row['phase_since']
                    state='long_tool' if stamp-max(since,row['progress']) >= policy['tool_seconds'] else None
                elif elapsed >= policy['working_seconds']:
                    state='stalled'
                db.execute('UPDATE monitor_alerts SET cleared=? WHERE instance_id=? AND cleared IS NULL AND type!=?',(stamp,row['id'],state or ''))
                db.execute('UPDATE monitor_instances SET state=? WHERE id=?',(state or 'active',row['id']))
                if state:
                    ident=new_id('alert')
                    if db.execute('INSERT OR IGNORE INTO monitor_alerts(id,instance_id,type,created) VALUES(?,?,?,?)',(ident,row['id'],state,stamp)).rowcount:
                        created.append(ident)
                for alert in db.execute('SELECT * FROM monitor_alerts WHERE instance_id=? AND notified=0 AND cleared IS NULL',(row['id'],)).fetchall():
                    if self._notify(db,row,alert):
                        db.execute('UPDATE monitor_alerts SET notified=1 WHERE id=?',(alert['id'],))
            for retry in db.execute("SELECT * FROM read_retries WHERE state='suspended'").fetchall():
                instance='read:'+retry['id']
                ident=new_id('alert')
                if db.execute('INSERT OR IGNORE INTO monitor_alerts(id,instance_id,type,created) VALUES(?,?,?,?)',
                              (ident,instance,'read_suspended',stamp)).rowcount:
                    created.append(ident)
                # Route only from an existing, trusted source; never infer an
                # audience or thread from error text/provider messages.
                route=self._retry_route(db,retry['id'])
                if route:
                    for alert in db.execute('SELECT * FROM monitor_alerts WHERE instance_id=? AND notified=0',(instance,)).fetchall():
                        if self._notify(db,route,alert):
                            db.execute('UPDATE monitor_alerts SET notified=1 WHERE id=?',(alert['id'],))
        return created

    def _retry_route(self,db,key):
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'notifications' in tables:
            row=db.execute("SELECT * FROM notifications WHERE ? LIKE id||':%'",(key,)).fetchone()
            if row and row['origin_thread'] and not row['origin_thread'].startswith('slack:'):
                return dict(origin=row['origin_thread'],context=row['context'],task_id=row['task_id'])
        if 'slack_inbox' in tables:
            row=db.execute("SELECT i.*,c.personal FROM slack_inbox i JOIN slack_channels c USING(team_id,channel_id) WHERE ? LIKE i.id||':%' OR i.id IN (SELECT input_id FROM slack_outbox WHERE ? LIKE id||':%') LIMIT 1",(key,key)).fetchone()
            if row:
                return dict(origin=f"slack:{row['team_id']}:{row['channel_id']}:{row['generation']}",context=row['context'],
                            audience='personal' if row['personal'] else 'shared',generation=row['generation'],input_id=row['id'],task_id=None)
        return None

    def _notify(self,db,row,alert):
        labels={'stalled':'超过工作进展提醒阈值，没有可核实的新进展','long_tool':'工具运行超过长操作提醒阈值，等待结果','approval_wait':'正在等待操作确认','user_wait':'正在等待用户补充信息','read_suspended':'只读状态核对已挂起，需要检查诊断后处理'}
        message='运行提醒：'+labels[alert['type']]+'。执行状态未更改；可查询诊断。'
        if row['context']=='root': message='<root>'+message+'</root>'
        if (row['origin'] or '').startswith('slack:'):
            from .access import channel_access
            try:
                channel_access(db,row['origin'],row['audience'])
            except PermissionError:
                return False
            source=db.execute('SELECT id,generation FROM slack_inbox WHERE id=?',(row['input_id'],)).fetchone()
            if source and source['generation'] != row['generation']:
                return False
            if not source and row['task_id']:
                # Scheduled Slack workers have a durable task grant but no user
                # inbox row. Create routing metadata already marked completed;
                # this never enters the model queue or copies an input body.
                grant=db.execute('SELECT * FROM slack_task_grants WHERE task_id=?',(row['task_id'],)).fetchone()
                if not grant or grant['conversation_id'] != row['origin']:
                    return False
                _,team,channel,generation=row['origin'].split(':')
                source_id='monitorin:'+alert['id']
                db.execute("INSERT OR IGNORE INTO slack_inbox(id,team_id,channel_id,message_ts,reply_ts,body,context,status,created_at,updated_at,generation) VALUES(?,?,?,?,?,'',?,'completed',?,?,?)",
                           (source_id,team,channel,source_id,grant['reply_thread'],row['context'],now(),now(),int(generation)))
                source={'id':source_id,'generation':int(generation)}
            if not source:
                return False
            part=int(hashlib.sha256(alert['id'].encode()).hexdigest()[:12],16)
            db.execute('INSERT OR IGNORE INTO slack_outbox(id,input_id,part,body,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                       ('monitor:'+alert['id'],source['id'],part,message,now(),now()))
            db.execute("INSERT OR IGNORE INTO read_sources VALUES('output',?,'pending')",('monitor:'+alert['id'],))
            return True
        if not row['origin']:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='async_tasks'").fetchone() or not db.execute('SELECT 1 FROM async_tasks WHERE task_id=?',(row['task_id'],)).fetchone():
                return False
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='notifications'").fetchone():
            return False
        db.execute('INSERT OR IGNORE INTO notifications(id,event_key,digest,origin_thread,context,topic,message,task_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (alert['id'],alert['id'],hashlib.sha256(message.encode()).hexdigest(),row['origin'],row['context'],'monitor.'+alert['type'],message,row['task_id'],now(),now()))
        return True


def diagnostic(db,task_id):
    rows=[dict(r) for r in db.execute('SELECT * FROM monitor_instances WHERE task_id=?',(task_id,))]
    alerts=[dict(r) for r in db.execute('SELECT a.* FROM monitor_alerts a JOIN monitor_instances m ON m.id=a.instance_id WHERE m.task_id=?',(task_id,))]
    return {'instances':rows,'alerts':alerts}
