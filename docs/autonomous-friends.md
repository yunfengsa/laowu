# 自主创建与管理朋友

本机维护者批准模板并启用后，老五在普通 Slack 或本地主会话中先复用合适职责；现有职责不适合时可自行创建永久朋友，继续派发、读取完整交接并审阅，不需要用户逐次确认或使用 root。注册配置只说明角色已保存，不代表真实任务已经完成。网络、额度、权限及依赖故障必须处理原因，不能靠反复创建朋友解决。

## 本机策略

`config/friend-policy.json` 缺失时关闭新变更。公开的 `config/friend-policy.example.json` 将总开关及每个模板的 enabled 都设为 false；其中工具、技能及权限字段是供维护者审阅的能力上限，不因出现在示例中而启用。只在本机维护时复制示例、核实技能已安装并明确批准所需模板。该本机文件被 Git 忽略，发布检查也拒绝它；受控项目备份可包含它。普通 Slack 管理接口不能修改本机策略。

Slack 工程文件权限将本项目的整个 `config` 目录设为只读，以保护策略文件及包含目录的写入、替换、删除和改名；该目录不能用作可写工作目录，其他项目工作目录的普通 `config` 不受此规则限制。此边界依赖 Codex 文件权限执行，发布前须由主会话在真实无模型沙箱核验。策略读取拒绝目录或文件符号链接及非普通文件，文件上限为 16 KiB；技能加载也拒绝选定 SKILL 文件及其项目内祖先的符号链接。

初始模板如下；本机可批准其中的子集，不能扩大内置上限。

| template_id | 技能 | 工具上限 | 写入上限 | personal 上限 |
| --- | --- | --- | --- | --- |
| engineer | engineering | read/write/shell/search/memory/checkpoint | 可批准 | 否 |
| researcher | research | read/search/web/memory/checkpoint | 否 | 否 |
| confidant | listening | memory/checkpoint | 否 | 可批准，仍须入口允许 |

模板必须明确列出已安装技能，不自动安装技能或增加 MCP。动态朋友固定绑定创建时的模板和权限上限；后续策略调整不会给旧朋友提权。总开关关闭禁止新的创建、更新和启停，已登记角色仍可查询、执行；已成功创建的相同幂等重试属于查询。已有角色的可编辑标记表示角色类型允许日常管理，实际变更还需总开关开启。

## 管理接口

接口只供认证主会话，worker 不能查询或管理角色，也不能继续委派。来源由 Service 认证，工具参数不能指定身份、来源、personal、权限上限或外部 MCP。

| API | 参数与结果 |
| --- | --- |
| friend_list | 默认保留数组返回，包含 active、dynamic、version、editable、template_id、needs_definition；include_inactive=false 可过滤停用项。include_templates=true 返回 friends、enabled、templates。bootstrap 也提供精简朋友元数据及模板目录。 |
| friend_get | friend_id；返回公开职责定义、技能、工具、版本、状态及 permission_ceiling，包括停用项。不公开来源频道或外部服务启动配置。 |
| friend_create | 必需 template_id、name、description、instructions、idempotency_key；可选 skills、tools、can_write，不传时取批准模板值。服务端生成 ID。 |
| friend_update | 必需 friend_id、expected_version；只可修改 name、description、instructions、skills、tools、can_write，且不能超过创建时上限。 |
| friend_set_active | friend_id、expected_version、active；停用阻止新派发及 resume，不取消活跃运行。没有物理删除接口。 |
| friend_ensure | 仅本地文件维护；Slack 禁用，不能覆盖动态 ID 或 laowu 保留身份。 |

动态角色完全存入现有 friend_registry/friend_versions；friend_management 保存固定上限、分类及私有来源，friend_creations 仅保存键 hash、规范化请求 hash、ID 与遗忘撤销标记，不保存请求正文。创建、历史及幂等结果在同一事务提交。同键同规范化内容返回原 ID，内容不同报冲突；幂等键在整个本机实例内有效，可跨频道/重启重试。

管理响应与 `context_read` 的角色定义重建使用同一字段白名单，不返回静态角色的 `mcp_servers` 等服务启动配置；内部执行读取保留完整配置。

更新和启停用 expected_version 做 CAS，在同一事务核实认证、角色类型和版本。冲突先重新读取并决定，不盲目覆盖另一频道修改。所有内置及文件维护角色受保护；文件 sync 不能覆盖或提升动态 ID。新朋友只保存通用职责，不将当前请求、聊天或记忆正文自动复制进定义。

## 执行和记忆

运行认领时冻结 spec、角色版本和独立的可信记忆策略；修改只影响之后认领的运行。角色 ID laowu 在注册、派发、适配器和 worker 鉴权中被拒绝，记忆主权限不由 actor 字符串授予。运行仍经过 restrict_friend、频道鉴权和实际工程隔离探针；不能用增加 MCP 绕过。

新谈心朋友的 personal 资格来自创建时服务端模板，工程/研究模板没有此资格。Slack 再与来源频道和当前任务权限取交集：共享入口不读 personal 或其他任务，私人入口可允许谈心朋友读取 personal。搜索与 context_read 续读使用同一路径并重新检查撤权。原生朋友仍按自身、当前任务和原有公共兼容范围读取。MCP 记忆范围不是完整文件隔离；工程执行另须文件隔离探针通过。

初始上下文仍只装载角色、技能、任务请求和检查点，不加载全历史、不自动选材。新工具需要主会话重新加载 MCP 工具列表；仅配置注册成功不得报告实际执行成功。

## 遗忘、迁移和恢复

遗忘采取保守清理：所有动态角色的当前文本、历史版本、runs.spec_json 及运行上下文副本都清空，保留 ID 和不可变上限，停用并标为 needs_definition。完整重新提供 name、description、instructions 后，才可单独启用。旧创建幂等键永久撤销，即使角色后来重新定义也不能用旧创建请求恢复正文。

数据库仍使用 schema 2，布局标识 phase1_layout_revision 升为 2；迁移前备份，重复启动幂等，未来 schema/布局被拒绝。旧三角色及文件角色迁移为 protected static。受控恢复继续遵守遗忘账本及完成水位：旧快照不能复活已清理定义；完成遗忘后新创建角色的新备份可保留新正文。恢复还合并本机 SQLite 已撤销的创建键；即使快照早于角色创建，也保留清空后的角色标识而不复制后来的正文。不能丢弃遗忘账本来恢复旧副本。

自动化测试使用临时数据库、合成 Slack 凭证及假客户端，不调用真实模型。真实宿主隔离探针、MCP/Slack 模型任务和本机迁移启用由维护主会话独立验证。

恢复不含 `config/friend-policy.json` 的旧备份时，会移除当前策略文件，使自主管理按缺失策略的默认值关闭；遗忘水位及角色撤销合并规则保持有效。
