"""One admission policy for operator pauses and durable forget barriers.

The legacy paused bit remains an operator/reconciliation hold. Forgetting uses
the write-ahead ledger, so rollback cannot reopen dispatch before cleanup.
Callers admitting work must hold the store lock through their local mutation.
"""
import contextlib


class ProcessingPaused(RuntimeError):
    pass


def epoch(db):
    row = db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone()
    return row[0] if row else None


def status(store, db=None):
    if db is None:
        with contextlib.closing(store.connect()) as connection:
            return status(store, connection)
    reasons, hints = [], []
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='background_meta'").fetchone():
        meta = dict(db.execute("SELECT key,value FROM background_meta WHERE key IN ('paused','pause_reason')"))
        if meta.get('paused') == '1':
            reason = meta.get('pause_reason', 'legacy_unknown')
            if reason not in ('manual', 'restore'):
                reason = 'legacy_unknown'
            reasons.append(reason)
            hints.append({'manual': '用户显式暂停；需要继续时执行 daemon resume。',
                          'restore': '备份恢复后待核对；核实旧任务、通知和计划后 daemon resume，Slack 需另行 enable。',
                          'legacy_unknown': '旧 paused=1 无来源，无法区分手动暂停与历史遗忘；核实遗忘账本、旧队列及计划后 daemon resume。'}[reason])
    try:
        ledger = store._ledger()
        if ledger['pending']:
            reasons.append('forget_pending')
        elif (ledger['ids'] or ledger['cleanup_events']) and not store._forget_applied(db, ledger):
            reasons.append('forget_unapplied')
    except (OSError, ValueError, TypeError):
        reasons.append('forget_ledger_invalid')
    maintenance = any(r.startswith('forget_') for r in reasons)
    if maintenance:
        hints.append('遗忘尚未确认完成；禁止模型与内容投递。修复清理/存储故障并重新打开 Store 以重试清理；daemon resume 不能越过此屏障。')
    return {'paused': bool(reasons), 'reasons': reasons, 'maintenance': maintenance,
            'recovery': hints}


def require(store, db, *, background=True):
    state = status(store, db)
    if state['maintenance'] or (background and state['paused']):
        raise ProcessingPaused('处理已暂停: ' + ','.join(state['reasons']))


def set_operator_pause(store, paused):
    with store.transaction() as db:
        state = status(store, db)
        if not paused and state['maintenance']:
            raise RuntimeError('遗忘尚未完成，不能 daemon resume')
        db.execute("INSERT OR REPLACE INTO background_meta VALUES('paused',?)", ('1' if paused else '0',))
        db.execute("INSERT OR REPLACE INTO background_meta VALUES('pause_reason','manual')")
        return status(store, db)


def hold_for_restore(db):
    # Restore requires explicit reconciliation even if the archive was captured
    # while processing ran; forgetting may never clear this hold.
    db.execute("INSERT OR REPLACE INTO background_meta VALUES('paused','1')")
    db.execute("INSERT OR REPLACE INTO background_meta VALUES('pause_reason','restore')")
