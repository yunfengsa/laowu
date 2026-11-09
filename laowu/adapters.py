"""CLI adapters. Profiles are local; authentication remains owned by each CLI.

This module prepares launches, never calls a model or manages a process. The
supervisor must use a new process group, drain pipes without logging them, and
terminate the group when its owning session goes away.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .common import atomic_write, json_text, valid_id
from .diagnostics import classify_error, error_code


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str, diagnostics: dict | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}


@dataclass
class WorkerLaunch:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    stdin: str


CLIENTS = ("codex",)
VERIFIED_CODEX_VERSION = "0.163.0"
_REQUIRED = {"codex": ("--json", "--ephemeral", "--sandbox", "--skip-git-repo-check", "--add-dir",
                       "--strict-config")}


def _safe_target(root: Path, path: Path) -> Path:
    if not path.resolve().is_relative_to(root.resolve()):
        raise AdapterError("unsafe_path", "项目配置或运行目录不能通过符号链接指向项目外")
    return path


def _probe(client: str) -> dict:
    if client not in CLIENTS:
        return {"path": None, "version": None, "available": False,
                "supported": False, "error": "unsupported_client"}
    path = shutil.which(client)
    result = {"path": path, "version": None, "available": bool(path), "supported": False}
    if not path:
        result["error"] = "client_missing"
        return result
    try:
        version = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=15, check=False)
        # Version output is allowed metadata, but never return arbitrary diagnostics.
        match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.]+)?\b", version.stdout)
        result["version"] = match.group() if match else None
        result["verified_version"] = result["version"] == VERIFIED_CODEX_VERSION
        args = [path, "exec", "--help"]
        help_result = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)
        help_text = help_result.stdout
        main_help = subprocess.run([path, "--help"], capture_output=True, text=True,
                                   timeout=15, check=False)
        result["no_daemon"] = "--no-daemon" in main_help.stdout
        result["supported"] = bool(result["version"] and help_result.returncode == 0
                                   and all(flag in help_text for flag in _REQUIRED[client]))
        if not result.get("no_daemon"):
            result["supported"] = False
        if not result["supported"]:
            result["error"] = "unsupported_client"
    except (OSError, subprocess.TimeoutExpired):
        result["error"] = "client_probe_failed"
    return result


def detect_clients() -> dict:
    """Read executable/version/help only. Never inspect authentication files."""
    return {client: _probe(client) for client in CLIENTS}


def _configured_mcp_names(executable: str, cwd: Path) -> set[str]:
    """Read effective server names only, discarding configs/URLs/env from memory.

    Codex merges -c table overrides with lower config layers. An empty table is
    therefore NOT an MCP allowlist. Never persist this command's raw output.
    """
    try:
        result = subprocess.run([executable, "mcp", "list", "--json"], cwd=cwd,
                                capture_output=True, text=True, timeout=20, check=False)
        if result.returncode:
            raise AdapterError("mcp_inventory_failed", "无法读取 Codex MCP 名称，已停止朋友启动",
                               {"source": "mcp_inventory", "exit_code": result.returncode,
                                **classify_error(result.stderr)})
        items = json.loads(result.stdout)
        if not isinstance(items, list):
            raise ValueError("invalid inventory")
        names = set()
        for item in items:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not name or len(name) > 256 or any(ord(c) < 32 for c in name):
                raise ValueError("invalid server name")
            names.add(name)
        return names
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError) as exc:
        raise AdapterError("mcp_inventory_failed", "无法读取 Codex MCP 名称，已停止朋友启动") from exc


def _validate_config(executable: str, cwd: Path, overrides: dict) -> None:
    """Validate current schemas without creating a model turn.

    0.163.0 rejects --strict-config for `mcp list`; a stdio app-server checks the
    schema and exits on EOF. Neither its stdout nor stderr may be persisted.
    """
    argv = [executable, "app-server", "--strict-config", "--stdio"]
    for key, value in overrides.items():
        argv += ["-c", key + "=" + _toml(value)]
    try:
        result = subprocess.run(argv, cwd=cwd, input="", capture_output=True,
                                text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError("config_validation_failed", "Codex 严格配置校验未完成，已停止启动") from exc
    if result.returncode:
        raise AdapterError("config_validation_failed", "Codex 严格配置校验失败，请检查当前版本与配置",
                           {"source": "config_validation", "exit_code": result.returncode,
                            **classify_error(result.stderr)})


def _toml(value: object) -> str:
    """Encode the deliberately small config subset as TOML inline values."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(_toml(str(key)) + " = " + _toml(item)
                                for key, item in value.items()) + "}"
    raise AdapterError("invalid_profile", "配置值必须为字符串、数字、布尔、列表或字典")


def _command(root: Path, client: str, python: str = sys.executable) -> list[str]:
    # Keep the launch executable ASCII-only. The entrypoint re-enters the same
    # project venv through a private ASCII path for Codex 0.163.0 compatibility.
    # Saved MCP/hooks must survive /tmp cleanup, so never persist a venv alias.
    if Path(python).is_file():
        python = str(Path(python).resolve())
    if not python.isascii():
        raise AdapterError('interpreter_path', 'Codex 需要 ASCII 路径的启动解释器；项目目录可包含中文')
    return [python, str(root / "scripts" / "laowu.py"), "--root", str(root),
            "mcp", "--client", client]


def _servers(root: Path, friend: dict, client: str, run_id: str, token: str) -> dict:
    extra = friend.get("mcp_servers") or {}
    if not isinstance(extra, dict) or "laowu" in extra:
        raise AdapterError("invalid_profile", "朋友 MCP 配置格式错误或占用 laowu 保留名称")
    servers = {}
    for name, config in extra.items():
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", name) or not isinstance(config, dict):
            raise AdapterError("invalid_profile", "朋友 MCP 名称或配置不合法")
        if not config.get("command") and not config.get("url"):
            raise AdapterError("invalid_profile", "MCP 必须指定 command 或 url")
        allowed = {"command", "args", "cwd", "env_vars", "url", "bearer_token_env_var",
                   "env_http_headers", "enabled_tools", "disabled_tools", "startup_timeout_sec",
                   "tool_timeout_sec", "required", "enabled", "default_tools_approval_mode"}
        if set(config) - allowed:
            raise AdapterError("unsupported_mcp_config", "MCP 包含不支持的字段；凭证请使用环境变量引用")
        servers[name] = {**copy.deepcopy(config), "enabled": True}
    args = _command(root, client)
    args += ["--worker-run", run_id, "--worker-token", token]
    servers["laowu"] = {
        "command": args[0], "args": args[1:], "cwd": str(root), "enabled": True,
        "enabled_tools": ["memory_search", "task_checkpoint", "event_emit", "context_read"],
        "default_tools_approval_mode": "prompt",
        "tools": {name: {"approval_mode": "approve"}
                  for name in ("memory_search", "task_checkpoint", "event_emit", "context_read")},
    }
    return servers


def _role(friend: dict, workspace: Path) -> str:
    skills = friend.get("skills", [])
    if not isinstance(skills, list):
        raise AdapterError("invalid_profile", "skills 必须为列表")
    text = ("LAOWU_MANAGED_WORKER: 1\n"
            f"你是老五的朋友 {friend.get('name', friend['id'])}，身份 ID：{friend['id']}。\n"
            "你只完成老五交给你的任务，结果交给老五，不扮演面向用户的老五。\n"
            "本身份与主项目老五身份不同。不要召唤其他 agent、启动脱离监管的后台进程或修改主项目配置。\n"
            "遵守工具权限。若缺少许可或能力，明确报告 blocked，不绕过权限，不把未执行说成已完成。\n"
            "不保存原始聊天、工具输出、凭证或推测的用户心理诊断。用 laowu MCP 读取必要记忆和提交简短检查点；后台任务可用受限 event_emit 交接重要事件。\n"
            f"授权任务工作目录：{workspace}\n"
            f"允许修改工作目录内容：{'是' if friend.get('can_write') else '否'}\n\n"
            f"{friend.get('instructions', '')}\n")
    for skill in skills:
        if not isinstance(skill, dict) or not isinstance(skill.get("content"), str):
            raise AdapterError("invalid_profile", "每项 skill 需要 name 和 content")
        text += f"\n## 已初始化技能：{skill.get('name', 'unnamed')}\n{skill['content']}\n"
    return text


def prepare_worker(root: Path, friend: dict, client: str, run_id: str, prompt: str,
                   workspace: Path, worker_token: str = "", *, access_context=None) -> WorkerLaunch:
    """Create a private run profile and return an argv/stdin launch specification."""
    if friend.get('id') == 'laowu':
        raise AdapterError('invalid_profile', 'laowu 不能作为 worker 身份')
    if client not in CLIENTS:
        raise AdapterError("unsupported_client", "不支持该客户端")
    if access_context:
        from .access import restrict_friend
        friend = restrict_friend(friend, access_context, root=root, workspace=workspace)
    try:
        valid_id(run_id)
        valid_id(friend["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AdapterError("invalid_profile", "run_id 或 friend.id 不合法") from exc
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    if not workspace.is_dir():
        raise AdapterError("workspace_missing", "任务工作目录不存在")
    info = _probe(client)
    if not info.get("supported"):
        raise AdapterError(info.get("error", "unsupported_client"),
                           f"{client} 未安装或缺少所需 headless 参数；请先运行 doctor")
    runtime = _safe_target(root, root / ".laowu" / "runtime" / run_id)
    if runtime.exists():
        raise AdapterError("run_exists", "运行目录已存在，不能覆盖运行实例")
    role = _role(friend, workspace)
    servers = _servers(root, friend, client, run_id, worker_token)
    can_write = bool(friend.get("can_write"))
    capabilities = friend.get("tools", ["read", "write", "shell"] if can_write else ["read"])
    if not isinstance(capabilities, list) or any(t not in {
            "read", "write", "shell", "search", "web", "memory", "checkpoint"} for t in capabilities):
        raise AdapterError("invalid_profile", "未知的 Codex 朋友工具能力")
    if "write" in capabilities and not can_write:
        raise AdapterError("invalid_profile", "write 能力需要 can_write=true")
    runtime.mkdir(parents=True, mode=0o700)
    os.chmod(runtime, 0o700)
    # These are overlays, not a snapshot of os.environ. Launch descriptors may be
    # saved briefly on disk; API keys and account environment must never go there.
    env = {"LAOWU_ROOT": str(root), "LAOWU_WORKER_RUN": run_id,
           "LAOWU_WORKER": "1", "LAOWU_CLIENT": client}
    atomic_write(runtime / "LAOWU_FRIEND.md", role)
    executable = info["path"]
    try:
        inherited_names = _configured_mcp_names(executable, runtime)
    except AdapterError:
        shutil.rmtree(runtime)
        raise
    scoped_servers = {name: {"enabled": False} for name in inherited_names - set(servers)}
    scoped_servers.update(servers)
    shell_enabled = any(t in capabilities for t in ("read", "shell"))
    overrides = {
        "approval_policy": "never", "project_doc_max_bytes": 0,
        "developer_instructions": role,
        "mcp_servers": scoped_servers,
        "agents.enabled": False, "features.multi_agent": False, "features.multi_agent_v2": False, "features.hooks": False,
        "features.plugins": False, "features.apps": False,
        "features.memories": False, "features.shell_tool": shell_enabled,
        "web_search": "live" if any(t in capabilities for t in ("search", "web")) else "disabled",
    }
    if friend.get('_slack_scoped'):
        for feature in ('shell_tool', 'unified_exec', 'view_image', 'computer_use', 'browser_use',
                        'image_generation', 'code_mode', 'code_mode_host', 'workspace_dependencies',
                        'skill_search', 'skill_mcp_dependency_install'):
            overrides['features.' + feature] = False
        overrides['features.skip_host_skill_discovery'] = True
        # Deferred MCP calls use the isolated code-mode host even when the
        # model has no shell. Disabling the host disables memory/checkpoints.
        overrides['features.code_mode_host'] = True
        if friend.get('_slack_shell'):
            from .slack_permissions import profile_overrides, probe
            if not probe(root, workspace):
                shutil.rmtree(runtime)
                raise AdapterError('filesystem_isolation_failed', 'Slack 工程文件隔离验证失败')
            overrides.update(profile_overrides(root, workspace, can_write))
            overrides['features.shell_tool'] = shell_enabled
    try:
        _validate_config(executable, runtime, overrides)
    except AdapterError:
        shutil.rmtree(runtime)
        raise
    argv = [executable, "--no-daemon"]
    if friend.get('_slack_shell'):
        argv += ['-C', str(workspace)]
    argv += ["exec", "--strict-config", "--json", "--ephemeral", "--skip-git-repo-check"]
    if not friend.get('_slack_shell'):
        argv += ["--sandbox", "workspace-write" if can_write else "read-only"]
    if can_write:
        argv += ["--add-dir", str(workspace)]
    # Explicit per-name disabling constrains inherited MCP without dropping login/provider config.
    for key, value in overrides.items():
        argv += ["-c", key + "=" + _toml(value)]
    argv += ["-"]
    return WorkerLaunch(argv, runtime, env, prompt)


def parse_event(client: str, line: str, *, telemetry=False) -> dict:
    """Extract assistant text/session/status; the guardian validates the last answer.

    Older event shapes omit phase, so their commentary can remain in RAM briefly.
    Tool output and reasoning are never forwarded or persisted here.
    """
    if client not in CLIENTS or not isinstance(line, str) or len(line) > 2_000_000:
        return {}
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    kind = value.get("type")
    result = {}
    sid = value.get("session_id") or (value.get("thread_id") if kind == "thread.started" else None)
    if isinstance(sid, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", sid):
        result["session_id"] = sid
    item = value.get("item") or {}
    if (kind == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message"
            and item.get("phase") in (None, "final")):
        result.update(event="message", message=item.get("text", ""))
    elif kind == "turn.completed":
        result.update(event="result", status="completed")
    elif kind in {"turn.failed", "error"}:
        diagnostic = classify_error(value)
        result.update(event="error", status="failed", error=error_code(diagnostic),
                      terminal=kind == "turn.failed", diagnostic=diagnostic)
    if telemetry:
        from .monitoring import event_metadata
        result.update(event_metadata(value))
    if "message" in result:
        if not isinstance(result["message"], str):
            result.pop("message")
        else:
            pass
    return result


def _merge_owned(current: object, desired: object, previous: object, label: str) -> object:
    """Preserve unrelated keys; refuse overwriting edits to previously managed keys."""
    if isinstance(desired, dict):
        if current is None:
            current = {}
        if not isinstance(current, dict):
            raise AdapterError("config_conflict", f"配置类型冲突：{label}")
        merged = copy.deepcopy(current)
        old = previous if isinstance(previous, dict) else {}
        for key, value in desired.items():
            merged[key] = _merge_owned(merged.get(key), value, old.get(key), f"{label}.{key}")
        return merged
    if isinstance(desired, list) and label.endswith(tuple(".hooks." + e for e in
            ("SessionStart", "SessionEnd", "Stop", "UserPromptSubmit", "Interrupt"))):
        if current is None:
            current = []
        if not isinstance(current, list):
            raise AdapterError("config_conflict", f"钩子配置冲突：{label}")
        merged = copy.deepcopy(current)
        for old in previous or []:
            if old not in merged:
                raise AdapterError("config_conflict", f"受管钩子已被修改：{label}")
            merged.remove(old)
        for item in desired:
            if item not in merged:
                merged.append(item)
        return merged
    if current is not None and current != desired and (previous is None or current != previous):
        raise AdapterError("config_conflict", f"受管配置与现有内容冲突：{label}")
    return copy.deepcopy(desired)


def render_project_configs(root: Path, python: str = sys.executable) -> dict:
    """Merge only laowu MCP/hooks. Validate all conflicts before writing any file."""
    root = Path(root).resolve()
    manifest_path = _safe_target(root, root / ".laowu" / "managed-configs.json")
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    except (ValueError, OSError) as exc:
        raise AdapterError("config_conflict", "受管配置清单无法读取") from exc
    if not isinstance(previous, dict):
        raise AdapterError("config_conflict", "受管配置清单必须是对象")
    desired = {}
    args = _command(root, "codex", python)
    server = {"command": args[0], "args": args[1:], "cwd": str(root)}
    hooks = {}
    for event in ("SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "Interrupt"):
        command = [args[0], str(root / "scripts" / "laowu.py"), "--root", str(root),
                   "hook", "--client", "codex", "--event", event]
        hooks[event] = [{"hooks": [{"type": "command", "command": shlex.join(command), "timeout": 3}]}]
    desired[".codex/hooks.json"] = {"hooks": hooks}
    block = ("# BEGIN LAOWU MANAGED MCP\n[mcp_servers.laowu]\n"
             f"command = {_toml(server['command'])}\nargs = {_toml(server['args'])}\n"
             f"cwd = {_toml(server['cwd'])}\nenabled = true\n"
             'default_tools_approval_mode = "approve"\n# END LAOWU MANAGED MCP')
    outputs = {}
    for relative, fragment in desired.items():
        path = _safe_target(root, root / relative)
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (ValueError, OSError) as exc:
            raise AdapterError("config_conflict", f"不能解析现有 JSON：{relative}") from exc
        merged = _merge_owned(current, fragment, previous.get(relative), relative)
        outputs[relative] = json_text(merged) + "\n"
    relative = ".codex/config.toml"
    path = _safe_target(root, root / relative)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = r"(?ms)^# BEGIN LAOWU MANAGED MCP\n.*?^# END LAOWU MANAGED MCP"
    match = re.search(pattern, current)
    if len(re.findall(pattern, current)) > 1 or re.search(r"(?m)^\s*mcp_servers\s*=", current):
        raise AdapterError("config_conflict", "Codex MCP 存在重复受管块或不兼容的内联表")
    if match:
        if match.group() != previous.get(relative) and match.group() != block:
            raise AdapterError("config_conflict", "Codex 受管 MCP 块被修改，请先保留修改后再重新初始化")
        current = current[:match.start()] + block + current[match.end():]
    else:
        if previous.get(relative) or re.search(r'(?m)^\s*\[\s*mcp_servers\s*\.\s*["\']?laowu', current):
            raise AdapterError("config_conflict", "Codex 已有 laowu 配置或受管标记丢失")
        current = current.rstrip() + ("\n\n" if current.strip() else "") + block + "\n"
    outputs[relative] = current
    for relative, content in outputs.items():
        atomic_write(root / relative, content)
    desired[".codex/config.toml"] = block
    atomic_write(manifest_path, json_text(desired) + "\n")
    return {"files": [str(root / p) for p in outputs],
            "clients": {client: {"configured": True, "trust_required": True,
                                 "verified_version": VERIFIED_CODEX_VERSION} for client in CLIENTS},
            "managed_manifest": str(manifest_path)}
