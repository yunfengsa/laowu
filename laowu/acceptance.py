"""Independent review of a particular terminal handoff, never execution status."""
import hashlib
import json
from .common import now
from .features import enabled


def result_version(result):
    return hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def register(store, db, task_id):
    if enabled(store, 'task_acceptance'):
        db.execute('INSERT INTO task_acceptance(task_id,registered_at) VALUES(?,?)', (task_id, now()))


def snapshot(db, task_id):
    row = db.execute('SELECT * FROM task_acceptance WHERE task_id=?', (task_id,)).fetchone()
    if not row:
        return {'state': 'legacy'}
    history = [dict(r, evidence=json.loads(r['evidence'])) for r in db.execute(
        'SELECT * FROM task_reviews WHERE task_id=? ORDER BY submitted_at,rowid', (task_id,))]
    return dict(row, history=history)


def finish(db, task_id, run_id, result):
    db.execute('UPDATE task_acceptance SET state=?,run_id=?,result_version=? WHERE task_id=?',
               ('pending' if result else 'not_ready', run_id, result_version(result) if result else None, task_id))


def reset(db, task_id):
    db.execute("UPDATE task_acceptance SET state='not_ready',run_id=NULL,result_version=NULL WHERE task_id=?", (task_id,))


def review(store, task_id, run_id, decision, summary, evidence, *, access=None, manager=None):
    from .service import text_check
    if not enabled(store, 'task_acceptance'):
        raise ValueError('任务验收功能尚未启用')
    if decision not in ('accepted', 'needs_work'):
        raise ValueError('decision 须为 accepted 或 needs_work')
    text_check(summary, 4000)
    if not summary.strip():
        raise ValueError('验收摘要不能为空白')
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 100 or not all(isinstance(e, str) and e.strip() for e in evidence):
        raise ValueError('evidence 须为1至100条非空验证证据')
    for item in evidence:
        text_check(item, 2000)
    with store.transaction() as db:
        if manager is not None:
            manager._require_active(db)
        if access:
            access.require_task(task_id, db)
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        run = db.execute('SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1', (task_id,)).fetchone()
        entry = db.execute('SELECT * FROM task_acceptance WHERE task_id=?', (task_id,)).fetchone()
        if not entry:
            raise ValueError('legacy 任务不登记新验收')
        if (not task or not run or run['id'] != run_id or task['status'] not in ('completed','blocked','failed','interrupted','cancelled')
                or run['status'] in ('starting','running') or not task['result']):
            raise ValueError('只能验收最新运行的有效终态交接')
        version = result_version(json.loads(task['result']))
        if entry['run_id'] != run_id or entry['result_version'] != version:
            raise ValueError('交接版本已变化，请重新读取任务')
        old = db.execute('SELECT * FROM task_reviews WHERE task_id=? AND run_id=? AND result_version=?', (task_id,run_id,version)).fetchone()
        evidence_json = json.dumps(evidence, ensure_ascii=False)
        if old:
            if (old['decision'],old['summary'],old['evidence']) != (decision,summary,evidence_json):
                raise ValueError('同一交接已经验收，冲突提交被拒绝')
        else:
            db.execute('INSERT INTO task_reviews VALUES(?,?,?,?,?,?,?)', (task_id,run_id,version,decision,summary,evidence_json,now()))
            db.execute('UPDATE task_acceptance SET state=? WHERE task_id=?', (decision,task_id))
        outcome=snapshot(db, task_id)
        route = db.execute('SELECT a.origin_thread,a.context FROM async_tasks a WHERE a.task_id=?',(task_id,)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE name='async_tasks'").fetchone() else None
    if route:
        from .background import Outbox
        Outbox(store).publish(f'任务 {task_id} 执行已结束，验收状态：{decision}。请依据验收记录向用户说明。',
                              event_key=f'review:{task_id}:{run_id}:{decision}',topic='task.review.'+decision,
                              origin_thread=route['origin_thread'],context=route['context'],task_id=task_id)
    return outcome


def pending_tasks(db, conversation_id=None):
    where = "(t.status!='completed' OR a.state IN ('pending','needs_work')) AND coalesce(a.state,'legacy')!='accepted'"
    args = ()
    join = ''
    if conversation_id:
        join = ' JOIN slack_task_grants g ON g.task_id=t.id'
        where += ' AND g.conversation_id=?'
        args = (conversation_id,)
    return [dict(r) for r in db.execute('SELECT t.id,t.friend_id,t.status,t.updated_at,coalesce(a.state,\'legacy\') AS acceptance_state '
        'FROM tasks t LEFT JOIN task_acceptance a ON a.task_id=t.id' + join + ' WHERE ' + where + ' ORDER BY t.updated_at DESC', args)]
