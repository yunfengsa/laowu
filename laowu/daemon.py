"""One project service owns detached work, timers and the durable outbox."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .background import Outbox, Presence, daemon_status, initialize_background
from .codex_bridge import CodexBridge, socket_path
from .telemetry import best_effort
from .common import atomic_write, now, process_alive, process_identity
from .runtime import Manager, initialize_runtime, reconcile


def recover_interrupted(store):
    with store.transaction() as db:
        rows = db.execute("""SELECT t.id,t.error,s.status AS owner_status,s.owner_pid,s.owner_identity
            FROM tasks t JOIN async_tasks a ON t.id=a.task_id LEFT JOIN sessions s ON t.session_id=s.id
            WHERE t.status='interrupted' AND t.error IN ('owner_lost','session_closed','interrupted')""").fetchall()
        for row in rows:
            # The guardian may observe lease EOF before reconcile sees the dead
            # service, and records 'interrupted'. A live owner must never have
            # its explicitly interrupted worker restarted by this path.
            owner_gone = row['owner_status'] != 'active' or not process_alive(row['owner_pid'], row['owner_identity'])
            if row['error'] != 'interrupted' or owner_gone:
                db.execute("UPDATE tasks SET status='queued',result=NULL,error=NULL,updated_at=? WHERE id=?", (now(), row['id']))
                from .acceptance import reset
                reset(db,row['id'])
                db.execute("INSERT INTO execution_routes VALUES(?,'normal',?) ON CONFLICT(task_id) DO UPDATE SET queued_at=excluded.queued_at",(row['id'],now()))


class Dispatcher:
    def __init__(self, store, bridge_factory=CodexBridge):
        self.store = store
        self.outbox = Outbox(store)
        self.presence = Presence(store)
        self.bridge_factory = bridge_factory
        from .retries import ReadRetry
        self.reads = ReadRetry(store)

    def tick(self):
        self.presence.import_sessions()
        with self.store.connect() as db:
            waiting = db.execute("SELECT 1 FROM notifications WHERE status IN ('pending','sending','submitted','uncertain') AND next_attempt<=? LIMIT 1", (time.time(),)).fetchone()
        if not waiting:
            return
        with contextlib.ExitStack() as stack:
            bridge = self.reads.call('native:connect','bridge.connect',lambda:stack.enter_context(self.bridge_factory()))
            online = self.presence.online(self.reads.call('native:loaded','bridge.loaded',bridge.loaded))
            from .slack_notifications import route_slack_notifications
            route_slack_notifications(self.store, online)
            with self.store.connect() as db:
                events = [dict(r) for r in db.execute("""SELECT * FROM notifications
                    WHERE status IN ('sending','submitted','uncertain') AND next_attempt<=?
                    AND coalesce(target_thread,'') NOT LIKE 'slack:%'
                    ORDER BY next_attempt,created_at LIMIT 20""", (time.time(),))]
                if online:
                    marks = ','.join('?' for _ in online)
                    events += [dict(r) for r in db.execute("""SELECT * FROM notifications
                        WHERE status='pending' AND next_attempt<=? AND (origin_thread IS NULL OR origin_thread IN (""" + marks + ")) ORDER BY next_attempt,created_at LIMIT 20", (time.time(), *online))]
            used = set()
            for event in events:
                if event['status'] != 'pending':
                    # Receipts can be checked while the user's connection is offline;
                    # checking history never resumes a conversation or calls a model.
                    try:
                        status, turn = self.reads.call(event['id']+':receipt','bridge.receipt',lambda:self._receipt(bridge,event))
                    except Exception:
                        self._defer(event, 'receipt_unavailable')
                        continue
                    with self.store.transaction() as db:
                        db.execute("""UPDATE notifications SET status=?,turn_id=?,next_attempt=?,
                            error=?,updated_at=? WHERE id=? AND status!='cancelled'""",
                            (status, turn, time.time() + (30 if status == 'uncertain' else 2),
                             'delivery_uncertain' if status == 'uncertain' else None, now(), event['id']))
                    if status == 'delivered':
                        from .metrics import sample,seconds
                        best_effort(sample, self.store,event['id'],'delivery',sample_id='delivery:'+event['id'],delivery_seconds=max(0,time.time()-seconds(event['created_at'])))
                    if status in ('submitted', 'uncertain'):
                        used.add(event['target_thread'])
                    continue
                target = event['origin_thread'] if event['origin_thread'] in online else None
                if not event['origin_thread'] and online:
                    target = online[0]
                if not target or target in used:
                    continue
                try:
                    state, metadata = self.reads.call(event['id']+':state','bridge.state',lambda:bridge.state(target))
                except Exception:
                    self._defer(event,'state_unavailable')
                    continue
                if metadata.get('ephemeral'):
                    self._defer(event, 'ephemeral_thread_has_no_durable_receipt')
                    continue
                # Only the project registry determines eligible recipients. Never
                # discover arbitrary user's threads or resume an offline thread.
                if state != 'idle':
                    self._defer(event, None)
                    continue
                event['target_thread'] = target
                self._send(bridge, event)
                used.add(target)

    def _receipt(self,bridge,event):
        value=bridge.receipt(event)
        from .features import enabled
        if enabled(self.store,'read_retries') and value[0]=='uncertain':
            from .retries import ReceiptUnavailable
            raise ReceiptUnavailable()
        return value

    def _defer(self, event, error):
        with self.store.transaction() as db:
            db.execute("UPDATE notifications SET next_attempt=?,error=?,updated_at=? WHERE id=? AND status!='cancelled'",
                       (time.time() + 15, error, now(), event['id']))

    def _send(self, bridge, event):
        # Keep the project write lock through this short network operation so
        # forgetting cannot finish while an old in-memory payload is dispatched.
        with self.store._lock(), contextlib.closing(self.store.connect()) as db:
            row = db.execute("SELECT status FROM notifications WHERE id=?", (event['id'],)).fetchone()
            if not row or row['status'] != 'pending':
                return
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE notifications SET status='sending',target_thread=?,attempts=attempts+1,
                updated_at=? WHERE id=?""", (event['target_thread'], now(), event['id']))
            db.commit()  # Durable intent precedes the external operation.
            try:
                turn = bridge.send(event)
            except Exception:
                db.execute("UPDATE notifications SET status='uncertain',error='send_response_lost',next_attempt=?,updated_at=? WHERE id=?",
                           (time.time() + 15, now(), event['id']))
                return
            db.execute("UPDATE notifications SET status='submitted',turn_id=?,error=NULL,next_attempt=?,updated_at=? WHERE id=?",
                       (turn, time.time() + 2, now(), event['id']))


class Daemon:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.manager = None
        self.scheduler = None
        self.slack = None

    def run(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from .scheduling import ScheduleBook
        lockpath = self.store.private / 'daemon.lock'
        self.store._safe(lockpath)
        fd = os.open(lockpath, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('本项目后台服务已经运行') from exc
            initialize_runtime(self.store)
            initialize_background(self.store)
            reconcile(self.store)
            self.book = ScheduleBook(self.store)
            self.dispatcher = Dispatcher(self.store)
            self.manager = Manager(self.store, background=True)
            from .slack_supervisor import SlackSupervisor
            self.slack = SlackSupervisor(self.store)
            # _claim still checks every old live PID before relaunch.
            recover_interrupted(self.store)
            self._heartbeat()
            self.scheduler = BackgroundScheduler(timezone='Asia/Shanghai', job_defaults={'max_instances': 1, 'coalesce': True})
            from .monitoring import Monitor
            self.monitor = Monitor(self.store)
            from .policy import settings
            interval = settings(self.store)['monitor_scan_seconds']
            self.scheduler.add_job(lambda: best_effort(self.monitor.scan), 'interval', seconds=interval, id='laowu-monitor', misfire_grace_time=30)
            self.scheduler.add_job(self.tick, 'interval', seconds=2, id='laowu-background', misfire_grace_time=30)
            self.scheduler.start()
            while not self.stop.wait(1):
                self.slack.tick()
                if self.manager.closed.is_set():
                    raise RuntimeError('后台运行器意外停止')
        finally:
            if self.slack:
                self.slack.close()
            if self.scheduler:
                self.scheduler.shutdown(wait=True)
            if self.manager:
                self.manager.close()
                self._heartbeat(status='stopped')
            os.close(fd)

    def _heartbeat(self, status='running', error=None):
        with self.store.transaction() as db:
            db.execute("""INSERT INTO daemon_state VALUES(1,?,?,?,?,?) ON CONFLICT(singleton)
                DO UPDATE SET pid=excluded.pid,identity=excluded.identity,heartbeat=excluded.heartbeat,
                status=excluded.status,error=excluded.error""",
                (os.getpid(), process_identity(os.getpid()), time.time(), status, error))

    def tick(self):
        try:
            with self.store.connect() as db:
                paused = db.execute("SELECT value FROM background_meta WHERE key='paused'").fetchone()
            if paused and paused[0] == '1':
                self._heartbeat(error='paused')
                return
            recover_interrupted(self.store)
            self.book.due()
            scheduling_error = None
            for occurrence in self.book.pending():
                try:
                    task = self.manager.submit(occurrence['friend_id'], occurrence['request'], occurrence['workspace'],
                                               'schedule:' + occurrence['id'], detached=True,
                                               origin_thread=None, context=occurrence['context'])
                    self.book.attach_task(occurrence['id'], task['id'])
                except (ValueError, PermissionError, RuntimeError) as exc:
                    with self.store.connect() as db:
                        exists = db.execute('SELECT 1 FROM schedule_occurrences WHERE id=?', (occurrence['id'],)).fetchone()
                    if not exists:
                        continue  # Forgotten while this tick was preparing dispatch.
                    scheduling_error = 'schedule:' + type(exc).__name__
                    self.dispatcher.outbox.publish(
                        f"定时计划 {occurrence['schedule_id']} 暂未启动。请检查朋友状态和工作目录；该期仍保留待执行。",
                        event_key='schedule-error:' + occurrence['id'], topic='schedule.blocked',
                        context=occurrence['context'])
            self.dispatcher.outbox.collect_tasks()
            try:
                self.dispatcher.tick()
                delivery_error = None
            except Exception as exc:
                from .slack_notifications import route_slack_notifications
                route_slack_notifications(self.store)
                delivery_error = 'delivery:' + type(exc).__name__
            self._heartbeat(error=delivery_error or scheduling_error)
        except Exception as exc:
            self._heartbeat(error='tick:' + type(exc).__name__)


def run(store):
    service = Daemon(store)
    def stop(*_):
        service.stop.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    service.run()


def unit_name(root):
    return 'laowu-' + hashlib.sha256(str(root).encode()).hexdigest()[:12] + '.service'


def _quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n') + '"'


def service_action(store, action):
    root = store.root
    user = os.geteuid() != 0
    command = ['systemctl'] + (['--user'] if user else [])
    name = unit_name(root)
    if action == 'install':
        if any(char in str(root) for char in '\r\n'):
            raise ValueError('systemd 安装目录不能包含换行符')
        interpreter = root / '.venv' / 'bin' / 'python'
        if not interpreter.exists():
            raise RuntimeError('请先安装项目 .venv 及 pyproject.toml 中的依赖')
        subprocess.run([str(interpreter), '-c', 'import apscheduler, websockets'], check=True, capture_output=True)
        dest = Path.home() / '.config/systemd/user' if user else Path('/etc/systemd/system')
        path = dest / name
        if path.exists() and '# Managed by laowu' not in path.read_text():
            raise RuntimeError('同名 systemd unit 不属于老五，未覆盖')
        content = '\n'.join([
            '# Managed by laowu', '[Unit]', 'Description=Laowu background tasks and notifications',
            'After=network.target', '', '[Service]', 'Type=simple',
            'WorkingDirectory=' + str(root).replace('%', '%%'),
            'ExecStart=' + _quote(interpreter) + ' ' + _quote(root / 'scripts/laowu.py') + ' --root ' + _quote(root) + ' daemon run',
            'Environment=' + _quote('HOME=' + str(Path.home())),
            'Environment=' + _quote('CODEX_HOME=' + str(socket_path().parent.parent)),
            'Environment=' + _quote('PATH=' + os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')),
            'UMask=0077', 'Restart=on-failure', 'RestartSec=5', 'KillMode=control-group', 'TimeoutStopSec=30',
            '', '[Install]', 'WantedBy=' + ('default.target' if user else 'multi-user.target'), ''])
        dest.mkdir(parents=True, exist_ok=True)
        atomic_write(store.private / 'service' / name, content)
        path.write_text(content, encoding='utf-8')
        path.chmod(0o644)
        subprocess.run(command + ['daemon-reload'], check=True, capture_output=True)
        subprocess.run(command + ['enable', '--now', name], check=True, capture_output=True)
        return {'installed': True, 'unit': str(path), 'name': name}
    if action not in ('start', 'stop', 'restart'):
        raise ValueError('未知后台服务操作')
    subprocess.run(command + [action, name], check=True, capture_output=True)
    return {'action': action, 'unit': name}
