from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .common import json_text, project_root
from .registry import Registry
from .runtime import Manager, TERMINAL, cancel_task, initialize_runtime, reconcile, task_diagnostics, task_record
from .store import Store


def parser():
    p = argparse.ArgumentParser(description="老五：本地记忆与朋友工作空间")
    p.add_argument("--root", help="项目根目录，默认当前安装目录")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="初始化记忆、朋友和 Codex 项目配置")
    commands.add_parser("doctor", help="诊断依赖与状态，不调用模型")
    chat = commands.add_parser('chat', help='通过连接代理启动 Codex，精确跟踪终端连接')
    chat.add_argument('args', nargs=argparse.REMAINDER, help='-- 后传入 Codex 参数')
    daemon = commands.add_parser('daemon', help='管理独立后台服务').add_subparsers(dest='action', required=True)
    for action in ('install', 'start', 'stop', 'restart', 'status', 'run', 'pause', 'resume'):
        daemon.add_parser(action)
    schedules = commands.add_parser('schedules', help='管理持久定时计划').add_subparsers(dest='action', required=True)
    schedules.add_parser('list')
    create = schedules.add_parser('save')
    create.add_argument('file', help='JSON 配置文件，- 从 stdin 读取')
    for action in ('pause', 'resume', 'delete'):
        sub = schedules.add_parser(action)
        sub.add_argument('schedule_id')
    events = commands.add_parser('notifications', help='发布事件或检查通知投递').add_subparsers(dest='action', required=True)
    listing = events.add_parser('list')
    listing.add_argument('--status')
    for action in ('retry', 'cancel'):
        sub = events.add_parser(action)
        sub.add_argument('notification_id')
    emit = events.add_parser('emit')
    emit.add_argument('message', help='- 从 stdin 读取；默认发送最新连接会话')
    emit.add_argument('--key', required=True, dest='event_key')
    emit.add_argument('--thread', dest='origin_thread')
    emit.add_argument('--topic', default='event')
    emit.add_argument('--context', choices=['normal', 'root'], default='normal')
    emit.add_argument('--delay', type=float, default=0)
    commands.add_parser("migrate", help="先自动备份，再幂等升级存储；不启用服务或功能")
    reads = commands.add_parser('reads', help='查看和显式恢复挂起的只读核对').add_subparsers(dest='action', required=True)
    reads.add_parser('list')
    reads.add_parser('retry').add_argument('read_id')
    commands.add_parser("backup", help="备份本地记忆、朋友和配置")
    restore = commands.add_parser("restore", help="无活跃会话时恢复备份，并重建本地路径")
    restore.add_argument("path")
    mcp = commands.add_parser("mcp", help="stdio MCP 入口")
    mcp.add_argument("--client", default="codex", choices=["codex"])
    mcp.add_argument("--worker-run")
    mcp.add_argument("--worker-token")
    mcp.add_argument("--slack-token-file", help=argparse.SUPPRESS)
    slack = commands.add_parser('slack', help='管理老五 Slack Agent 接入').add_subparsers(dest='action', required=True)
    setup_slack = slack.add_parser('setup', help='配置工作区、本人及本地凭据')
    setup_slack.add_argument('--team', required=True, dest='team_id')
    setup_slack.add_argument('--owner', required=True, dest='owner_user_id')
    setup_slack.add_argument('--credentials-file', help='0600 JSON 文件，含 app_token、bot_token；不在命令行传令牌')
    for action in ('status', 'doctor', 'enable', 'disable', 'run'):
        slack.add_parser(action)
    channels = slack.add_parser('channels', help='列出或配置频道响应与私密资格')
    channels.add_argument('channel_id', nargs='?')
    channels.add_argument('--mode', choices=['mention', 'direct'])
    channels.add_argument('--personal', choices=['yes', 'no'], help='明确批准独占私密频道；仍须核实成员')
    reset = slack.add_parser('reset', help='重新开始频道对话，保留全局记忆')
    reset.add_argument('channel_id')
    hook = commands.add_parser("hook", help="客户端 hooks 入口")
    hook.add_argument("--client", default="codex", choices=["codex"])
    hook.add_argument("--event", required=True)
    worker = commands.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("--run-id", required=True)
    worker.add_argument("--lease-fd", required=True, type=int)
    memory = commands.add_parser("memory", help="记忆查询、保存、纠正和遗忘").add_subparsers(dest="action", required=True)
    search = memory.add_parser("search")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--scope")
    add = memory.add_parser("save")
    add.add_argument("content", help="传 - 从 stdin 读取")
    add.add_argument("--scope", default="personal")
    add.add_argument("--kind", default="fact")
    add.add_argument("--id", dest="memory_id")
    forget = memory.add_parser("forget")
    forget.add_argument("memory_id")
    friends = commands.add_parser("friends", help="管理朋友").add_subparsers(dest="action", required=True)
    friends.add_parser("list")
    friends.add_parser("sync")
    ensure = friends.add_parser("ensure")
    ensure.add_argument("file", help="包含完整 instructions 的 JSON 配置")
    tasks = commands.add_parser("tasks", help="管理任务").add_subparsers(dest="action", required=True)
    tasks.add_parser("list")
    tasks.add_parser("metrics", help="脱敏聚合指标；缺失usage保留NULL")
    review = tasks.add_parser("review")
    review.add_argument("task_id")
    review.add_argument("run_id")
    review.add_argument("decision",choices=['accepted','needs_work'])
    review.add_argument("summary")
    review.add_argument("--evidence",action='append',default=[])
    for action in ("show", "diagnose", "cancel", "resume"):
        sub = tasks.add_parser(action)
        sub.add_argument("task_id")
    start = tasks.add_parser("run")
    start.add_argument("friend_id")
    start.add_argument("request", help="传 - 从 stdin 读取")
    start.add_argument("--workspace")
    start.add_argument("--key", dest="idempotency_key")
    start.add_argument('--background', action='store_true', help='关闭当前命令后仍继续，由后台服务执行')
    start.add_argument('--thread', help='结果绑定此 Codex 会话；省略则发最新连接会话')
    start.add_argument('--context', choices=['normal', 'root'], default='normal')
    return p


def doctor(root):
    from .adapters import detect_clients
    store = Store(root)
    initialize_runtime(store)
    reconcile(store)
    registry = Registry(store)
    result = {"root": str(root), "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
              "platform": sys.platform, "platform_supported": sys.platform.startswith("linux"),
              "memory_backend": "sqlite", "clients": detect_clients(), "friends": registry.list(),
              "configuration": {"agents": (root / "AGENTS.md").is_file(),
                                "codex": (root / ".codex" / "config.toml").is_file()},
              "trust": "首次在 Codex 中信任项目，并在 /hooks 审阅钩子；本命令不代替客户端信任。",
              "live_model_test": "not_performed_by_doctor"}
    with store.connect() as db:
        result["database_integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
        result["fts5"] = bool(db.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0])
        result["tasks"] = {row[0]: row[1] for row in db.execute("SELECT status,count(*) FROM tasks GROUP BY status")}
    from .background import daemon_status
    from .codex_bridge import socket_path
    result['background'] = daemon_status(store)
    try:
        result['codex_socket_exists'] = socket_path().exists()
    except OSError as exc:
        result['codex_socket_exists'] = None
        result['codex_socket_error'] = 'permission_denied' if isinstance(exc, PermissionError) else 'socket_probe_failed'
    result['native_presence'] = 'Codex 原生会话可在最后客户端断开后保留30分钟；严格连接检测使用 laowu chat 入口。'
    return result


def execute(args, root):
    if args.command == "mcp":
        from .mcp import serve
        serve(root, args.client, args.worker_run, args.worker_token,
              slack_token_file=getattr(args, 'slack_token_file', None))
        return None
    if args.command == "hook":
        from .hooks import handle
        raw = sys.stdin.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("hook 输入过长")
        return handle(root, args.client, args.event, json.loads(raw or "{}"))
    store = Store(root)
    if args.command == 'reads':
        if os.environ.get('LAOWU_ROLE') == 'worker' or os.environ.get('LAOWU_MANAGED_WORKER') == '1':
            raise PermissionError('只读核对恢复须由本地主会话维护入口执行')
        from .retries import ReadRetry
        reads = ReadRetry(store)
        return reads.list() if args.action == 'list' else reads.retry(args.read_id)
    if args.command == "migrate":
        with store.connect() as db:
            return {"schema_version":int(db.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()[0]),"integrity":db.execute("PRAGMA integrity_check").fetchone()[0],"features_enabled_by_migration":False}
    if args.command == 'slack':
        from .slack_admin import execute_slack
        return execute_slack(store, args)
    if args.command == 'chat':
        from .background import initialize_background
        from .session_proxy import run_chat
        initialize_runtime(store)
        initialize_background(store)
        extra = args.args[1:] if args.args[:1] == ['--'] else args.args
        raise SystemExit(run_chat(store, extra))
    if args.command == "worker":
        from .worker import run
        raise SystemExit(run(store, args.run_id, args.lease_fd))
    if args.command in ('daemon', 'schedules', 'notifications'):
        from .background import Outbox, daemon_status, initialize_background
        initialize_runtime(store)
        initialize_background(store)
        if args.command == 'daemon':
            from .daemon import run, service_action
            if args.action == 'run':
                run(store)
                return None
            if args.action == 'status':
                return daemon_status(store)
            if args.action in ('pause', 'resume'):
                from .processing import set_operator_pause
                return set_operator_pause(store, args.action == 'pause')
            return service_action(store, args.action)
        if args.command == 'schedules':
            from .scheduling import ScheduleBook
            book = ScheduleBook(store)
            if args.action == 'list':
                return book.list()
            if args.action == 'save':
                raw = sys.stdin.read(65537) if args.file == '-' else Path(args.file).read_text(encoding='utf-8')
                if len(raw) > 65536:
                    raise ValueError('定时配置过长')
                return {'schedule': book.upsert(json.loads(raw)), 'background': daemon_status(store)}
            if args.action == 'delete':
                return book.delete(args.schedule_id)
            return book.set_enabled(args.schedule_id, args.action == 'resume')
        box = Outbox(store)
        if args.action == 'list':
            return box.list(args.status)
        if args.action == 'retry':
            return box.retry(args.notification_id)
        if args.action == 'cancel':
            box.get(args.notification_id)
            with store.transaction() as db:
                db.execute("UPDATE notifications SET status='cancelled' WHERE id=? AND status!='delivered'", (args.notification_id,))
            return box.get(args.notification_id)
        message = sys.stdin.read(16001) if args.message == '-' else args.message
        return box.publish(message, event_key=args.event_key, topic=args.topic, origin_thread=args.origin_thread,
                           context=args.context, delay=args.delay)
    if args.command == "setup":
        from .adapters import render_project_configs
        initialize_runtime(store)
        friends = Registry(store).sync()
        configs = render_project_configs(root)
        return {"initialized": True, "friends": friends, "configuration": configs,
                "next": "在此目录启动 codex；首次信任项目及 /hooks，后续自动读取老五。"}
    if args.command == "doctor":
        return doctor(root)
    if args.command == "backup":
        return store.backup()
    if args.command == "restore":
        from .adapters import render_project_configs
        result = store.restore(args.path)
        initialize_runtime(store)
        reconcile(store)
        Registry(store).sync()
        result["configuration"] = render_project_configs(root)
        return result
    if args.command == "memory":
        if args.action == "search":
            return store.memory_search(args.query, args.scope)
        if args.action == "save":
            content = sys.stdin.read(100001) if args.content == "-" else args.content
            return store.memory_upsert(content, scope=args.scope, kind=args.kind, memory_id=args.memory_id)
        return store.memory_forget(args.memory_id)
    if args.command == "friends":
        registry = Registry(store)
        if args.action == "sync":
            return registry.sync()
        if args.action == "list":
            return registry.list()
        return registry.ensure(json.loads(Path(args.file).read_text(encoding="utf-8")))
    if args.command == "tasks":
        if args.action == 'metrics':
            from .metrics import aggregate
            return aggregate(store)
        if args.action == 'review':
            if os.environ.get('LAOWU_ROLE') == 'worker' or os.environ.get('LAOWU_MANAGED_WORKER') == '1':
                raise PermissionError('朋友运行不能提交主会话验收')
            from .acceptance import review
            return review(store,args.task_id,args.run_id,args.decision,args.summary,args.evidence)
        initialize_runtime(store)
        if args.action == "diagnose":
            return task_diagnostics(store, args.task_id)
        reconcile(store)
        if args.action == "list":
            with store.connect() as db:
                return [dict(row) for row in db.execute("SELECT t.id,t.friend_id,t.status,t.updated_at,coalesce(a.state,'legacy') AS acceptance_state FROM tasks t LEFT JOIN task_acceptance a ON a.task_id=t.id ORDER BY t.updated_at DESC")]
        if args.action == "show":
            return task_record(store, args.task_id)
        if args.action == "cancel":
            return cancel_task(store, args.task_id)
        manager = Manager(store)
        try:
            if args.action == "resume":
                from .background import require_background_for_task
                detached = require_background_for_task(store, args.task_id)
                task = manager.resume(args.task_id)
                if detached:
                    return task
            else:
                request = sys.stdin.read(32001) if args.request == "-" else args.request
                if args.background:
                    from .background import daemon_status
                    if not daemon_status(store)['running']:
                        raise RuntimeError('后台服务未运行，请先 daemon install/start')
                task = manager.submit(args.friend_id, request, args.workspace, args.idempotency_key,
                                      detached=args.background, origin_thread=args.thread, context=args.context)
                if args.background:
                    return task
            while True:
                task = manager.wait(task["id"], 30)
                if task["status"] in TERMINAL:
                    return task
        finally:
            manager.close()
    raise ValueError("未知命令")


def main(argv=None):
    entry = Path(__file__).resolve().parents[1] / 'scripts/laowu.py'
    if (not sys.executable.isascii() and entry.is_file()
            and (entry.parent.parent / '.venv/bin/python').exists()):
        # The installed console entry needs the same bootstrap as scripts/.
        os.execv(sys.executable, [sys.executable, str(entry), *(sys.argv[1:] if argv is None else argv)])
    args = parser().parse_args(argv)
    root = project_root(args.root)
    os.umask(0o077)
    try:
        result = execute(args, root)
        if result is not None:
            print(json_text(result))
        return 1 if isinstance(result, dict) and result.get("status") in {"blocked", "failed", "interrupted", "cancelled"} else 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        safe = str(exc) if isinstance(exc, (ValueError, RuntimeError, PermissionError)) else type(exc).__name__
        output = {"ok": False, "error": safe}
        # Never corrupt MCP stdout on startup failure.
        print(json_text(output), file=sys.stderr if args.command == "mcp" else sys.stdout)
        return 1
