"""Numerical metadata only. Absent provider usage remains NULL."""
import time
from datetime import datetime
from .common import new_id
from .features import enabled

FIELDS = ('queue_seconds','execution_seconds','delivery_seconds','mcp_bytes','previews','pages',
          'input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens','total_tokens')
USAGE = {'input_tokens':'input_tokens','cached_input_tokens':'cached_input_tokens',
         'output_tokens':'output_tokens','reasoning_output_tokens':'reasoning_output_tokens','total_tokens':'total_tokens',
         'inputTokens':'input_tokens','cachedInputTokens':'cached_input_tokens',
         'outputTokens':'output_tokens','reasoningOutputTokens':'reasoning_output_tokens','totalTokens':'total_tokens'}


def usage(value):
    if not isinstance(value, dict):
        return {}
    return {dest:n for key,dest in USAGE.items() if type(n := value.get(key)) is int and 0 <= n <= 10**15}


def seconds(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def sample(store, subject, kind, *, sample_id=None, **values):
    if not enabled(store,'metrics'):
        return
    if kind not in ('mcp','worker','slack','delivery') or set(values) - set(FIELDS):
        raise ValueError('指标字段不在白名单')
    import math
    values = {k:v for k,v in values.items() if type(v) in (int,float) and math.isfinite(v) and v >= 0}
    with store.transaction() as db:
        db.execute('INSERT OR IGNORE INTO metric_samples(id,subject,kind,created,'+','.join(FIELDS)+') VALUES('+','.join('?' for _ in range(4+len(FIELDS)))+')',
                   (sample_id or new_id('metric'),subject,kind,time.time(),*(values.get(k) for k in FIELDS)))


def aggregate(store):
    with store.connect() as db:
        result = [dict(r) for r in db.execute('SELECT kind,count(*) AS samples,' + ','.join('sum('+f+') AS '+f for f in FIELDS) + ' FROM metric_samples GROUP BY kind')]
        return {'version':1,'samples':result,
                'alerts':db.execute('SELECT count(*) FROM monitor_alerts').fetchone()[0],
                'retry_attempts':db.execute('SELECT coalesce(sum(total_attempts),0) FROM read_retries').fetchone()[0]}


def begin(store,subject,kind,queued,stamp=None):
    if not enabled(store,'metrics'): return
    with store.transaction() as db:
        db.execute('INSERT OR IGNORE INTO metric_timings VALUES(?,?,?,?)',(subject,kind,queued,time.time() if stamp is None else stamp))


def finish(store,subject,kind,stamp=None,**values):
    if not enabled(store,'metrics'): return
    with store.connect() as db:
        row=db.execute('SELECT * FROM metric_timings WHERE subject=?',(subject,)).fetchone()
    if row:
        values['queue_seconds']=max(0,row['started']-row['queued']) if row['queued'] is not None else None
        values['execution_seconds']=max(0,(time.time() if stamp is None else stamp)-row['started'])
    sample(store,subject,kind,sample_id=kind+':'+subject,**values)
