# Slack文件解析与发送验收

能力和限制以 [Slack使用说明](slack.md#受控文件收发) 为准。只在已安装 `.[slack]` 的项目虚拟环境验证；此处的测试全部使用临时工作区、合成文件、模拟Slack API，不读取真实凭据，不外发Slack，不操作服务。

```sh
uv pip install --python .venv/bin/python -e '.[slack]'
.venv/bin/python -m unittest discover -s tests -p 'test_slack_file*.py' -v
.venv/bin/python -m unittest discover -s tests -q
```

`tests/slack_file_fixtures.py` 以标准库构建带真实文本内容及xref的PDF、真实OOXML包DOCX/XLSX、超过4MiB的随机RGB PNG和超像素拒绝样本。依赖不是为了模拟解析结果；生产解析子进程实际使用Pillow、pypdf、python-docx、openpyxl和defusedxml，固定版本见 `pyproject.toml`。

文件专项检查以下行为：

- 与真实Slack兼容的 `mode=snippet/file_access=visible/is_external=false` 160字节文本可以读取；外部/远程文件仍拒绝。
- PDF含可核对正文、页码及无文本页标识，DOCX正文与表格保留，XLSX包括隐藏表、单元格坐标和公式原文；伪报工作表数据范围不能遗漏单元格。
- 三种文档均按 `text_offset/text_version` 完整续读；解析版本改变拒绝旧offset。扫描件不做OCR，公式不计算，也不冒充缓存值已计算。
- 大于4MiB及20MiB边界原图解码后返回真实image块和尺寸；输出bytes不超过4MiB，MCP总响应不超过6MiB；异常图像、超过4000万像素拒绝。
- 超大输入、PDF页数、文档提取文字、ZIP条目/单条/总解压量/压缩比、XLSX坐标限额拒绝；重复ZIP条目、路径穿越、DTD/实体（含UTF-16）、宏及损坏文件拒绝。外部关系不访问，外部公式只显示原文。
- DOCX/XLSX发送保留原bytes，但会检查压缩包内可识别凭据，包括UTF-16属性和跨文本run的令牌；同来源最终交接授权、路径防泄漏、跨来源拒绝保持。
- completed和blocked完整有效JSON最终交接登记产物可发送；running/queued/failed、无有效交接、仅checkpoint、活动或待清理run仍拒绝。9.6MB合成PNG证明blocked交付不需伪造任务状态；恢复任务会中止后续发送阶段。
- `files.getUploadURLExternal` 使用POST form，`files.completeUploadExternal` 使用JSON；持久幂等、重启、不确定状态、撤权及跨来源保护由原有文件测试继续覆盖。

`test_real_proxy_and_stdio_pdf_docx_xlsx_and_large_image` 启动真实Unix socket代理和独立MCP stdio进程，逐一核对三种文档正文和大图image块。受管沙箱禁止bind时此项明确跳过，须在宿主补跑。`test_real_stdio_documents_without_socket_transport` 和原图片stdio测试仍执行真实JSON-RPC、Service授权及MCP呈现，仅替换本地代理传输，不能替代前一项宿主通信验收。缺依赖的测试也明确跳过，不记作通过。

主会话还须独立核验真实Slack文件的有效scope、分享权限、snippet/文档/大图下载、真实模型看图、授权产物上传及原线程回执，以及不确定发送状态查询；测试通过不表示这些外部操作已执行。不要为验收更改旧任务状态或使用新幂等键盲目重传。

设计依据：[Slack分配上传URL的form要求](https://docs.slack.dev/reference/methods/files.getUploadURLExternal/)、[pypdf文本提取及内存/OCR边界](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[openpyxl公式缓存和外链参数](https://openpyxl.readthedocs.io/en/stable/api/openpyxl.reader.excel.html)。
