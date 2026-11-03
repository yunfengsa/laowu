# 老五

>为什么有老五，因为我不想用老六（层出不穷的personal agent），只希望借助codex本身即配做我的“朋友”，希望我不会那么快的和老五绝交😃

从这个目录打开 Codex，默认和你交流的是“老五”。老五负责理解你的问题、安排专业朋友、审阅结果，并把值得记住的事情保存下来。

**当前版本 0.3 仅支持 Codex，运行环境为 Linux + Python 3.10 以上。** 使用 SQLite、APScheduler 和 websockets，可选安装官方 Slack SDK 接入 Slack，不需要数据库服务、Docker、额外模型 API 或腾讯云服务。已验证的 Codex 版本为 `0.163.0`。

## 开始使用

先安装 Codex CLI 并完成它自己的登录。项目复用现有登录和默认模型，不另配模型 API Key。首次克隆后，在仓库根目录安装并初始化：

```sh
git clone https://github.com/yunfengsa/laowu.git
cd laowu
python3 -m venv .venv
.venv/bin/python -m pip install -e .
python3 scripts/laowu.py setup
python3 scripts/laowu.py doctor
codex
```

系统需提供 Python 3.10+、venv/ensurepip 和 Git。如果使用 uv，可将创建环境和安装两步替换为 `uv venv .venv --python python3`、`uv pip install --python .venv/bin/python -e .`。项目入口自动使用仓库内的 `.venv`。

`.codex/` 是被 Git 忽略的本机生成配置，`setup` 按当前目录生成 MCP 和 hooks 的绝对路径，并初始化 `.laowu/` 本地状态；移动目录后重新运行 `setup`。它不会安装后台服务或启用 Slack。先检查 `doctor` 的诊断，再启动 Codex；诊断成功不等于真实模型或 Slack 联调成功。

第一次使用时，按 Codex 原生提示信任项目；如提示新 hooks 需要审阅，在 `/hooks` 信任本项目钩子。项目不会替你绕过客户端的信任流程。已经打开的会话可能需要重新连接 MCP 或重开一次，才能使用新配置。

定时、后台任务和主动消息见 [后台使用说明](docs/background.md)。后台服务安装一次，之后可以直接对老五说“每天九点检查……”“这件事后台继续，做好后告诉我”。精确感知终端断线的入口是 `python3 scripts/laowu.py chat`；原生桌面端的在线状态存在宿主限制，详见使用说明。

普通对话保持老五身份。要处理项目本身，直接说：

```text
<root>帮我检查记忆系统，或增加一位擅长写作的朋友。</root>
```

你无需知道朋友的内部 ID，也无需手动启动或关闭他们。

## 老五与朋友

| 角色 | 职责 | 初始能力 |
| --- | --- | --- |
| 老五 | 理解你的意图、维护记忆、安排和审阅工作 | 本地记忆、朋友及任务管理 |
| 阿衡 `engineer` | 编码、调试、测试 | 授权工作目录内读写和执行、检索、任务检查点 |
| 阿宁 `confidant` | 倾听、梳理心事与关系问题 | 相关个人记忆、任务检查点；无 shell |
| 阿知 `researcher` | 调查、资料比较、核验来源 | 阅读、搜索、相关记忆、任务检查点 |

每位朋友都有独立人格、技能清单、MCP 配置和版本。朋友实际运行前会校验 CLI、技能和配置。新版本校验或写入失败时保留旧版本；已经启动的朋友继续使用原版本快照。

主会话委派通过本地运行器启动独立的 `codex --no-daemon exec --ephemeral`，使用原有登录及配置的模型/provider。后台服务只在实际任务到期或有通知要表达时调用模型；空闲心跳不调用模型。默认最多两位朋友同时工作，同一目录或上下级目录的写入任务串行执行。

## 记忆怎样保存

- **稳定规则**：`AGENTS.md`、`docs/persona.md`、`friends/`、`.agents/skills/`。
- **可变状态**：`.laowu/state.sqlite3`，包括结构化记忆、来源、修订、会话摘要、任务检查点、朋友版本和运行状态。
- **隐私范围**：`personal` 供老五及谈心朋友；`shared` 供所有朋友；`friend:<id>` 供指定朋友；`task:<id>` 供绑定该任务的朋友。
- **检索**：SQLite FTS5，中文采用确定性的单字/双字切分，完整短语优先匹配；不依赖 embedding API。

当前模型负责挑出值得记住的内容，本地程序负责保存。没有另一个付费模型在后台提炼聊天；也不会批量复制客户端原始对话。每轮保存短摘要，重要事实与任务关键步骤及时落盘。只有工具提交成功后，老五才会说“记住了”。强制退出前尚未提交的内容无法保证保存。

可以直接说“记住这件事”“我之前说错了，改成……”“忘掉这件事”“你现在记得我什么”。纠正会替换当前记录并使派生旧摘要失效。遗忘会先停止项目中的活动任务，再清除有关记忆、派生记录，以及可能包含它的任务摘要和临时材料；受管旧备份也会清理。

`.laowu/forgotten.json` 只保存被遗忘的记录 ID 和清理事件标识，不保存记忆正文，用于阻止旧快照恢复这些记录。不要单独删掉它。项目外另存的旧备份不受本项目控制；在全新环境恢复时，应使用包含最新遗忘账本的备份。客户端自己的历史、模型服务商记录和你另存的文件不在此处的删除范围内。

`.laowu/` 已排除在 Git 之外。记忆权限由本地 MCP 过滤；这是一组可信的本机朋友，**不是对能够读同一磁盘的进程进行机密文件隔离的系统**。

## 关闭、恢复与失败

每次运行区分永久朋友 ID、任务 ID、临时运行 ID 和宿主会话 ID。普通同步任务仍由主会话管理，连接结束时停止所属进程组。明确的异步任务和定时任务由独立后台服务管理，关闭窗口后继续执行；结果按原会话或最新连接会话规则排队。后台服务退出会清理它的工作进程，重新启动后核对检查点再恢复因服务中断的任务。进程身份包含启动时间，避免把复用的 PID 当成旧朋友。

工作进程组另有独立监管进程，实际 CLI 在进程身份落盘后才启动。支持 pidfd 时优先使用；旧 Linux 或沙箱不提供 pidfd 时，guardian 保留尚未回收的直接子进程证据后清理该组，组内监管进程也可终止自身组，不新增 pidfd 硬性要求。进程归属无法安全确认时保留 `cleanup_pending`，继续占用相应容量和目录互斥，并阻止遗忘或恢复删除清理线索；可用 `tasks diagnose` 核查，不根据数据库中的裸 PID 强行清理。原生 macOS 未在本版支持范围内，Mac 用户需提供 Linux 运行环境。

新会话恢复记忆及检查点，核实真正存活的实例，标记中断工作，再按授权继续。不会自动重跑已取消的任务。涉及外部修改的步骤先检查实际结果，避免重复执行。朋友不得自行启动脱离监管的后台服务。

任务状态包括 `queued`、`running`、`completed`、`blocked`、`failed`、`interrupted`、`cancelled`。有效交接仍有未完成事项时为 `blocked`；缺少登录、权限或工具时不能冒充完成。恢复依据是本项目检查点，不承诺复活旧 Codex 会话的全部上下文。

## 管理命令

```sh
python3 scripts/laowu.py friends list
python3 scripts/laowu.py friends sync
python3 scripts/laowu.py tasks list
python3 scripts/laowu.py tasks show TASK_ID
python3 scripts/laowu.py tasks diagnose TASK_ID
python3 scripts/laowu.py tasks cancel TASK_ID
python3 scripts/laowu.py tasks resume TASK_ID
python3 scripts/laowu.py memory search '关键词'
python3 scripts/laowu.py memory save '一条明确的重要信息' --scope personal
python3 scripts/laowu.py memory save '更正后的信息' --id MEMORY_ID
python3 scripts/laowu.py memory forget MEMORY_ID
python3 scripts/laowu.py backup
```

`tasks diagnose` 只显示任务、运行和会话的脱敏诊断元数据，不读取请求、结果或会话摘要。客户端错误与管理器失效的复验步骤见 [诊断与验证说明](docs/verification.md)。

备份命令返回 `.laowu/backups/` 下的快照路径。快照包含一致性数据库、身份、朋友、技能和项目配置，不包含 Codex 登录凭证或任务工程的源文件；这些源码应另外保管。

恢复前停止后台服务（`python3 scripts/laowu.py daemon stop`）并退出本项目的活动 Codex 会话。将本项目程序代码及依赖准备到目标目录后执行：

```sh
python3 scripts/laowu.py --root /目标/老五 restore /备份/snapshot_xxx.zip
python3 /目标/老五/scripts/laowu.py doctor
```

恢复会先备份目标当前状态，然后恢复快照中的朋友和技能、过滤已遗忘记录、清除旧进程身份，并重建 Codex 配置的绝对路径。项目内部的任务目录及产物路径随根目录迁移；外部工作目录仍指向原位置，实际文件需另外迁移。备份存放在同一磁盘不能抵御整盘损坏。

新备份记录已完成遗忘的水位，覆盖当前全部遗忘要求时会保留清理后新增的任务和计划。缺少水位的旧快照、未完成清理或未覆盖的新遗忘仍会保守清除可能引用旧事实的任务材料和计划定义；不能只因记忆表已无原记录就假定其他材料安全。请用当前版本制作和恢复备份。

恢复后后台保持暂停，核实工作目录和外部操作后，用 `daemon resume` 恢复调度。旧会话在线状态不从备份恢复。

MCP 提供 `bootstrap`、`memory_*`、`friend_*`、`task_*`、`session_checkpoint`、`schedule_*`、`event_emit`、`notification_*`、`background_status`；启用相应功能后还提供 `context_read` 和主会话独立验收入口 `task_review`。朋友仅拥有授权范围内的记忆读取、续读、本任务检查点和限后台任务使用的事件入口。进一步的宿主限制见 [Codex 兼容说明](docs/compatibility.md)。

## Slack 入口

老五可作为独立 Slack Agent，通过 Socket Mode 在私聊和指定频道中对话，复用 Codex、朋友和本地记忆。入口默认关闭，安装可选依赖并配置应用及凭据后启用。认证后的 Slack 主会话可用 `slack_channels`、`slack_history`、`slack_thread` 按需读取授权频道与线程；频道历史不等于完整线程或附件内容。安装及读取边界见 [Slack 使用说明](docs/slack.md)。

## 可选能力

首批能力包含 MCP 展示预算与续读、独立任务验收、进展监测、有限只读重试及脱敏指标，五项默认均关闭。需要时从 [安全示例](config/features.example.json) 复制为本机 `config/features.json`，按需修改布尔开关；该本机覆盖文件不提交。迁移、启用和回退见 [首批能力说明](docs/phase1.md)。

## 验证与发布

```sh
# 覆盖 Slack SDK 的离线测试也需要安装可选依赖
.venv/bin/python -m pip install -e '.[slack]'
.venv/bin/python -m unittest discover -s tests -v
```

测试使用临时目录、合成数据和模拟模型事件，覆盖记忆、遗忘、恢复、并发、进程清理、配置生成、hooks、MCP 及 Slack 权限边界。宿主 Unix socket 和真实 Codex/Slack 验证需要相应运行环境；离线测试通过不代表真实模型调用成功。可复现步骤及限制见 [验证说明](docs/verification.md)，公开与本机文件的划分、提交前检查见 [发布安全说明](docs/publishing.md)。
