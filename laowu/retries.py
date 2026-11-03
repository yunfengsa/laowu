"""Persistent retry budget for explicitly allowlisted observations only."""
import time
from contextvars import ContextVar
_READ_DEADLINE = ContextVar("laowu_read_deadline", default=None)


def remaining(default):
    budget = _READ_DEADLINE.get()
    if budget is None:
        return default
    clock, deadline = budget
    left = deadline - clock()
    if left <= 0:
        raise TimeoutError("read_budget_exhausted")
    return min(default, left)

from .features import enabled
from .policy import settings, DEFAULTS

OPERATIONS={'bridge.connect','bridge.loaded','bridge.state','bridge.receipt','slack.receipt','slack.reconcile','slack.qualify','slack.backfill','slack.context'}
DELAYS=tuple(DEFAULTS['read_delays_seconds'])


class ReceiptUnavailable(ConnectionError):
    """The original stable ID is not yet observable; never resend the action."""


class RetryPending(RuntimeError):
    pass


class RetrySuspended(RuntimeError):
    pass


def temporary(exc):
    if isinstance(exc,(TimeoutError,ConnectionError)):
        return 'network'
    import errno
    if isinstance(exc,OSError) and exc.errno in (errno.ECONNREFUSED,errno.ECONNRESET,errno.ETIMEDOUT,errno.ENETUNREACH,errno.EHOSTUNREACH):
        return 'network'
    diagnostic=getattr(exc,'diagnostic',{})
    category=diagnostic.get('category')
    if category in ('network','stream','server','rate_limit') and diagnostic.get('basis') == 'structured':
        return category
    code=getattr(exc,'code',None)
    if code in ('ratelimited','http_429'):
        return 'rate_limit'
    if code in ('network_error','request_timeout','connection_failed','transport_uncertain','connection_lost'):
        return 'network'
    if code in ('http_500','http_502','http_503','http_504','internal_error','service_unavailable','server_unavailable'):
        return 'server'
    return None


class ReadRetry:
    def __init__(self,store,clock=time.time):
        self.store,self.clock=store,clock

    def list(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT id,operation,state,started,attempts,total_attempts,resumptions,next_attempt,category FROM read_retries ORDER BY id')]

    def retry(self, key):
        """Explicit local maintenance: reopen observation, never resend an action."""
        stamp = self.clock()
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM read_retries WHERE id=?', (key,)).fetchone()
            if not row or row['state'] != 'suspended' or row['operation'] not in OPERATIONS:
                raise ValueError('仅挂起的白名单读取可显式恢复')
            db.execute("UPDATE read_retries SET state='ready',started=?,attempts=0,category=NULL,next_attempt=?,owner_pid=NULL,owner_identity=NULL,resumptions=resumptions+1 WHERE id=?", (stamp,stamp,key))
            db.execute('UPDATE monitor_alerts SET cleared=? WHERE instance_id=? AND cleared IS NULL', (stamp,'read:'+key))
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # Source states are durable before claims/submissions. Legacy or
            # ambiguous blocked rows lack proof of non-submission: reconcile.
            for kind, table in (('input','slack_inbox'), ('output','slack_outbox')):
                if table not in tables:
                    continue
                for source in db.execute('SELECT * FROM '+table+" WHERE status='blocked'").fetchall():
                    if not key.startswith(source['id']+':'):
                        continue
                    proof = db.execute('SELECT state FROM read_sources WHERE kind=? AND id=?',(kind,source['id'])).fetchone()
                    state = 'pending' if proof and proof[0] == 'pending' else 'uncertain'
                    extra = ',next_attempt=0' if kind == 'output' else ''
                    db.execute('UPDATE '+table+' SET status=?,error=NULL'+extra+' WHERE id=?', (state,source['id']))
            if 'notifications' in tables:
                # Native uncertain/submitted states are deliberately preserved.
                for source in db.execute('SELECT id FROM notifications').fetchall():
                    if key.startswith(source['id']+':'):
                        db.execute('UPDATE notifications SET next_attempt=0 WHERE id=?',(source['id'],))
            return dict(db.execute('SELECT id,operation,state,started,attempts,total_attempts,resumptions FROM read_retries WHERE id=?',(key,)).fetchone())

    def call(self,key,operation,fn):
        if operation not in OPERATIONS:
            raise PermissionError('自动重试只允许白名单状态或回执读取')
        if not enabled(self.store,'read_retries'):
            return fn()
        policy = settings(self.store)
        budget, maximum = policy['read_window_seconds'], policy['read_extra_attempts']
        delays = policy['read_delays_seconds']
        import os
        from .common import process_identity, process_alive
        stamp=self.clock()
        suspended=False
        retrying=False
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM read_retries WHERE id=?',(key,)).fetchone()
            if row:
                if row['operation'] != operation or row['state']=='suspended':
                    raise RetrySuspended('只读重试已挂起，需核实原因')
                if row['next_attempt']>stamp:
                    raise RetryPending('等待只读重试时间')
                if row['state']=='reading' and process_alive(row['owner_pid'],row['owner_identity']):
                    raise RetryPending('同一读取正在执行')
                retrying=row['state'] in ('waiting','reading')
                suspended=retrying and (stamp-row['started']>=budget or row['attempts']>=maximum)
                if suspended:
                    db.execute("UPDATE read_retries SET state='suspended',owner_pid=NULL,owner_identity=NULL WHERE id=?",(key,))
                else:
                    # Reservation is durable BEFORE calling the read, including
                    # its initial attempt. A crashed reader consumes its slot.
                    db.execute("UPDATE read_retries SET state='reading',started=CASE WHEN ? THEN started ELSE ? END,attempts=attempts+?,total_attempts=total_attempts+?,owner_pid=?,owner_identity=? WHERE id=?",
                               (int(retrying),stamp,int(retrying),int(retrying),os.getpid(),process_identity(os.getpid()),key))
            else:
                db.execute("INSERT INTO read_retries(id,operation,started,attempts,next_attempt,state,owner_pid,owner_identity) VALUES(?,?,?,0,?,'reading',?,?)",
                           (key,operation,stamp,stamp,os.getpid(),process_identity(os.getpid())))
        if suspended:
            raise RetrySuspended('只读重试预算耗尽')
        deadline = (row['started']+budget) if retrying and row else stamp+budget
        token = _READ_DEADLINE.set((self.clock,deadline))
        try:
            try:
                result=fn()
            finally:
                _READ_DEADLINE.reset(token)
        except Exception as exc:
            category=temporary(exc)
            after=getattr(exc,'retry_after',None)
            with self.store.transaction() as db:
                row=db.execute('SELECT * FROM read_retries WHERE id=?',(key,)).fetchone()
                if not row: raise RetrySuspended('恢复或遗忘已撤销本次读取') from None
                delay=delays[min(row['attempts'],len(delays)-1)]
                import math
                if type(after) in (int,float):
                    delay=max(delay,after) if math.isfinite(after) and after>=0 else budget+1
                # Start the outage window on the first failed observation; a
                # long healthy polling period does not eat its retry allowance.
                started=stamp if row['category'] is None and row['attempts']==0 else row['started']
                allowed=category is not None and row['attempts']<maximum and self.clock()+delay-started<=budget
                db.execute('UPDATE read_retries SET started=?,state=?,next_attempt=?,category=?,owner_pid=NULL,owner_identity=NULL WHERE id=?',
                           (started,'waiting' if allowed else 'suspended',self.clock()+delay,category or 'unknown',key))
            if allowed:
                raise RetryPending('临时读取故障，已安排有限重试') from None
            raise RetrySuspended('读取故障不可自动重试或已超预算') from None
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM read_retries WHERE id=?',(key,)).fetchone()
            if not row: raise RetrySuspended('恢复或遗忘已撤销本次读取')
            over=retrying and self.clock()-row['started']>=budget
            if over:
                db.execute("UPDATE read_retries SET state='suspended',owner_pid=NULL,owner_identity=NULL WHERE id=?", (key,))
            else:
                db.execute("UPDATE read_retries SET state='ready',started=?,attempts=0,category=NULL,next_attempt=?,owner_pid=NULL,owner_identity=NULL WHERE id=?", (self.clock(),self.clock(),key))
        if over:
            raise RetrySuspended('只读重试总时间超限')
        return result
