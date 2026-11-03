# 首批能力：展示预算、独立验收、规则监测与脱敏指标

五项可选功能默认关闭。源码提供 [features.example.json](../config/features.example.json)，本机覆盖文件 `config/features.json` 不进入 Git；缺失时使用全关闭默认值。数值策略见 [runtime-policy.json](../config/runtime-policy.json)。本说明描述功能与操作流程，验证方法见 [验证说明](verification.md)。

## 模块与数据边界

- `features.py` 集中定义五个布尔开关；配置位置为 `config/features.json`。未提供的键为 false，未知键或非布尔值拒绝。
- `migrations.py` 把记忆 schema 从 1 增量升级至 2。Store 升级前用 SQLite backup 保存一致性 `before_migration_*.sqlite3`，升级事务幂等：版本与 context_epoch 在同一事务提交；schema 已为2时，先只读检查 phase1_layout_revision（当前1）、缺失/空 epoch 和增量表列；需要修补才先备份再原子升级，重复启动不重复修改或备份。补齐旧 read_retries 的 owner/累计计数字段、execution_routes.queued_at 及缺表；queued_at 只从原任务 created_at 回填，无对应任务则保持 NULL。故障全部回滚，未来 schema、布局 revision 或扩展版本拒绝。Store、显式 migrate 与备份导入共用该路径。任务验收、引用/游标、监测、只读重试与数字指标使用独立表。历史运行器 diagnostics 列也纳入该迁移；独立导入的旧运行表另以 runtime_meta/schema_version=1 和迁移前备份登记兼容升级。新库直接建完整表，不裸补列。
- `context.py` 只处理 MCP 展示。Store、Service、CLI、运行器恢复请求及检查点保持完整；不读取宿主原始历史，不创建原聊天副本，不自动摘要或卸载宿主上下文。
- `acceptance.py` 将审阅与执行状态分开；`monitoring.py` 只记录和告警；`retries.py` 只重试明确白名单内的观察；`metrics.py` 只存数字、固定枚举与必要对象标识。

## 展示与续读协议

开关 `mcp_budget=true` 后，常规 MCP 正文为 `{"version":1,"data":...}`。预算按实际 MCP 工具结果正文的紧凑 JSON 序列化 UTF-8 字节计算，包含 content/text 的转义开销：bootstrap ≤32768 字节，其他工具 ≤16384 字节。完整内容可容纳时直接给出。长字符串、超长列表和嵌套对象使用显式 `preview`、`length`、`encoding`、`ref`、`complete=false`；状态、错误、unfinished 等关键字段或关键摘要仍保留。预览不能作为完整交接。

`context_read(ref,cursor?)` 返回 JSON 对象，其中 `data` 是 JSON 文本片段，`offset` 是该文本的字符位置，`length` 是完整 JSON 文本的 UTF-8 字节数。顺序拼接各页 data，最后再 JSON.parse；每页片段的 JSON 编码 ≤8192 字节，整体工具正文仍 ≤16384 字节。`next_cursor=null` 才表示读完。单页不保证是独立完整的被引用对象，但工具响应自身始终是完整 JSON。

引用只含服务器生成的类型化数据库选择器、字段路径、版本指纹和少量固定/脱敏元数据；不接受文件路径。每页验证主会话/worker token、记忆 scope、Slack conversation/generation 和结果授权。任务引用绑定最新 run 与持久结果/检查点版本，流式计数或 heartbeat 变化不会破坏同一结果的续读。记忆修订、遗忘、令牌撤销、运行变化及恢复都拒绝旧引用；重新检索取得新 ref。默认引用与游标到期时间为签发后15分钟，续页不延长。遗忘账本 pending 时，Context 展示、对象读取及所有旧引用一律拒绝；即使文件清理失败导致数据库 epoch 回滚，删除意图仍生效。memory 对象还核对账本中的遗忘 ID。

Slack 的结果授权只返回结果及状态，不包含 request/checkpoint，也不允许 task_review、resume、checkpoint 或 cancel。通知的结果授权绑定原结果指纹和原 run，不能跨恢复读取新交接。朋友只能续读其既有记忆范围和本任务检查点。

## 独立验收

开关 `task_acceptance=true` 后，只在真正插入新任务的同一事务内登记 `not_ready`。所有旧任务、旧任务的后续 resume、幂等命中旧任务始终为 `legacy`。执行 `status` 的原有含义不变。

有效交接产生 `pending`；失败、取消或异常且没有有效交接维持 `not_ready`。主会话先核对完整 result/validation/unfinished 及产物，再调用：

```
task_review(task_id, run_id, decision="accepted"|"needs_work", summary, evidence=["具体核对结果"])
```

summary 和每条 evidence 都须非空白，evidence 至少1条。这里只验证证据确实提供，内容是否充分仍由主会话独立核实。同一事务检查 MCP manager/session 仍 active、任务终态、最新 run、交接版本和授权；运行中、旧 run、版本变化和冲突提交拒绝。完全相同的提交幂等。Slack 必须拥有本频道任务的 require_task 授权，仅结果 grant 不足。审阅记录独立保存，不覆盖执行交接。`completed + needs_work` 允许原授权范围内显式 resume；恢复清当前 result/验收状态，保留历史。accepted 的新任务不再列为未完成；bootstrap 包含 completed/pending 和 needs_work。异步通知区分执行结束、待验收和 accepted/needs_work。

本地维护 CLI 保留无需活动 MCP 会话的显式验收能力，受管 worker 禁用该入口。CLI：`tasks show ID` 保留完整结果；`tasks review TASK RUN accepted SUMMARY --evidence TEXT` 提交审阅。`tasks diagnose ID` 仅返回脱敏诊断，不复制验收 summary/evidence。

## 监测与只读重试

daemon 的 APScheduler 另有独立15秒任务，不经过投递任务或 paused 的早退分支。监测规则数据库写入不等待投递过程持有的 advisory IO lock；SQLite 事务仍与遗忘/恢复串行。所有同步/异步受管 worker、Slack 普通 turn 和通知 turn 都有实例；启动发现包括 uncertain Slack 输入，已有或缺失实例均能覆盖。

heartbeat、client_event、phase 和 progress 分开。流式输出及 updated_at 不算 progress；工具完成按实例/工具 ID 去重，检查点须内容改变，有效交接也记录进展。工具阶段识别工具事件类型及带前缀的名称；并行工具保留尚未结束的长工具阶段，工具 ID 重复事件不延长开始时间。普通 working 默认10分钟无进展告警；所有正在执行的工具按工具开始/最近实际进展的30分钟长操作门限提醒，不能继承工具开始前的空转时间。等待审批/用户按各自阶段默认10分钟分别提醒。原 worker 3600秒硬超时保留。监控和指标写入故障与执行隔离；任务 checkpoint 和 Slack 频道 summary 先提交主事务，再 best_effort 运行独立短观察事务。Slack 进展绑定提交时输入并重新验证频道代际；观察 SQL 回滚不撤销摘要。worker 先清理进程组、流、租约和运行目录，再尽力记录指标，Slack active 清理由独立 finally 保证。规则不自动 resume 或 cancel；既有 daemon 服务中断的 recover_interrupted 规则保留。

告警按实例/类型持久去重，同状态不刷屏；进展恢复、状态变更或结束记录 cleared。Slack 提示直接写既有 outbox，绑定原输入线程和受众/代际；计划任务没有用户输入时只建已完成的路由元数据，不创建模型 turn。paused 时允许投递监测提示，普通模型输入继续暂停。native 目标忙时通知留在原会话队列，空闲后显示；任务查询/通知查询可见。

开关 `read_retries=true` 只允许连接/loaded/state/receipt、Slack 资格读取和回执/补收读取。明确网络、结构化5xx、429 或原稳定 ID 回执暂不可见才进入有限重试。额外最多3次，间隔2、10、30秒；故障窗口至多300秒，Retry-After 超预算直接挂起。预留次数和窗口开始时间在调用前写入，崩溃后的 reading 会消费已预留的机会，耗尽后先提交 suspended 再抛错。重启和并发读取不能重置未成功的窗口。成功后清本窗口 attempts/category，保留 total_attempts；5分钟后的新故障获得新窗口。真实 Slack transport_uncertain 和 Codex connection_lost 只在白名单读取边界归类为网络故障；动作调用仍不重试。未知、权限、配置、额度故障不自动重试。剩余观察预算传入 socket/RPC/HTTP 超时，分页读取不能重新获得完整300秒；到期后不安排新尝试，并拒绝超出窗口的重试结果。只读观察预算不包裹 turn/start、chat.postMessage 或其他写入。

外部写入回执不确定时只按原稳定 ID 核对，禁止重发动作。开关启用后，Slack 写入的确定失败挂起，可能已执行则只进入回执观察。只读观察挂起形成持久告警；关闭开关不会删除计数。手动处理先核对状态/权限和原 ID，不能以换 ID 代替核实。本地主会话可执行 `reads list` 查看 ID、状态、窗口次数、累计次数和恢复次数，再用 `reads retry READ_ID` 显式恢复挂起的白名单核对。该入口在同一事务记录新窗口并递增 resumptions，保留 total_attempts，清对应提醒并让既有队列下次扫描继续。受管 worker 不能调用这个维护入口。旧 `notifications retry` 不会清除只读预算；遇 read_suspended 应使用 reads 入口。

Slack 将未提交 pending 与可能已提交 uncertain 的来源持久保存到 read_sources，在 start_turn/start_event/chat.postMessage 前写 uncertain。blocked 恢复时读取持久来源；缺可靠未提交证据一律 uncertain，不依赖内存 _submitted 或 turn_id 是否存在。native uncertain/submitted 保持原状态，只清核对排期。已经不确定的原动作永不改成 pending 重发；有持久未提交证据的 pending 可以在资格核对成功后进行首次提交。

## 数值策略与重载

`config/runtime-policy.json` 是唯一数值策略文件；不自动创建正式文件，也不改变功能开关。只有布尔值的旧 features.json 继续兼容，五个布尔 DEFAULTS 中没有加入数字。

| 键 | 默认值 | 用途 |
| --- | --- | --- |
| bootstrap_bytes / response_bytes / page_bytes | 32768 / 16384 / 8192 | MCP正文/分页预算 |
| reference_ttl_seconds | 900 | 签发引用的有效期 |
| monitor_scan_seconds | 15 | daemon 独立扫描间隔 |
| working_seconds / tool_seconds / waiting_seconds | 600 / 1800 / 600 | 工作、工具、审批或用户等待提醒 |
| read_extra_attempts | 3 | 每故障窗口额外只读尝试数 |
| read_delays_seconds | [2, 10, 30] | 各次额外尝试间隔 |
| read_window_seconds | 300 | 每次故障窗口上限 |

未知键、布尔值、非有限/非正数拒绝；字节和次数必须为整数。预算满足1024 ≤ page ≤ response ≤ bootstrap ≤1048576，过小以致无法容纳引用元数据时拒绝该展示。额外次数为1至100，间隔列表长度须一致、非递减，间隔总和不超过窗口。文件随项目备份/恢复；缺失回默认。

Context 每次展示/续页读取数值策略，旧引用已经签发的 expires 不延长；监控每次扫描读取提醒阈值；ReadRetry 每次核对读取次数/间隔/窗口上限，但保留已经开始的 started 和累计次数，在途传输保留此次调用的 deadline。`monitor_scan_seconds` 在 daemon 建立 scheduler 时读取，变更须重启 daemon。代码升级本身仍须重连 MCP/重启 daemon 才加载新模块。正式值是否调整、何时重载均由主会话处理。

## 脱敏指标

`metrics=true` 独立采集排队、执行和投递耗时，MCP 正文字节与 preview/page 次数（mcp_budget=false 也采集）；错误回复的采样同样 best_effort，不改变原工具 isError 语义；event/progress/heartbeat/phase 时间保存在脱敏监测行。告警数及额外重试总数来自持久表；额外重试总数使用 total_attempts，不随成功、新窗口或人工恢复清零。只解析宿主真实 usage 的已知数值字段（input/cached input/output/reasoning output/total tokens）；缺失是 SQL NULL，不根据字数推算，不补0，不保存正文、工具输出或推理日志。

`tasks metrics` 返回按类型聚合的数值；`tasks diagnose ID` 返回该任务的执行、验收状态、脱敏监测/告警和指标。Slack `background_status` 只显示当前会话的告警；结果 grant 不取得源任务路由信息。

## 启用步骤

1. 审阅本说明与 [验证说明](verification.md)。已有安装升级前，先核实活动任务及外部操作，按维护流程停止相关服务和会话，并创建一致性备份；源码另行保管。全新安装无需恢复旧快照。
2. 在可运行 Unix socket 的宿主完成离线测试。在临时项目中演练两次 `migrate` 和备份往返，确认幂等性，再处理自己的正式数据。
3. 在仓库根目录运行 `python3 scripts/laowu.py migrate`。该命令只触发先备份的幂等 schema 升级和完整性检查，不启用功能或服务；检查返回的 schema_version 和 integrity，保留迁移前备份。
4. 首次配置可运行 `cp -n config/features.example.json config/features.json`，已有覆盖文件应保留并审阅。按需把五项布尔开关改为 true；关闭某项可独立回退功能，已有验收记录与重试计数不会因此删除。Git 忽略该覆盖文件，项目数据备份仍保留它。
5. 重新连接 MCP。启用对应开关后检查 `context_read`、`task_review` 可用，确认模型能续页并识别 preview。已安装 daemon 的环境需在维护窗口重启才能加载代码或扫描周期变更。
6. 用合成任务验证 pending→accepted、needs_work→resume、legacy 任务不补登记，以及告警和 usage 缺失时的行为。真实模型与 Slack 联调应明确授权并使用测试频道，不通过制造不确定外部写入来反复触发动作。操作记录留在本地，公开文档不记录主机状态或会话标识。

## 回退、恢复与遗忘

功能回退优先关闭相应开关并重连 MCP/重启 daemon，保留 schema v2 和审阅/重试历史。不要让旧代码打开 v2 数据库，也不要用删表/裸降版本号回退。若必须恢复备份，主会话停止活动运行后使用项目 restore；先在临时副本演练，并遵守现存遗忘账本。不能通过恢复源码或库文件覆盖遗忘账本。

v1 备份在临时 SQLite 副本升级至 v2，通过校验后再替换；未来 schema 直接拒绝。restore 生成新的全局引用 epoch，清 refs/cursors、运行监测/工具阶段/只读观察租约和临时计时；保留有效验收历史。memory_forget 另外清验收 summary/evidence、告警、指标与派生缓存。恢复旧备份与当前/备份遗忘账本取并集并清派生材料，不让旧备份复活已经遗忘的内容。

旧备份可能没有 features.json/runtime-policy.json；恢复会移除后加的这两份可选配置，回到默认关闭和默认数值；是否再次启用由主会话核实。升级不等于启用，sent/delivered 也不表示用户已读。


## 代码审查定位

| 范围 | 主要文件 |
| --- | --- |
| 展示预算、ref/游标、MCP/角色工具清单 | `context.py`、`mcp.py`、`service.py`、`adapters.py` |
| 独立验收、完整恢复、迟到交接拒绝 | `acceptance.py`、`runtime.py`、`worker.py`、`background.py` |
| Slack 结果授权版本、范围和通知 | `access.py`、`slack_notifications.py`、`slack_gateway.py` |
| 独立规则扫描、工具阶段与去重 | `monitoring.py`、`daemon.py`、`worker.py`、`slack_gateway.py` |
| 持久只读重试与传输剩余预算 | `retries.py`、`codex_bridge.py`、`slack_codex.py`、`slack_client.py` |
| 脱敏数值/usage、诊断和聚合查询 | `metrics.py`、`runtime.py`、`cli.py` |
| 版本迁移、备份/恢复/遗忘、开关与数值策略 | `migrations.py`、`store.py`、`features.py`、`policy.py` |
| 验证与角色说明 | `tests/test_phase1.py`、`tests/test_phase1_review.py`、`tests/test_phase1_second_review.py`、`tests/test_adapters.py`、根及朋友 `AGENTS.md`、本说明和 `.agents/skills/` |

表中 Python 文件均在 `laowu/`。配置生成与公开文件范围见 [发布安全说明](publishing.md)。
