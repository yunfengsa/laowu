"""Shared utilities and private process-group control; never copy credentials."""
from __future__ import annotations

import json
import os
import re
import tempfile
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".laowu-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def process_identity(pid: int, *, include_zombie=False) -> str | None:
    """Linux PID start ticks prevent PID reuse from granting run ownership."""
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        if fields[0] == "Z" and not include_zombie:
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{fields[19]}"
    except (OSError, ValueError, IndexError):
        return None


def process_alive(pid: int | None, identity: str | None) -> bool:
    return bool(pid and identity and process_identity(pid) == identity)


def group_members(pgid: int) -> dict[int, str]:
    """Return live members with start identities, never a bare list of PIDs."""
    members = {}
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            # Group and start ticks must come from ONE stat snapshot: otherwise
            # PID reuse between reads could attach an unrelated identity here.
            if fields[0] != 'Z' and int(fields[2]) == pgid:
                members[int(entry.name)] = f'{boot}:{fields[19]}'
        except (OSError, ValueError, IndexError):
            continue
    return members


def signal_process(pid: int, identity: str, signum: int) -> bool:
    """Pin a Linux process before checking its start identity and signalling it."""
    try:
        fd = os.pidfd_open(pid)
    except (OSError, AttributeError):
        return False
    try:
        if not process_alive(pid, identity):
            return False
        signal.pidfd_send_signal(fd, signum)
        return True
    except (OSError, AttributeError):
        return False
    finally:
        os.close(fd)


def stop_process_group(pgid: int, identity: str, witnesses=(), *, exclude_self=False) -> bool:
    """Stop only an identity-verified group; uncertain ownership stays pending.

    A living group anchor or recorded CLI is required before taking each member
    snapshot. Individual pidfds prevent PID reuse between inspection and signal.
    """
    witnesses = [(pgid, identity), *witnesses]
    trusted = {}
    def snapshot():
        current = process_identity(pgid)
        try:
            all_members = group_members(pgid)
        except OSError:
            return None  # An unreadable /proc is unknown, not an empty group.
        members = dict(all_members)
        if exclude_self:
            members.pop(os.getpid(), None)
        if not members:
            return {}
        if current and current != identity:
            # A reused numeric leader is not an ownership witness. A recorded
            # surviving CLI/tool may still verify the original group.
            trusted.pop(pgid, None)
        def belongs(pid, ident):
            try:
                fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                return (all_members.get(pid) == ident and fields[0] != 'Z'
                        and int(fields[2]) == pgid and f'{boot}:{fields[19]}' == ident)
            except (OSError, ValueError, IndexError):
                return False
        if not any(belongs(pid, ident) for pid, ident in [*witnesses, *trusted.items()]):
            return None
        trusted.update(all_members)
        return members

    members = snapshot()
    if members is None:
        return False
    for signum, grace in ((signal.SIGTERM, 0.2), (signal.SIGKILL, 0.5)):
        # The anchor goes last, retaining group ownership while stopping tools.
        signalled = False
        for pid, ident in sorted(members.items(), key=lambda item: item[0] == pgid):
            signalled = signal_process(pid, ident, signum) or signalled
        if members and not signalled:
            return False
        deadline = time.monotonic() + grace
        while members and time.monotonic() < deadline:
            time.sleep(0.01)
            members = snapshot()
            if members is None:
                return False
        if not members:
            return True
    return False


def run_process_group(gate_fd: int, manifest: str, argv: list[str]) -> int:
    """Private gated group anchor, invoked by worker.py with this absolute file.

    The outer gate is a lifetime lease. A second gate prevents a real CLI exec
    before its identity is durably available to a surviving guardian/manager.
    """
    import select
    if os.read(gate_fd, 1) != b'1':
        return 1
    identity = process_identity(os.getpid())
    # The final fallback can kill ONLY this private group, never the caller's.
    if not identity or os.getpgrp() != os.getpid():
        os.close(gate_fd)
        return 1
    stopped = False
    def stop(*unused):
        nonlocal stopped
        stopped = True
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(writer)
        os.close(gate_fd)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, signal.SIG_DFL)
        ready = os.read(reader, 1)
        os.close(reader)
        if ready == b'1':
            try:
                os.execvpe(argv[0], argv, os.environ)
            except OSError:
                pass
        os._exit(127)
    os.close(reader)
    status = None
    recorded = None
    try:
        child_identity = process_identity(child)
        if not identity or not child_identity:
            raise RuntimeError('group_identity_unavailable')
        recorded = {'anchor_pid': os.getpid(), 'anchor_identity': identity,
                    'members': [[child, child_identity]]}
        atomic_write(Path(manifest), json.dumps(recorded))
        os.write(writer, b'1')
        os.close(writer)
        writer = None
        # The CLI inherits the guardian's pipes. The anchor must not hold them open.
        for fd in (0, 1, 2):
            try:
                os.close(fd)
            except OSError:
                pass
        while not stopped:
            pid, status = os.waitpid(child, os.WNOHANG)
            if pid:
                break
            status = None
            ready, _, _ = select.select([gate_fd], [], [], 0.05)
            if ready and not os.read(gate_fd, 1):
                stopped = True
    finally:
        if writer is not None:
            os.close(writer)
        clean = stop_process_group(os.getpid(), identity, exclude_self=True) if identity else False
        if not clean:
            # Linux without pidfds still permits a process to revoke its OWN
            # group without any PID-reuse race. Save only an actually observed
            # CLI exit; the guardian must separately confirm group cleanup.
            code = os.waitstatus_to_exitcode(status) if status is not None and not stopped else None
            try:
                os.killpg(os.getpid(), signal.SIGTERM)
                time.sleep(0.2)
                if recorded is not None and code is not None:
                    atomic_write(Path(manifest), json.dumps({**recorded, 'self_cleanup': True,
                        'cli_exit_code': code}))
            finally:
                if os.getpgrp() == os.getpid():
                    os.killpg(os.getpid(), signal.SIGKILL)
        os.close(gate_fd)
        if status is None and clean:
            _, status = os.waitpid(child, 0)
    code = os.waitstatus_to_exitcode(status) if status is not None and not stopped else 1
    if code < 0:
        signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
    return code


def valid_id(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("ID 必须使用小写字母、数字、下划线或短横线，最多 64 字符")
    return value


def project_root(value: str | Path | None = None) -> Path:
    if value:
        root = Path(value).expanduser().resolve()
    elif os.environ.get("LAOWU_ROOT"):
        root = Path(os.environ["LAOWU_ROOT"]).resolve()
    else:
        root = Path(__file__).resolve().parent.parent
    return root


if __name__ == '__main__':
    import sys
    raise SystemExit(run_process_group(int(sys.argv[1]), sys.argv[2], sys.argv[3:]))
