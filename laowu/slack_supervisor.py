"""Nonblocking supervision of the optional Slack connector."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


class SlackSupervisor:
    def __init__(self, store):
        self.store, self.child, self.lease = store, None, None
        self.retry_at = 0

    def tick(self):
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='slack_meta'").fetchone():
                return
        from .slack_state import SlackState
        state = SlackState(self.store)
        if not state.config().get('enabled'):
            self.close()
            return
        if self.child and self.child.poll() is not None:
            self.close()
            self.retry_at = time.monotonic() + 10
        if self.child or time.monotonic() < self.retry_at:
            return
        reader, writer = os.pipe()
        try:
            env = {k: v for k, v in os.environ.items() if not k.startswith('SLACK_')}
            env['LAOWU_SLACK_LEASE_FD'] = str(reader)
            command = [str(Path(sys.executable).resolve()), str(self.store.root / 'scripts/laowu.py'),
                       '--root', str(self.store.root), 'slack', 'run']
            self.child = subprocess.Popen(command, env=env, pass_fds=(reader,), start_new_session=True,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.lease = writer
            writer = None
        except Exception:
            self.retry_at = time.monotonic() + 10
            state.runtime(status='failed', error='connector_launch_failed')
        finally:
            os.close(reader)
            if writer is not None:
                os.close(writer)

    def close(self):
        if self.lease is not None:
            os.close(self.lease)
            self.lease = None
        if self.child:
            if self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=3)
            self.child = None


def run_gateway(store):
    import fcntl
    from .slack_state import SlackState
    from .slack_gateway import run
    state = SlackState(store)
    lock = state.directory / 'gateway.lock'
    store._safe(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Slack 接入服务已经运行') from exc
        lease = os.environ.pop('LAOWU_SLACK_LEASE_FD', None)
        if lease:
            def watch():
                with contextlib.suppress(OSError):
                    while os.read(int(lease), 1):
                        pass
                os.kill(os.getpid(), signal.SIGTERM)
            threading.Thread(target=watch, daemon=True, name='slack-parent-lease').start()
        try:
            run(store)
        except Exception as exc:
            import re
            code = getattr(exc, 'code', type(exc).__name__)
            state.runtime(status='failed', error=code if re.fullmatch(r'[A-Za-z0-9_]{1,80}', str(code)) else type(exc).__name__)
            raise RuntimeError('Slack 接入启动失败；请运行 slack doctor 检查本地配置') from None
    finally:
        os.close(fd)
