# 发布安全说明

仓库发布程序、稳定角色规则、技能、合成测试和使用说明。本机配置、记忆和运行记录留在各自机器；不要将整个已初始化工作区打包上传。Git 忽略规则只影响未跟踪文件，已经暂存或跟踪的文件仍须审核。

| 文件范围 | 发布策略 |
| --- | --- |
| `laowu/`、`scripts/`、`tests/`、`pyproject.toml` | 源码及合成测试；包括文本 SQL fixture |
| `AGENTS.md`、`friends/`、`.agents/skills/`、公开使用文档 | 稳定规则；更改时避免混入个人经历和本机运行记录 |
| `config/features.example.json` | 五项全 false 的安全示例；需要时复制为本机覆盖文件 |
| `config/runtime-policy.json`、`config/slack-manifest.json` | 默认数值与 OAuth scope 模板，不含身份或凭据 |
| `.codex/`、`config/features.json` | 忽略并保留本地；setup 按当前路径生成 Codex 配置，缺少 features 覆盖时默认关闭 |
| `.laowu/`、凭据目录、`.env`、数据库及 WAL/SHM、日志、备份、密钥、缓存 | 私有或生成内容，不提交；Git ignore 不删除文件，也不改变数据备份规则 |
| 本机验收与实施草稿 | 忽略 `docs/acceptance.md`、`docs/*-acceptance.md`、`docs/client-error-investigation.md`、`docs/*-manifest.json`、`docs/phase1-second-review.md`、`docs/*.patch`、`docs/phase1-skills/`；原文件保留本地 |

`.env.example`、`.env.*.example`、`credentials.example.json` 和 `secrets.example.json` 可以使用占位符，文件名例外不意味着内容可信。私有目录中的 example 仍不发布。测试的虚构凭据不是可用密钥；不按 token 等普通变量名判断泄露，也不因为文件位于 tests 就放行真实密钥。公开报告只记录方法与合成案例，不复制实际聊天、内部任务/运行/会话标识或主机状态。

## 提交前审核

1. 先用 `git status --short --untracked-files=all` 查看候选路径，使用 `git check-ignore -v --no-index 路径` 核对排除规则。审阅新文件；对敏感内容只报告文件、行号和规则，不回显值。
2. 按审核后的明确路径逐项暂存，避免 `git add .`、`git add -A` 或强制加入被忽略的文件。用 `git diff --cached --name-status` 和 `git ls-files` 核对完整拟发布集合。
3. 运行 `python3 scripts/check_publish.py`。脚本只读取 Git 索引中的路径及 blob，不遍历工作区、生产 `.laowu/` 或外部凭据，不生成扫描副本。私有路径先拒绝读取，符号链接/子模块也要求人工处理。它检查高置信凭据签名、私有运行标识、个人主目录路径和二进制状态；输出仅含路径、行号、固定规则。空索引返回 2，发现问题返回 1，无命中返回 0。
4. 扫描通过后，再在本地审阅 `git diff --cached` 并运行 `git diff --cached --check`。扫描规则不能证明绝无秘密：其他凭据格式、未标注的聊天、业务信息和伪装文件仍需人工判断；不要把完整 diff 或扫描命中正文贴到公开报告。
5. 将精确索引导出到空临时目录，例如 `git checkout-index --all --prefix=/tmp/laowu-release-review/`（先选择并创建空目录，目标前缀必须以 `/` 结尾）。在该目录按 README 安装、setup、doctor，并运行 [验证说明](verification.md) 的全量检查；不要从原工作区复制 `.codex/`、features 覆盖、运行状态或虚拟环境。临时检查产物也不提交。
6. 最后核对索引与已验证内容一致，再由仓库维护者提交及推送。后续修改应重新暂存并重查对应内容。此流程不替维护者选择开源许可证。

发现疑似秘密时，暂停发布相关文件，只在本地定位并移出发布集合。若已向外暴露真实密钥，应由凭据所有者撤销或轮换；仅删除 Git 文件不能撤回既有泄露。不要为处理忽略规则删除个人数据或重置运行状态。
