# Codex 适配与运行边界

当前版本仅支持 Codex，适配基线为 **0.163.0**，包括 CLI 参数、配置 schema、MCP 合并及本地 App Server 主动轮次。其他版本会进行能力检测，但不因此声称完整兼容。其他客户端会明确返回 `unsupported_client`，不会生成它们的配置。

## 登录与启动

朋友由受管理的 `codex --no-daemon exec --strict-config --json --ephemeral` 子进程执行。启动前检查可执行文件、版本和帮助中的必需参数；还会读取有效 MCP 的名称，并用空 stdin 的 `codex app-server --strict-config --stdio` 检查配置 schema。该预检不创建模型轮次，不读取认证文件。未安装、缺少参数、MCP 清单不可读或严格校验失败时停止启动，返回结构化错误并清理本次临时配置。0.163.0 的 `mcp list` 不支持 `--strict-config`，因此不用于严格 schema 校验。

继续使用当前 HOME、CODEX_HOME、用户模型和 provider 配置。项目不会复制账号凭证、配置新的 API key，或使用 `--ignore-user-config`。`WorkerLaunch.env` 只包含本项目非秘密覆盖项；监督进程从自己的环境继承现有登录环境。

适配基线版本的命令工具会误解包含中文的 Python 解释器路径。项目启动脚本为同一 `.venv` 建立权限 0700 的 `/tmp/laowu-python-用户ID-项目摘要/venv` 路径别名，不复制依赖或凭据；启动及后台工作继续使用同一虚拟环境。别名丢失时自动重建，已存在但归属或目标不符则报错，不覆盖。中文路径的启动行为由入口回归测试覆盖，目标宿主仍需实际验证。

## 朋友的上下文和能力

每个运行实例使用 `.laowu/runtime/<run_id>/`，权限为 `0700`。其独立角色指令及选定技能正文写入 `LAOWU_FRIEND.md`，并通过 `developer_instructions` 注入。任务内容通过 stdin 传入；`project_doc_max_bytes=0` 避免主老五的 AGENTS.md 人格混入朋友。

运行参数使用 `agents.enabled=false` 关闭原生朋友调度，并关闭 hooks、plugins、apps 与自动记忆。Codex 会合并 MCP 配置层，`mcp_servers={}` 不能清除继承的服务器。因此启动前从 `mcp list --json` 中只保留服务器名称，对未选服务器逐项设置 `enabled=false`，对朋友声明的服务器显式设置 `enabled=true`；不保存清单中的 URL、环境值或认证信息，也不修改用户配置。

老五 MCP 使用本次运行令牌，客户端设置 `enabled_tools=["memory_search", "task_checkpoint", "event_emit", "context_read"]`。服务端约束必要记忆读取、本任务检查点和通知归属；`event_emit` 只允许已授权后台任务使用，不能改变接收会话；`context_read` 续读仍逐次校验本任务的权限与引用版本。仅这四个本地接口使用 `approval_mode="approve"`，其他接口默认要求批准。项目主会话的 laowu MCP 使用服务器级 `default_tools_approval_mode="approve"`，以实现用户授权的自动记忆和朋友管理。该策略只作用于本项目本地服务，不改变外部 MCP 的批准设置；服务端校验、任务归属和宿主沙箱限制仍生效。

`can_write=false` 对应 `read-only`；允许写入的工程朋友使用 `workspace-write`，通过 `--add-dir` 加入已授权任务目录。`approval_policy=never` 表示不能完成的批准请求立即失败，不表示绕过沙箱。不会设置 `danger-full-access` 或任何 bypass 参数。

Codex 的内置工具不是逐项权限隔离接口。read/shell 控制 shell 功能开关，search/web 控制网页搜索；实际写权限由沙箱决定。没有 shell 能力的朋友关闭 shell。选定技能正文会初始化；本项目没有声称完全清除客户端可能展示的全局技能目录。受管理角色指令要求只使用初始化的能力，不递归委派。

## 状态与关闭

适配器只准备参数，不管理进程。监督进程必须以独立进程组启动 CLI，在宿主连接关闭时向组发送 TERM，超时后 KILL，并回收退出状态。`--no-daemon` 避免把朋友模型循环交给共享 Codex daemon。禁止朋友主动启动脱离进程组的后台任务；操作系统级守护进程或用户其他 Codex 会话不由项目终止。

原生 hooks 的 SessionEnd 不保证对应 UI 标签关闭瞬间，因此关闭检测以项目监督机制为准。0.2 的异步/定时任务由独立后台服务持有监督管道，关闭原会话不再终止它们；普通同步任务保持原有生命周期。内存中解析 stdout，只持久化会话 ID、经过验证的最后一条助手交接和结果状态；缺少 phase 的事件可能包含过程说明，但不额外落盘。工具输出、推理、任意 stderr 和原始错误文本不作为项目记忆保存。CLI 的 `--ephemeral` 关闭该朋友的原生会话持久化；恢复以本项目摘要和检查点为准。

失败运行现在还保存白名单错误分类、分类依据、可用的 HTTP 状态码、退出码和 stderr 字节数。`turn.failed` 是终态；单独的 `error` 事件可能来自重试，后续成功的 `turn.completed` 可以清除该中间错误。非零退出和最终 JSON 交接校验仍然有效。stderr 只在有限内存窗口内分类，不保存文本；未知错误继续使用 `client_error`。这些类别是诊断线索，不证明认证或网络服务的真实根因。

用 `python3 scripts/laowu.py tasks diagnose <task_id>` 查询各次运行的脱敏元数据；该命令不输出任务请求、交接正文、检查点、启动参数或环境值，也不启动模型或恢复任务。`tasks show` 和 MCP `task_status` 另附 `runs` 字段。旧数据库在运行层初始化时增补 `runs.diagnostics` 列，已有状态及错误不改写；旧错误无法追溯补全，诊断为空是正常情况。`doctor` 检查 socket 遇到权限限制时返回 `codex_socket_exists=null` 和固定错误分类；静态检查成功也不代表模型调用成功。诊断方法、测试限制和宿主复验步骤见 [验证说明](verification.md)。

主动回复使用本机 Unix WebSocket 的 App Server `turn/start` 独立工具输出模式，不使用原始 newline JSON socket，也不把 MCP 日志通知当作主动唤醒。此能力带 experimental 标志；使用或升级前需在目标宿主复验。`thread/items/list` 在该版本尚未实现，回执改用 `thread/turns/list`。原生会话及 MCP 在最后客户端断开后可能继续保留约 30 分钟，因此不能精确代表桌面窗口在线；可用 `laowu chat` 终端代理获取准确订阅租约。详见 [后台说明](background.md)。

## 项目配置的维护

`.codex/` 整体被 Git 忽略；首次克隆或移动目录后运行 `setup`，按当前路径生成配置。`render_project_configs` 只维护 `.codex/config.toml` 中标记的 laowu MCP 块，以及 `.codex/hooks.json` 中本项目的 SessionStart、UserPromptSubmit、Stop、Interrupt、SessionEnd 钩子。Interrupt 用于用户中断当前轮次时停止关联运行。额外用户配置保留；已有受管内容被手工改写时报告冲突。受管清单存放在 `.laowu/managed-configs.json`，便于恢复目录后重生成绝对路径。首次原生工作区/钩子信任仍由 Codex 控制，项目不会自动绕过。

参数与 hooks 行为以 doctor 能力检测、启动严格校验和原生信任状态为准。适配器单元测试采用模拟 CLI 事件和临时目录；不启动模型的配置校验与端到端模型验证须分别记录，记录保存在本地。Claude/Gemini、远端托管执行与第三方客户端界面隐藏均未实现。

官方依据：[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)、[Codex hooks](https://learn.chatgpt.com/docs/hooks)、[MCP 工具策略示例](https://learn.chatgpt.com/docs/extend/mcp)、[approve 模式实现](https://github.com/openai/codex/blob/main/codex-rs/core/src/mcp_tool_call.rs)。
