"""Route shared outbox events to eligible Slack endpoints without model calls."""
from __future__ import annotations

import time

from .common import now
from .slack_state import SlackState, conversation_id


def route_slack_notifications(store, native_online=()):
    # This is local routing only. Keep its event/channel snapshots and enqueue
    # atomic with forgetting, so a cached event cannot recreate an erased inbox.
    from .processing import status
    with store._lock():
        if status(store)['paused']:
            return 0
        return _route_slack_notifications(store, native_online)


def _route_slack_notifications(store, native_online):
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='slack_meta'").fetchone():
            return 0
    state = SlackState(store)
    if not state.config().get('enabled'):
        return 0
    with store.connect() as db:
        runtime = db.execute("SELECT value FROM slack_meta WHERE key='runtime'").fetchone()
        import json
        runtime = json.loads(runtime[0]) if runtime else {}
        if runtime.get('status') != 'connected' or time.time() - runtime.get('heartbeat', 0) > 60:
            return 0
        if native_online:
            placeholders = ','.join('?' for _ in native_online)
            native = db.execute('SELECT max(connected_at) FROM connections WHERE thread_id IN (' + placeholders + ') AND state=\'active\'', tuple(native_online)).fetchone()[0]
        else:
            native = None
        from datetime import datetime
        native_activity = datetime.fromisoformat(native).timestamp() if native else 0
        events = [dict(r) for r in db.execute("SELECT * FROM notifications WHERE status='pending' AND next_attempt<=? ORDER BY created_at LIMIT 100", (time.time(),))]
    routed = 0
    for event in events:
        origin = event['origin_thread']
        if origin and not origin.startswith('slack:'):
            continue
        # Native sources without a declared audience are private by default.
        with store.connect() as db:
            grant = db.execute('SELECT * FROM slack_task_grants WHERE task_id=?', (event['task_id'],)).fetchone() if event['task_id'] else None
        audience = grant['audience'] if grant else 'personal'
        if not grant and origin and origin.startswith('slack:'):
            parts = origin.split(':')
            current = state.channel(parts[1], parts[2]) if len(parts) == 4 else None
            # A directly emitted origin event was already scoped by its caller.
            if current and str(current['generation']) == parts[3]:
                audience = 'personal' if current['personal'] else 'shared'
        candidates = [c for c in state.channels() if c['eligible'] and c['last_activity']
                      and (audience == 'shared' or c['personal'])]
        if origin:
            parts = origin.split(':')
            if len(parts) != 4:
                continue
            candidates = [c for c in candidates if (c['team_id'], c['channel_id']) == (parts[1], parts[2])]
        else:
            candidates = [c for c in candidates if c['last_activity'] > native_activity]
        if not candidates:
            continue
        target = max(candidates, key=lambda c: c['last_activity'])
        # Audience may never be silently downgraded after a membership change.
        if grant and audience == 'personal' and not target['personal']:
            continue
        reply = grant['reply_thread'] if grant else None
        if grant is None:
            with store.connect() as db:
                last = db.execute('SELECT reply_ts FROM slack_inbox WHERE team_id=? AND channel_id=? AND origin_notice IS NULL ORDER BY created_at DESC LIMIT 1', (target['team_id'], target['channel_id'])).fetchone()
                reply = last[0] if last else None
        # Claim and enqueue in separate durable transactions: if interrupted the
        # stable origin_notice key lets the next tick complete the same claim.
        item = state.enqueue(target['team_id'], target['channel_id'], 'notice:' + event['id'],
                             event['message'], reply_ts=reply, context=event['context'], origin_notice=event['id'])
        with store.transaction() as db:
            if event['task_id']:
                # Delivery authorizes reading only this result, never arbitrary
                # native tasks or mutation of the source task.
                from .acceptance import result_version
                task = db.execute('SELECT result FROM tasks WHERE id=?',(event['task_id'],)).fetchone()
                run = db.execute('SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1',(event['task_id'],)).fetchone()
                db.execute('INSERT OR IGNORE INTO slack_result_versions VALUES(?,?,?,?)',(event['id'],event['task_id'],run[0] if run else None,result_version(json.loads(task[0]) if task and task[0] else None)))
                db.execute('INSERT OR IGNORE INTO slack_result_grants VALUES(?,?,?,?)',
                           (event['task_id'], conversation_id(target), audience, event['id']))
            db.execute("UPDATE notifications SET target_thread=?,status='submitted',error=NULL,updated_at=? WHERE id=? AND status='pending'",
                       (conversation_id(target), now(), event['id']))
        routed += 1
    return routed
