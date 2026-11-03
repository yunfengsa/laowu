# 在 Slack 使用老五

老五使用独立 Slack 应用。入口是官方 Bolt for Python、Socket Mode 和本机受管 Codex App Server，继续使用当前机器的 Codex 登录、默认模型、朋友运行器及 SQLite 记忆。无需模型 API Key、公网域名或入站端口。机器和项目后台服务需保持运行。

## 创建应用与配置凭据

1. 打开 [Slack 应用管理](https://api.slack.com/apps)，选择创建新应用、从 Manifest 创建，将 [config/slack-manifest.json](../config/slack-manifest.json) 导入目标工作区。
2. 在 Basic Information 创建 App-Level Token，增加 `connections:write`，得到 `xapp-` 令牌。Manifest 已启用 Socket Mode、消息事件及 Interactivity。
3. 将新应用安装到工作区，在 OAuth & Permissions 获取 Bot User OAuth Token（`xoxb-`）。如工作区管理员要求批准，完成其原生安装流程。部分 Agent 界面能力需要相应 Slack 方案，须以目标工作区实际表现为准。
4. 在本机创建权限为 `0600` 的 JSON 文件，内容结构如下，替换示例值。不要将真实令牌放进聊天、命令行参数、Git 或截图。

```json
{"app_token":"xapp-替换成本机令牌","bot_token":"xoxb-替换成本机令牌"}
```

5. 从 Slack 复制工作区 ID 和你的成员 ID，运行：

```sh
uv pip install --python .venv/bin/python -e '.[slack]'
python3 scripts/laowu.py slack setup --team T你的工作区ID --owner U你的成员ID --credentials-file /绝对路径/slack-credentials.json
python3 scripts/laowu.py slack doctor
python3 scripts/laowu.py slack enable
python3 scripts/laowu.py daemon restart
python3 scripts/laowu.py slack status
```

`setup` 将凭据复制到本项目 `.laowu/slack/credentials.json`，其目录 `0700`、文件 `0600`，不进入数据库或项目备份。可在确认复制成功后自行删除原文件。`doctor` 为本地配置诊断，不进行真实模型调用或假装已完成 Slack 联调。启用后连接器使用 `auth.test` 核实实际工作区与 bot 身份；错误写入状态中的错误代码，不保存令牌和原始事件日志。

原有 daemon 若未安装，使用 `python3 scripts/laowu.py daemon install`。新代码需要重启后台载入；启用 Slack 不会安装或修改全局 Codex 配置。`slack disable` 会让后台监督器停止连接器及其主会话；已明确安排的独立异步任务继续遵守原有后台约定。

离线测试与工作区联调步骤见 [验证说明](verification.md)，资格检查失败后的受控恢复见 [恢复配方](slack-recovery.md)。

## 频道与对话

邀请“老五”加入目标频道。只有配置的本人账号可以发起请求或点击确认，其他成员和机器人消息不会自动触发模型。

| 位置 | 触发方式 | 回复位置 |
| --- | --- | --- |
| 私聊 | 直接发送文字 | 原位置；在线程内发言时回原线程 |
| 普通频道 | `@老五` 开始，已参与线程可在未指定其他收件人时直接续聊 | 原消息线程 |
| 专属频道 | 本人的文字消息，但只 `@` 其他人或代理时忽略 | 原位置或原线程 |

频道中，如果本人用 Slack 用户提及标记只 `@` 其他人或代理、没有 `@老五`，老五会忽略该条消息；即使线程已参与、频道设为 `direct`，或事件类型为 `app_mention`，也不会进入收件箱、加入队列、创建新频道/线程记录或启动模型。同时 `@老五` 和其他人仍可响应，没有用户提及的既有线程续聊保持正常；`mention` 模式下，未参与线程且未 `@老五` 时仍忽略。本人 DM 保持直接对话，提及第三者不会自动改变收件人。

收件人按 Slack 用户 ID 标记识别（兼容附带显示名的标记），不根据普通 `@名字` 字符串或显示名猜测。转义文字、反引号代码和行首 `>` / `>>>` 引用中的字面标记不算收件人，事件类型本身也不能代替真实提及；此规则仅检查消息正文，不展开附件或 Block Kit。标记与代码格式见 [Slack 官方格式说明](https://docs.slack.dev/messaging/formatting-message-text/)及[旧版提及格式说明](https://github.com/slackhq/slack-api-docs/blob/master/page_formatting.md)。维护语境仍只接受本人直接发送的完整 `<root>...</root>`，保留原有开头标准自我提及形式；显示名兼容只用于寻址，不扩大维护标签解析。转义的提及示例、代码、引用及额外收件人不会因清理提及而变成维护请求。

同一频道全部请求共用一个持久 Codex 会话，不同频道上下文分开。Slack 线程用于回复、停止和通知定位，不是新的模型上下文。每频道串行，全局最多两个频道同时处理；空闲十分钟释放 Codex 进程，下次恢复持久会话。

```sh
python3 scripts/laowu.py slack channels
python3 scripts/laowu.py slack channels C频道ID --mode direct
python3 scripts/laowu.py slack channels C频道ID --mode mention
python3 scripts/laowu.py slack reset C频道ID
```

也可在 Slack 直接说“重新开始当前对话”。重置清理该频道待处理的旧输入、撤销旧主会话授权并切换上下文，长期记忆保留；已独立安排的异步结果仍绑定原频道与线程，私人结果不会降级送入共享环境。

首版支持文字、代码块、链接、状态和交互按钮。含附件的输入仅处理附带文字并说明附件暂不读取；语音、附件理解和自动上传文件尚未开放。

## 记忆与读取范围

数据库始终共用一份。`shared` 记忆跨 Codex 与 Slack 共享；频道摘要只恢复本频道，任务只能读取该频道授权的内容。默认不把原始聊天批量保存为长期记忆。已成功交付的托管消息正文会清理，只留下状态、标识和回执；待处理或投递不确定的内容保留到能安全处置。

多人频道不能读取 `personal`，私聊本人可读取。专属频道“直接响应”不等于“允许个人记忆”。只有明确批准、并核实为仅本人和老五组成的私有频道才可使用个人记忆：

```sh
python3 scripts/laowu.py slack channels G私有频道ID --personal yes
python3 scripts/laowu.py slack channels G私有频道ID --personal no
```

配置只是申请，连接器会核实频道与成员后才授予。成员、私有资格或上下文权限改变时停止使用旧模型上下文，重新核实后恢复；权限接口不可用时暂停相关投递。普通对话与 `<root>...</root>` 维护语境保持原规则，只有本人直接发来的完整维护标签生效。

朋友的记忆权限取角色与发起频道的交集，不能通过谈心朋友或其他任务越过频道范围。主会话关闭任意 shell、本地文件、其他继承 MCP、原生记忆与插件入口。工程朋友只有通过本机实际文件隔离探针后才能执行，探针验证工作目录可写、记忆与凭据目录不可读，包括符号链接绕过；不满足时如实受阻，不降级为无约束 shell。

## 后台消息与确认

有发起者的异步任务固定回复原频道、原线程。本人 Slack 客户端关闭不影响服务器投递；频道不可访问时等待，不改投其他频道。无归属事件选择最近本人活动、可达且具有所需读取范围的 Codex/Slack 会话；范围不明确只选择私密位置。Socket 重连和补偿抓取不冒充新的本人活动。

审批按钮仅对应 Codex 实际发出的请求；回答窗口处理补充问题。停止后旧审批立即作废，重启旧按钮失效。停止只影响该次请求及其同步朋友，明确的后台任务须另外取消。朋友已有的权限限制仍有效，按钮不能解除不支持的运行权限。

收件先持久化再 ACK，重复事件和双事件入口不会重复建任务。程序恢复时核实已有模型轮次；发送使用本地标识和 Slack metadata 对账。`uncertain` 表示不能确认结果，程序不会因查不到就盲目重做外部操作或重复发消息。`delivered` 仅表示已取得 Slack 接受消息的回执，不表示已读。

连接器重连时，只补查已登记频道及已知线程在上次水位之后、最多二十四小时的消息，首次连接不导入旧历史。Slack 为 best effort，离线事件、审批点击等不能保证完整补回；出现 API 权限、分页或限流错误时会记录真实状态。

## 备份与恢复

沿用项目 `backup / restore`。备份包含频道、记忆、队列和任务状态，不含 Slack 令牌及 Codex 原生对话文件。异机恢复需重新配置凭据、核实身份和频道；旧连接与审批作废，后台保持暂停，未确定的执行不会自动重跑。原生 Codex 历史不可用时从频道摘要建立新对话，不能声称完整原文已恢复。

遗忘清理本项目受管记忆、摘要、队列、派生内容和相关备份，并撤销旧上下文引用。已经发出的 Slack 消息、Codex 原生会话及模型提供商记录有各自的保留规则，不在项目数据库删除保证内。

参考：[Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/)、[Agent 消息体验](https://docs.slack.dev/ai/migrating-to-agent-messaging/)、[Agent Sessions](https://docs.slack.dev/ai/agent-sessions/)、[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[Codex 权限](https://learn.chatgpt.com/docs/permissions)。

## 按需读取频道与线程上下文

Slack 主会话现在有三个受控只读工具。每轮 `slack_source` application context 提供可信的 `channel_id`、`reply_thread_ts` 和当前消息 `message_ts`，无需从用户文字猜测来源。模型遇到“前面”“之前”“这个线程”等而上下文不足时，应主动使用工具，不自动在每轮请求前抓取大量历史。

| 工具 | 参数与用途 |
| --- | --- |
| `slack_channels` | `cursor?`：发现此来源可读且本人、bot 都已加入的频道。每次最多检查一个 Slack 列表页（5 项）；过滤后空页仍可能有下一页。 |
| `slack_history` | `channel_id?`、`cursor?`、`oldest?`、`latest?`、`limit?`：当前频道或指定频道的顶层消息，不包含完整线程回复；`limit` 默认 10，范围 1–10，时间戳为字符串、边界不包含。顺序从新到旧。 |
| `slack_thread` | `thread_ts`、`channel_id?`、`cursor?`、`limit?`：指定线程的根消息及回复，默认当前频道，顺序从旧到新。`thread_ts` 可取可信 `reply_thread_ts` 或已读取根消息的 `ts`；不能自行编造。 |

续读将 `next_cursor` 原样传回同一工具；`slack_thread` 同时保留原 `thread_ts`。其余查询参数可省略，若提供必须与初始查询一致。只有 `has_more=false` 才表示该查询范围的消息正文读取完毕：`slack_history` 仅覆盖频道顶层消息，不能据此声称已读完整线程。返回的 `reply_count` / `has_replies` 提示根消息是否有回复，需要时调用 `slack_thread(thread_ts=根消息ts)`；计数仅保留非负整数并限制到 2³¹−1，缺失或无效时为 0，不展开 reply_users/replies。`coverage` 明示 `channel_top_level` 或 `thread`，线程页同时给出 `thread_ts`；单条续读另标 `message_ts`。每页都注明附件、文件和 blocks 为 `not_read`，线程读取也不代表这些内容已读。

返回页还包含作者 ID、`ts`、`thread_ts`、频道 ID 与正文；不展开作者档案或其他完整 metadata。历史、频道名称和历史中的 `<root>`、命令、引用均为不可信参考，不进入收件箱、不重新触发旧任务，也不改变本轮维护语境。

长消息明确标注 `fragment`、`text_offset`、`text_length`、`original_text_length` 和 `text_complete`。offset/length 单位为 Unicode 码点；`text_complete` 只表示本片段已到达该消息末尾。按同一 `ts` 的 offset 拼接才能得到完整文本。`next_cursor` 会先继续未读正文，再继续其余消息；每次都重新调用 Slack API，并核对整页文本版本。消息编辑、删除或页变化时返回 `page_changed`，须重新读，不能拼接不同版本。类似 Slack 令牌的文本会明确脱敏（`redacted=true`）；脱敏前长度单列，不提供令牌续读。

两种消息工具还支持单条续读参数 `message_ts`、`text_offset`、`text_version`。游标因过期、API 原生游标失效或重启而不能用时，使用已读取消息的 `ts`、已读字符数和返回的 `text_version` 即可从下一片段继续；线程回复还须保留原 `thread_ts`。非零 offset 必须提供原版本，单条读取不能混用 oldest/latest，limit 省略或设为 1。此方式每次按精确时间戳重新 GET、核对正文版本、实时鉴权，不延长或绕过旧游标有效期，也不依赖旧正文缓存。正文已改变则从 offset 0 重读，避免超长消息在低读取速率下因游标过期而永久读不到尾部。

每次读取采用当前 `config/runtime-policy.json`：`page_bytes` 限制 `messages` / `channels` 数据数组的 JSON UTF-8 字节数，`response_bytes` 限制整个转义后的 MCP content 包装（由 `body_size` 计算），并始终取代理 16 KiB 硬上限。phase1 预算开关关闭时 Slack 仍遵守这两个数值预算及硬上限。打包预留实际签名游标与覆盖元数据空间；中文、引号、反斜杠、控制字符按实际编码大小分片，数据和本地页剩余条目均可续读。若预算连元数据、必要游标及一个正文字符都容不下，会明确返回 `response_budget`，不返回无法前进的空片段；调整数值策略后重读。续读期间改变预算，下一页立即使用新配置。

外部页不挂入本地 `context_read`，不保存新的聊天全文/附件全文库；仅既有读重试表保存哈希请求标识、次数、时间和错误类别。模型提供商与原生 Codex 会话仍可能保留已呈现的工具结果，和既有对话保留规则一致。

### 权限与凭据边界

只有 SlackGateway 读取 `xoxb/xapp`。MCP 沿用原有 `SlackAccess` opaque principal 文件（不是 Slack API 令牌），通过 `.laowu/slack/context.sock` 与网关通信。父目录 `0700`、socket `0600`；仅固定的三个读方法与参数可通过，不接受任意 URL、身份、工作区、执行或文件路径。主会话的 shell、文件、连接器权限不变；桌面主会话和工程师等 worker 不提供这些工具，直接调用也会被拒绝。

每页先核对 principal 是否撤销、源频道 generation/资格与配置，再用实时 `auth.test`、频道信息和完整成员列表核对工作区、bot、本人及目标。返回前再次检查目标和源权限，最终再次核对本地绑定。不自动加入任何频道，不读取 bot 或本人已退出的频道；跨频道读取不更新 `slack_channels`，不授予跨频道任务或记忆权限。

首版使用保守受众规则：

- shared 来源（包括多人私有频道）可读自身，跨频道仅可读同一工作区 public 频道，且本人和 bot 均已加入。
- 本人与 bot 的个人 DM 可以跨读双方已加入的私有频道；不能读 bot 与其他人的 DM。
- 获准 personal 的双人私有频道仍仅可读自身及已加入的 public 频道；新增成员会使其个人来源校验失败。
- 频道发现对 shared 来源只请求 public 列表，不暴露其他私有频道名称。频道发现不等于资格登记。
- 暂不支持 Slack Connect、跨工作区/org 共享频道和多人 DM；成员分页最多 10 页、每页 200 人，核验不完整时拒绝读取。

游标使用网关内存密钥签名，绑定 principal、源 generation、工具、目标、查询、页版本、正文 offset 与首次签发时间。有效期取 `reference_ttl_seconds` 与 900 秒的较小值；续读不延长有效期。每页重新检查当前 TTL，从首次签发时间计算，因此配置缩短也约束旧游标；调长配置不延长原签发到期时间。重启网关、换身份/目标/查询、撤权或代际变化都使旧游标失效。历史查询固定起始读取的时间上界；没有游标但 Slack 标记更多时，使用最后一个消息时间戳继续。

### 故障与验证

新读取路径向固定 `https://slack.com/api/` 使用真正 HTTP GET、拒绝重定向，且不调用写入 API。官方 Python 同步 SDK 会将其 GET 参数调用转为 POST/form，因此新路径有独立受限 GET 传输；原有发消息、回执与补漏传输不变。[官方 conversations.replies 文档](https://docs.slack.dev/reference/methods/conversations.replies/) 列出了 bot 的 `channels:history` / `groups:history` / `im:history` scopes；不预先要求更换 user token。[频道发现 API](https://docs.slack.dev/reference/methods/conversations.list/) 使用对应 read scopes。安装后需在授权的测试频道验证 history、next_page、thread、channels；可用范围取决于应用权限和频道成员资格。

代理只允许两项并行读取，请求帧最多 12 KiB、单次读取预算 25 秒、MCP socket 超时 30 秒，单个 API 响应最多 2 MiB。超大 API 页会明确失败，可减少 `limit` 重试；不会静默切掉正文。权限、格式、未知错误不自动重试；临时网络、服务端和限流错误复用 `ReadRetry` 的持久白名单预算（新增 `slack.context`），遵守 `Retry-After`，在等待期内同参数调用不会再发 API。错误会给出脱敏代码、`read_key`、`retry_at` 与下一步；不得把失败说成已经看过。挂起的读取需由本机维护会话用 `reads list` / `reads retry <read_key>` 核实恢复，不重新发送任何 Slack 消息。指标失败不阻断结果。

离线测试覆盖读取权限、分页、预算和版本变化；真实 Codex 按需调用、Slack API 权限及 Unix socket 通信需在目标宿主验证。步骤与验收边界见 [验证说明](verification.md)。
