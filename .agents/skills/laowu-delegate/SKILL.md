---
name: laowu-delegate
description: 老五安排与管理专业朋友完成编码、深入谈心或调查任务；用于选择、初始化、启动、恢复、检查和收取朋友工作，普通寒暄无需使用。
---

# 朋友协作

仅供老五主会话使用。朋友工作会话不得继续委派。

1. 调用 `friend_list` 选择适合职责的朋友。默认编码用 `engineer`、深入谈心用 `confidant`、证据调查用 `researcher`。需要多种能力时拆成清楚的任务并整合结果，不让用户承担内部调度。
2. 用 `friend_ensure(spec)` 核对或建立朋友配置，确认角色、技能、MCP 和工具的实际预检结果。已有朋友缺能力时做针对性更新；新职责确实无法覆盖时创建稳定 ID、清晰人格和最小能力集合的新朋友。未通过验证的版本不可冒充可用，运行中的任务保留原配置版本。
3. 调用 `task_start(friend_id, request, workspace, idempotency_key)`。请求包括目的、允许的修改范围、验收要求和必要背景；工作目录必须真实且符合任务。幂等键代表一次逻辑任务，重试沿用它；有意创建新任务才换键。对私密话题只传给有权限的朋友，不能为“多想一想”复制整段聊天给工程师。
4. 使用 `task_status` 或 `task_wait(task_id, timeout<=30)` 查询。读取检查点，按真实进展与用户交流；状态没变不反复播报。运行器最多同时运行两位朋友，同一工作目录的写入任务串行，不另开 CLI 绕过限制。
   用户要求后台继续、完成后通知时，按 `laowu-background` 技能使用 `task_start(background=true)`；此类任务由独立后台服务持有。只向用户确认已安排，完成结果经原会话主动交接。普通同步任务仍在本轮等待并审阅。
5. 需要中断时 `task_cancel`。恢复前核实任务状态、已有产物和外部操作结果；仍有效且已授权的中断、受阻或失败任务用 `task_resume(task_id)` 恢复，已取消任务只在用户重新要求时恢复。`bootstrap` 只恢复状态，不能当作自动重跑请求。任务受阻或失败先明确原因，解决阻塞、修复能力或请求内容后再安排；登录失效或缺少必要权限时不伪装成功。
6. 朋友最终交接必须是无 Markdown 围栏的 JSON 对象：`result` 为字符串，`artifacts`、`validation`、`unfinished` 为字符串数组，`memory_candidates` 为对象数组。`unfinished` 非空表示任务受阻（`blocked`），不能向用户宣称完成。审阅是否真正满足用户意图；把候选记忆按记忆技能确认并保存，再由老五自然地给用户完整回应。

调用 `friend_ensure(spec)` 时，配置使用 `id`、`name`、`description`、`instructions`（完整角色指令）、`skills`（技能名数组）、`mcp_servers`、`can_write`、`tools`、`active`；按工具实际 schema 补齐参数。持久化后的 `friend.json` 使用 `instructions_file` 指向同目录 `AGENTS.md`，不要把文件路径误当成 API 的角色正文。技能引用本项目已有技能目录；工具用 `read/write/shell/search/web/memory/checkpoint` 的语义能力，由客户端适配器验证映射，不直接照搬宿主全部工具。继承模型，不自行指定模型或增加模型服务。

内部朋友不是用户需操作的产品界面；无需常态播报名字。用户问起过程时如实说明。CLI 原生工具日志可能显示这些工作，不承诺隐藏。

## 首批运行约定

展示预算只影响 MCP：preview 不等于完整交接。审阅前用 context_read(ref,cursor) 补齐结果、validation、unfinished 和所需产物；引用失效则重新 task_status。execution status 与 acceptance.state 独立。legacy 永不补登记，含旧任务未来 resume 及幂等命中；新任务有效交接后为 pending，无有效交接为 not_ready。主会话核实最新 run_id、结果及证据后调用 task_review(task_id,run_id,decision,summary,evidence)，decision 为 accepted 或 needs_work。同一提交可幂等重试，冲突或旧 run 被拒绝应重新读取，不能覆盖。completed+needs_work 可以在原授权内显式 task_resume；恢复清当前结果和验收状态，历史保留。accepted 任务不再列为待办。Slack 仅结果授权不能验收、修改任务或读取请求/检查点。

验收补充：summary 和 evidence 各项须非空白，至少提供一条可核实证据；MCP 验收在同事务检查主 manager/session 仍 active。内容真实性和充分性由主会话核实，非空检查不代替独立验收。
