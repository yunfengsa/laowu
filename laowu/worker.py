"""A short-lived guardian. A closed owner pipe stops the entire CLI process group."""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

from .adapters import parse_event
from .telemetry import best_effort
from .common import now, process_identity
from .diagnostics import ErrorStream, error_code


def handoff(text: str) -> dict:
    decoder = json.JSONDecoder()

    def validated(data):
        if not isinstance(data, dict) or not isinstance(data.get("result"), str):
            return None
        if not all(isinstance(data.get(k), list) and all(isinstance(s, str) for s in data[k])
                   for k in ("artifacts", "validation", "unfinished")):
            return None
        if not isinstance(data.get("memory_candidates"), list) or not all(isinstance(m, dict) for m in data["memory_candidates"]):
            return None
        json.dumps(data, allow_nan=False)  # Reject non-JSON numeric constants before persistence.
        return {key: data[key] for key in ("result", "artifacts", "validation", "unfinished", "memory_candidates")}

    # Parse the normal whole response first. raw_decode accepts an index into
    # the original string, so neither this path nor recovery copies suffixes.
    start = 0
    while start < len(text) and text[start].isspace():
        start += 1
    try:
        data, _ = decoder.raw_decode(text, start)
    except (ValueError, RecursionError):
        pass
    else:
        result = validated(data)
        if result is not None:
            return result

    # Recover fenced JSON or a handoff preceded by prose, but cap retries:
    # adversarial braces must not cause an unbounded number of full parses.
    cursor = start + 1 if start < len(text) and text[start] == "{" else 0
    for _ in range(64):
        cursor = text.find("{", cursor)
        if cursor < 0:
            break
        try:
            data, _ = decoder.raw_decode(text, cursor)
        except (ValueError, RecursionError):
            pass
        else:
            result = validated(data)
            if result is not None:
                return result
        cursor += 1
    raise ValueError("invalid_handoff")


def stop_group(process):
    if process is None:
        return True
    from .common import stop_process_group
    witnesses = []
    try:
        manifest = json.loads(process._laowu_manifest.read_text())
        if (manifest['anchor_pid'] == process.pid
                and manifest['anchor_identity'] == process._laowu_identity):
            witnesses = manifest['members']
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    clean = stop_process_group(process.pid, process._laowu_identity, witnesses)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    return clean


def run(store, run_id: str, lease_fd: int) -> int:
    from .runtime import initialize_runtime
    initialize_runtime(store)
    from .monitoring import Monitor
    monitor = Monitor(store)
    best_effort(monitor.scan)
    started_clock = time.time()
    provider_usage = {}
    process = None
    gate_read = gate_write = None
    selector = selectors.DefaultSelector()
    interrupted = False
    failure = None
    messages = []
    output_size = 0
    cli_session_id = None
    stderr = ErrorStream()
    diagnostics = {"version": 1, "error_events": 0, "turn_completed": False}
    pending_error = None
    terminal_error = None
    folder = store.root / ".laowu" / "runtime" / run_id

    def interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        with store.transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["status"] not in {"starting", "running"}:
                return 1
            run_info = dict(row)
            db.execute("UPDATE runs SET supervisor_pid=?,supervisor_identity=?,updated_at=? WHERE id=?",
                       (os.getpid(), process_identity(os.getpid()), now(), run_id))
        config = json.loads((folder / "launch.json").read_text(encoding="utf-8"))
        from .access import task_access, restrict_friend, worker_environment
        grant = task_access(store, run_info['task_id'])
        if grant:
            restrict_friend(json.loads(run_info['spec_json']), grant, root=store.root, workspace=run_info['workspace'])
        environment = os.environ.copy()
        environment.pop("CODEX_THREAD_ID", None)
        environment.pop("LAOWU_SESSION_ID", None)
        environment.update(config["env"])
        environment = worker_environment(environment)
        environment["LAOWU_ROLE"] = "worker"
        environment["LAOWU_ROOT"] = str(store.root)
        gate_read, gate_write = os.pipe()
        manifest = folder / 'group.json'
        # No real CLI executes before its group identity is durably registered.
        argv = [sys.executable, str(Path(__file__).with_name('common.py')),
                str(gate_read), str(manifest), *config['argv']]
        process = subprocess.Popen(argv, cwd=config["cwd"], env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, close_fds=True, pass_fds=(gate_read,))
        os.close(gate_read)
        gate_read = None
        process._laowu_identity = process_identity(process.pid)
        process._laowu_manifest = manifest
        with store.transaction() as db:
            permitted = db.execute("SELECT 1 FROM runs r JOIN sessions s ON s.id=r.session_id WHERE r.id=? AND r.status IN ('starting','running') AND s.status='active'", (run_id,)).fetchone()
            db.execute("UPDATE runs SET status=CASE WHEN status='starting' THEN 'running' ELSE status END,child_pid=?,child_identity=?,updated_at=? WHERE id=?",
                       (process.pid, process._laowu_identity, now(), run_id))
            db.execute('INSERT OR IGNORE INTO run_cleanup VALUES(?)', (run_id,))
        if permitted and process._laowu_identity:
            os.write(gate_write, b'1')
        else:
            interrupted = True
        os.set_blocking(lease_fd, False)
        selector.register(lease_fd, selectors.EVENT_READ, "lease")
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        # Nonblocking prompt delivery keeps cancellation responsive even before the CLI reads stdin.
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        input_data = config["stdin"].encode("utf-8")
        input_offset = 0
        buffer = b""
        deadline = time.monotonic() + 3600
        last_check = 0.0
        event_count = 0

        def consume(line):
            nonlocal output_size, failure, cli_session_id, event_count, pending_error, terminal_error
            event = parse_event(run_info["client"], line.decode("utf-8", errors="replace"), telemetry=True)
            if not event:
                return
            event_count += 1
            best_effort(monitor.observe, run_id, **{k:v for k,v in event.items() if k in ('client_event','phase','progress_key','tool_id','tool_state')})
            provider_usage.update(event.get('usage',{}))
            if event.get("session_id"):
                cli_session_id = event["session_id"]
            if event.get("error"):
                diagnostics["error_events"] += 1
                diagnostic = {"source": "json_event", **event["diagnostic"]}
                # A generic final error must not erase an earlier useful classification.
                if pending_error is None or diagnostic["category"] != "unknown":
                    pending_error = diagnostic
                if event["terminal"]:
                    terminal_error = pending_error
            if event.get("event") == "result":
                diagnostics["turn_completed"] = True
                pending_error = None  # Recoverable error/reconnect events can precede success.
            if event.get("message"):
                message = str(event["message"])
                output_size += len(message)
                if output_size > 262144:
                    failure = "output_limit"
                    return
                messages.append(message)

        while not interrupted and time.monotonic() < deadline:
            for key, _ in selector.select(timeout=0.1):
                if key.data == "lease":
                    if not os.read(lease_fd, 1):
                        interrupted = True
                    continue
                if key.data == "stdin":
                    try:
                        if input_offset < len(input_data):
                            input_offset += os.write(key.fd, input_data[input_offset:input_offset + 16384])
                        if input_offset == len(input_data):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    except BrokenPipeError:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        consume(line)
                    if len(buffer) > 1048576:
                        failure = "event_limit"
                elif key.data == "stderr":
                    stderr.feed(chunk)
            if failure in {"output_limit", "event_limit"}:
                break
            if time.monotonic() - last_check > 0.2:
                try:
                    task_access(store, run_info['task_id'])
                except PermissionError:
                    interrupted = True
                with store.transaction() as db:
                    current = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
                    session = db.execute("SELECT status FROM sessions WHERE id=?", (run_info["session_id"],)).fetchone()
                    if not current or current["status"] not in {"starting", "running"} or not session or session["status"] != "active":
                        interrupted = True
                    db.execute("UPDATE runs SET event_count=?,cli_session_id=?,updated_at=? WHERE id=?",
                               (event_count, cli_session_id, now(), run_id))
                best_effort(monitor.observe, run_id,heartbeat=True)
                last_check = time.monotonic()
            if process.poll() is not None and not any(k.data in {"stdout", "stderr"} for k in selector.get_map().values()):
                break
            if process.poll() not in (None, 0):
                # A killed anchor can leave inherited pipes open in CLI/tools.
                # Do not wait for EOF before recovering its recorded group.
                break
        if buffer:
            consume(buffer)
        if time.monotonic() >= deadline:
            failure = "timeout"
        if interrupted:
            failure = "interrupted"
        elif process.poll() is None:
            failure = failure or "process_incomplete"
        elif process.returncode != 0:
            failure = failure or f"cli_exit_{process.returncode}"
        diagnostics.update(exit_code=process.poll(), stderr_bytes=stderr.byte_count)
        observed = terminal_error or pending_error
        if observed is None or observed["category"] == "unknown":
            if stderr.diagnostic:
                observed = {"source": "stderr", **stderr.diagnostic}
        if observed:
            diagnostics["last_error"] = observed
        if not interrupted and (terminal_error or pending_error or process.returncode not in (None, 0)):
            if failure is None or failure.startswith("cli_exit_"):
                failure = error_code(observed) if observed else failure
        result = None
        if not failure:
            try:
                # Codex versions without a phase field can also emit commentary.
                # Only the last completed assistant message is the final handoff.
                result = handoff(messages[-1] if messages else "")
                from .store import _SECRET
                if _SECRET.search(json.dumps(result, ensure_ascii=False)):
                    failure = "sensitive_output"
                    result = None
            except ValueError:
                failure = "invalid_handoff"
        status = "interrupted" if interrupted else "failed" if failure else "blocked" if result and result["unfinished"] else "completed"
        if status == "blocked":
            failure = "unfinished_work"
        from .runtime import finish_run
        updated = finish_run(store,run_id,status,result,failure,diagnostics,event_count,cli_session_id)
        if result and updated:
            best_effort(monitor.observe, run_id,progress_key='valid_handoff')
        return 0 if status == "completed" else 1
    except Exception as exc:
        from .runtime import finish_run
        finish_run(store,run_id,'failed',None,type(exc).__name__)
        return 1
    finally:
        # Cleanup is independent of optional observations and of each other.
        clean = False
        try:
            for fd in (gate_read, gate_write):
                if fd is not None:
                    best_effort(os.close, fd)
            clean = best_effort(stop_group, process)
        finally:
            best_effort(selector.close)
            if process:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream:
                        best_effort(stream.close)
            best_effort(os.close, lease_fd)
            import shutil
            if clean and folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
        def record_metrics():
            from .metrics import sample, seconds
            with store.connect() as db:
                task_time = db.execute('SELECT coalesce(e.queued_at,t.created_at) FROM tasks t LEFT JOIN execution_routes e ON e.task_id=t.id WHERE t.id=(SELECT task_id FROM runs WHERE id=?)',(run_id,)).fetchone()
            sample(store,run_id,'worker',sample_id='worker:'+run_id,
                   execution_seconds=time.time()-started_clock,
                   queue_seconds=max(0,started_clock-seconds(task_time[0])) if task_time else None,**provider_usage)
        best_effort(record_metrics)
        best_effort(monitor.observe, run_id,finished=True)
