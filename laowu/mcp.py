"""Minimal MCP stdio transport using JSON-RPC 2.0, with no network listener.

Only initialize, ping and tools are advertised. No custom sampling or raw logs.
The reader stays responsive to EOF while a tool waits for a worker.
"""
from __future__ import annotations

import json
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from . import __version__
from .service import Service, visible_tools, validate_arguments


def serve(root, client="codex", worker_run=None, worker_token=None, *, slack_token_file=None):
    service = Service(root, client, worker_run, worker_token, slack_token_file=slack_token_file)
    output_lock = threading.Lock()
    closing = threading.Event()
    executor = ThreadPoolExecutor(max_workers=4)

    def send(payload):
        if closing.is_set():
            return
        try:
            with output_lock:
                sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except (BrokenPipeError, OSError):
            closing.set()
            service.close()

    def handle(message):
        ident = message.get("id")
        if ident is None:
            return
        response = {"jsonrpc": "2.0", "id": ident}
        try:
            method = message.get("method")
            params = message.get("params", {})
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2025-03-26"
                response["result"] = {"protocolVersion": version, "capabilities": {"tools": {}},
                    "serverInfo": {"name": "laowu", "version": __version__},
                    "instructions": ("你是已认证的朋友工作会话，无需且不能调用主会话 bootstrap。"
                                     "只检索获授权记忆、保存本任务检查点并向老五交接；不能继续委派。"
                                     if worker_run else
                                     "先调用 bootstrap 恢复状态；只在成功保存后声称已记住。朋友运行不拥有继续委派接口。")}
            elif method == "ping":
                response["result"] = {}
            elif method == "tools/list":
                response["result"] = {"tools": [{"name": name, "description": description, "inputSchema": definition,
                    "annotations": {"readOnlyHint": name in {"memory_search", "task_status", "task_wait", "context_read", "slack_channels", "slack_history", "slack_thread"},
                                    "destructiveHint": name in {"memory_forget", "task_cancel"},
                                    "openWorldHint": name in {"task_start", "task_resume"}}}
                    for name, description, definition in visible_tools(worker_run, service.access)]}
            elif method == "tools/call":
                try:
                    name, arguments = params.get("name"), params.get("arguments", {})
                    validate_arguments(name, arguments)
                    result = service.call(name, arguments)
                    from .context import Context
                    result = Context(service).present(name, arguments, result)
                    response["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, separators=(",", ":"))}], "isError": False}
                except Exception as exc:
                    safe = str(exc) if isinstance(exc, (ValueError, PermissionError, RuntimeError)) else type(exc).__name__
                    if len(safe.encode()) > 2048:
                        safe = '错误详情超过展示预算；非持久错误未复制。错误类型：' + type(exc).__name__
                    response["result"] = {"content": [{"type": "text", "text": json.dumps({"version":1,"error": safe}, ensure_ascii=False)}], "isError": True}
                    from .metrics import sample
                    from .telemetry import best_effort
                    best_effort(sample,service.store,service.worker_run or service.manager.id,'mcp',
                           mcp_bytes=len(json.dumps(response['result'],ensure_ascii=False,separators=(',',':')).encode()),previews=0,pages=0)
            else:
                response["error"] = {"code": -32601, "message": "Method not found"}
        except Exception:
            response["error"] = {"code": -32602, "message": "Invalid params"}
        send(response)

    def stop(signum, frame):
        closing.set()
        service.close()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not closing.is_set():
            line = sys.stdin.buffer.readline(1048577)
            if not line:
                break
            try:
                if len(line) > 1048576:
                    raise ValueError("oversized")
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError("invalid")
            except ValueError:
                send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC message"}})
                continue
            if message.get("method") in {"initialize", "tools/list", "ping"}:
                handle(message)
            elif "id" in message:
                executor.submit(handle, message)
    finally:
        closing.set()
        service.close()
        executor.shutdown(wait=True, cancel_futures=True)
