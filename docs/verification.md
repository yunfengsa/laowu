# 验证与诊断

从 [README](../README.md) 的克隆、依赖安装和 setup 开始。验证记录保存在本地；公开仓库只提供方法和合成测试，不附带实际聊天、内部任务标识、凭据或主机运行记录。

## 离线回归

在仓库根目录运行：

```sh
.venv/bin/python -m pip install -e '.[slack]'
.venv/bin/python -m unittest discover -s tests -v
```

如果环境由 uv 创建且没有 pip，将安装命令替换为 `uv pip install --python .venv/bin/python -e '.[slack]'`；测试命令保持一致。

测试以临时目录、合成 SQLite 数据、模拟模型事件和网络传输为基础；Slack SDK 序列化检查需要可选依赖。缺少 Slack SDK 会跳过相关测试，不能将跳过记作通过。部分测试建立本机 Unix socket，沙箱禁止 bind/connect 时需在允许这些能力的宿主重跑，不放宽正式项目的权限来让测试通过。

按改动可选择较窄的检查：

```sh
.venv/bin/python -m unittest discover -s tests -p test_adapters.py -v
.venv/bin/python -m unittest discover -s tests -p test_store.py -v
.venv/bin/python -m unittest discover -s tests -p test_slack_client.py -v
.venv/bin/python -m unittest discover -s tests -p 'test_phase1*.py' -v
.venv/bin/python -m unittest discover -s tests -p test_publishing.py -v
.venv/bin/python -m unittest discover -s tests -p 'test_p1_*.py' -v
```

`test_p1_*` 覆盖遗忘水位与新旧快照往返、停止时上游和本地取消分别失败、交接解析的有界内存增长，以及 guardian/进程组监管进程被强杀后的清理。进程测试只运行本地 Python 假 CLI，包含忽略 SIGTERM 的工具、身份落盘前禁止 exec、PID 复用，以及真实子进程中模拟 pidfd 缺失的兼容路径；不调用模型或 Slack。无 pidfd 的组清理只允许组内进程操作自身组，或 guardian 持有未回收直接子的证据（使用 WNOWAIT，且无其他 reaper、SIGCHLD 为默认处理）；已回收或身份不符必须拒绝。归属未知的组须保持占位，不以“没有找到组长”冒充已经清理。这里只在 Linux 验证，不能将模拟能力缺失等同真实旧内核或原生 Mac 验证。

`tests/fixtures/phase1_schema2.sql` 是合成旧布局 fixture，必须随源码发布。测试中的虚构令牌和身份用于拒绝/脱敏回归，不能用于认证；不要为复现实验替换成真实凭据。Slack 资格恢复的受测配方见 [slack-recovery.md](slack-recovery.md)。

## 新目录安装与配置

从精确审核的 Git 索引导出到空目录，按 README 安装并执行 setup、doctor；不能把整个工作区或私人状态复制过去。验证 `.codex/` 的 MCP 与 hooks 引用新目录，`.laowu/` 由该安装创建。未复制本机 `config/features.json` 时五项功能应全部关闭；示例文件不会自动启用功能。setup 不安装 daemon，也不启用 Slack。

迁移与备份检查只对合成临时项目进行：连续运行两次 migrate 应保持完整性与幂等性；备份后恢复到另一临时根，检查本机开关、数值策略及生成配置随恢复保留或重建。没有可选配置的旧备份应恢复默认值。现有 `test_store.py` 和 `test_phase1*.py` 覆盖这些分支。

## 客户端错误诊断

`python3 scripts/laowu.py doctor` 检查依赖、存储和客户端能力，不调用模型；socket 权限受限可能显示未知。`python3 scripts/laowu.py tasks diagnose TASK_ID` 只读取脱敏诊断，不恢复任务或再次提交模型。认证、权限、网络及未知错误分类只是线索，不能单凭分类断言根因。

区分临时 error 事件、最终 turn.failed、进程退出与有效交接。恢复前先确认管理器和所属会话有效、旧进程已经结束，并核对外部动作是否已经发生；不能用更换任务标识来绕过去重。诊断输出仍可能包含本机标识，保存为私有记录，报告问题时只摘取必要的错误类别与版本。

## 目标宿主与真实联调

Codex、后台与 Slack 的真实检查由拥有相应授权的本机会话执行；离线模拟通过不等于服务已启用或真实模型成功。

- 运行完整离线套件。真实文件隔离探针由 `LAOWU_RUN_SANDBOX_PROBE=1` 显式开启，使用 `test_slack_permissions.py`；该探针不调用模型，但需宿主 Codex 沙箱能力。受限环境的跳过不证明隔离生效。
- 在新配置和原生信任完成后，确认 MCP 能加载。启用展示预算时，用合成长内容验证 preview、完整分页续读、越权和失效引用；启用任务验收时，核对最新运行的完整交接后由主会话独立审阅。
- 后台功能在测试项目检查原会话归属、断线排队、回执不确定时仅核对、不重复发送。步骤见 [后台说明](background.md)。
- Slack 在专用测试频道核对本人和 bot 成员资格、频道/线程读取与下一页、权限变化后拒绝读取；需要真实模型时用合成消息验证按需调用。真实消息发送另需授权。步骤与范围见 [Slack 说明](slack.md)。

每次记录测试命令、通过/失败/跳过及原因，区分模拟、宿主通信和真实模型层次。原始输出与内部标识不进入公开文档。发布文件的最终审核见 [发布安全说明](publishing.md)。
