# 定时任务与主动通知

当前使用 APScheduler 3.11.3 的时间计算与调度，SQLite 保存计划、触发批次、任务、连接和投递状态。每个项目一个 systemd 服务；不部署 Redis、Celery 或第二个模型 API。复用当前用户的 Codex 登录、默认模型和权限设置。

## 安装与日常使用

```sh
python3 scripts/laowu.py daemon install
python3 scripts/laowu.py daemon status
```

安装会启用项目专属 systemd 服务并立即启动，机器重启后也启动。root 用户使用系统服务，普通用户使用用户服务；后者退出登录后继续及开机启动还需要系统管理员配置该用户的 linger。服务不会复制认证文件。机器断电或休眠期间不能执行。

通常直接告诉老五希望什么时候做什么、完成后如何通知即可。主会话首次加载新 MCP 配置可能需要重新连接；当前已打开的旧 MCP 仍可用 CLI 管理新功能。

```sh
python3 scripts/laowu.py daemon pause
python3 scripts/laowu.py daemon resume
python3 scripts/laowu.py daemon stop
python3 scripts/laowu.py daemon start
python3 scripts/laowu.py schedules list
python3 scripts/laowu.py notifications list
```

`pause` 暂停新任务领取、时间触发和消息投递，不中断已经执行的任务；`stop` 停止服务及所属工作进程。明确取消任务使用 `tasks cancel TASK_ID`。服务重启只自动恢复因服务关闭或崩溃而中断的后台任务，已完成、失败、受阻或已取消任务不会自动重跑。

## 定时计划

主会话使用 `schedule_upsert(spec)`。也可将 JSON 保存为文件后调用 `schedules save 文件.json`，或通过 stdin：`schedules save -`。下面仅是格式示例，不会自动创建：

```json
{
  "id": "daily-check",
  "name": "每日项目检查",
  "friend_id": "engineer",
  "request": "检查指定项目的测试状态，交回结果；未经任务授权不发布外部内容。",
  "workspace": "/实际存在的项目目录",
  "timezone": "Asia/Shanghai",
  "context": "root",
  "trigger": {"type": "cron", "expression": "0 9 * * *"},
  "misfire_grace_seconds": 86400
}
```

支持一次性 `date/run_at`、间隔 `interval/seconds`（至少 10 秒）和五字段 `cron/expression`。默认北京时间；有夏令时歧义的一次性时间必须带 UTC 偏移。cron 周日为 0 或 7，日期和星期均受限制时采用常规 OR 语义。

停机漏跑默认只补最近一期，超过默认 24 小时容忍期跳过；时间计算与触发记录在同一事务提交。相同计划 ID 更新配置，已经产生的任务保留原任务快照。每期稳定幂等 ID 避免崩溃后重复建任务。周期任务可排队，应按任务耗时合理设定频率；全项目仍限制最多两个工作进程和重叠目录写入串行。

`schedules pause ID`、`resume ID` 控制未来触发，`delete ID` 软删除且不能复用原 ID；已经产生的任务须另行取消。暂停后恢复也按漏跑规则补最近一期。

## 异步工作与事件

主会话使用 `task_start(background=true, context="normal"|"root", ...)`，必须先绑定宿主会话。后台服务接管任务，结果固定送回发起会话。CLI 可用于外部脚本：

```sh
python3 scripts/laowu.py tasks run engineer '用户已授权的具体工作' --background --thread CODEX_THREAD_ID
python3 scripts/laowu.py notifications emit '实际事件内容' --key 稳定事件标识 --thread CODEX_THREAD_ID
```

省略 `--thread` 表示无归属事件。同一事件重试必须沿用 key；相同 key 的不同内容会报错。CLI 支持 `--context root` 保留维护语境，默认普通交流。程序内事件可调用 `event_emit`。这一版提供事件发布接口和任务完成事件；没有自动接入邮件、文件变化等外部来源，新增来源需另行定义触发条件。

| 事件来源 | 接收规则 |
| --- | --- |
| 有发起会话的异步任务 | 始终回原会话；原会话离线时排队，其他会话在线也不转投 |
| 定时任务、无归属事件 | 首次发送时选择本项目最新连接的会话；无人连接则排队 |
| 已发出但回执不确定 | 保留首次目标，只核对原目标回执，不盲目重发或改投 |

正在回复的会话会等到空闲再接收。服务每约 2 秒检查一次，忙碌或连接失败后退避；不保证严格实时。连接心跳与空队列不调用模型，也不发送“状态未变化”消息。

## 主动说话的通道与边界

后台通过本机 Codex App Server 的 Unix WebSocket，发送独立的 `laowu.background_event` 工具交接并启动助手轮次。它会产生老五在该会话中的回复，不伪造用户提问，也不依赖只能在请求期间使用的 MCP 日志通知。事件只提供有关任务的最小交接，老五读取对应结果后向你表达。

**原生桌面端限制：Codex 0.163.0 没有公开每个会话的实际窗口订阅数，最后客户端断开后仍可能保留会话及 MCP 约 30 分钟。** 原生模式以有效项目 MCP 进程和 loaded 状态判断可达，因此这段保留期内可能将结果写入刚关闭的会话，不能承诺精确的“物理窗口在线”语义。该结果仍保存在原会话；原生窗口重新打开也未必会刷新连接先后次序。

需要精确感知终端连接和最新接入顺序时，使用：

```sh
python3 scripts/laowu.py chat
python3 scripts/laowu.py chat -- resume CODEX_THREAD_ID
```

入口在已有 App Server 与 Codex CLI 之间建立项目私有代理。成功订阅项目会话才登记连接，每 10 秒续期，断开立即注销；代理进程突然崩溃时通过 PID 身份和最多 30 秒租约失效排除。代理不保存原始传输内容，不启动另一套登录或改权限；需要本机现有 App Server 可连接。该线程若从代理切回桌面端，应重建项目 MCP 连接以重新登记原生连接，避免旧的代理断开记录掩盖重连。

接口按 Codex 0.163.0 适配，使用实验性的独立工具输出轮次，安装或升级后需按 [验证说明](verification.md) 复验。持久回执使用实际可用的 `thread/turns/list`；该版本 schema 中的 `thread/items/list` 尚未实现。临时 `ephemeral` 会话没有所需持久历史，因此暂不投递。此处不承诺所有未来 Codex 版本、其他桌面客户端或远程实例兼容。

投递状态：`pending` 等待，`sending` 已保存发送意图，`submitted` 已取得轮次 ID，`delivered` 已看到该轮成功完成且存在助手最终回复。**delivered 不表示用户已读。** `uncertain` 表示回执不确定，自动核对近期最多 100 轮；查不到也不重复调用模型。`failed` 表示目标轮次失败。`notifications retry ID` 仅重新检查已有目标的回执；如确实需要重新表达，应在核实后明确发布新事件。

## 备份、隐私和故障

调度及队列都在受管 SQLite 中，迁移先做一致性备份。恢复前停止后台服务和本项目会话；恢复后清空旧连接和进程身份，后台保持暂停，未确定投递先核对回执。源代码和虚拟环境不在数据快照中，需要在目标目录另行准备。内部工作目录随项目迁移，外部目录需另行迁移。

遗忘使用独立的临时处理屏障：先持久化遗忘意图，再停止活动任务、清理受管上下文、通知、计划及旧备份。全部清理提交并完成账本后，自动允许新输入；不会重收集旧异步结果或恢复旧任务、通知、计划。用户此前执行的 `daemon pause` 保留，备份恢复后的暂停也保留。之后如需定时工作，重新确认内容并创建计划；原生主会话的旧任务管理器须先 `bootstrap` 刷新上下文。Codex 自身已保存的会话历史不在项目删除范围内。

遗忘失败或进程崩溃时，账本 `pending=true` 仍阻止模型启动及内容投递，`daemon resume` 不能绕过。修复文件清理、存储或活动进程故障后，重新打开本地 Store 会重试未完成清理；数据库已提交而仅账本收尾失败时，不重复抹去清理后新增的输入。遗忘、启动修复和备份恢复用独立的操作锁串行，停止工作进程时不持有数据库写锁。遗忘账本与数据库完成水位共同决定屏障是否解除。

`daemon status` 的 `reasons` 区分 `manual`、`restore`、`legacy_unknown` 与 `forget_pending/forget_unapplied/forget_ledger_invalid`，并提供 `recovery` 提示。旧版只有 `paused=1` 的记录不能证明暂停来源，升级不会猜测并自动清除；须核实遗忘账本、旧队列、通知和计划后手动 `daemon resume`。

服务、任务、通知分别保留真实状态。登录失效、权限不足和缺工具会让任务失败或受阻；App Server 不可用时通知保持本地待处理，后台任务仍可独立运行。暂停或服务异常可从 `daemon status`、`doctor`、任务及通知查询看到，不会回答“已通知”冒充成功。

参考：[APScheduler 用户指南](https://apscheduler.readthedocs.io/en/3.x/userguide.html)、[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[Codex CLI 参数](https://learn.chatgpt.com/docs/cli/reference)、[websockets 同步客户端](https://websockets.readthedocs.io/en/15.0.1/reference/sync/client.html)。
