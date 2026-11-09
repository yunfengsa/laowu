"""Bounded, ephemeral file parsing. This module also runs as an isolated child.

Bytes travel over pipes only. No document cache, URLs, credentials, macros or
formula evaluation. Resource limits are installed before importing parsers.
"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

MAX_INPUT = 20 * 1024 * 1024
MAX_OUTPUT_IMAGE = 4 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_EDGE = 2560
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_PAGES = 200
MAX_ZIP_ENTRIES = 1024
MAX_ZIP_MEMBER = 16 * 1024 * 1024
MAX_ZIP_TOTAL = 64 * 1024 * 1024
MAX_ZIP_RATIO = 200
MAX_SHEETS = 100
MAX_ROWS = 10000
MAX_COLUMNS = 256
MAX_CELLS = 100000
PARSER_SECONDS = 15
MIMES = {
    'application/pdf': 'pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
}


class Rejected(Exception):
    pass


def parse_file(raw, kind, *, secret_pattern=None):
    """Supervise one short-lived child; fail closed if limits are unavailable."""
    if len(raw) > MAX_INPUT:
        return {'error': 'input_limit: 文件超过20MiB，未解析'}
    from .retries import remaining
    header = json.dumps({'kind': kind, 'secret_pattern': secret_pattern}).encode() + b'\n'
    try:
        child = subprocess.run(
            [sys.executable, '-I', str(Path(__file__).resolve())],
            input=header + raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=min(PARSER_SECONDS, remaining(PARSER_SECONDS)),
            cwd='/', env={'PATH': os.defpath, 'LANG': 'C.UTF-8'}, close_fds=True,
        )
    except subprocess.TimeoutExpired:
        return {'error': 'parser_timeout: 解析超时，未返回部分正文'}
    except OSError:
        return {'error': 'parser_unavailable: 无法启动受限解析进程'}
    if child.returncode or len(child.stdout) > 32 * 1024 * 1024:
        return {'error': 'parser_resource_limit: 解析进程失败或超过CPU/内存/输出限制，未返回部分正文'}
    try:
        result = json.loads(child.stdout)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        return {'error': 'parser_failed: 未取得有效解析结果'}


class Text:
    def __init__(self):
        self.parts = []
        self.size = 0

    def add(self, text):
        self.size += len(text.encode('utf-8'))
        if self.size > MAX_TEXT_BYTES:
            raise Rejected('text_limit: 提取正文超过4MiB；未返回部分正文，请拆分文档')
        self.parts.append(text)

    def value(self):
        return ''.join(self.parts)


def _check_secret(text, pattern):
    if pattern and re.search(pattern, text, re.I):
        raise Rejected('file_secret: 拒绝发送含可识别凭据或私钥的办公文档')


def checked_zip(raw, kind, secret_pattern=None):
    """Validate all members before libraries can expand XML or follow relations."""
    from defusedxml.ElementTree import fromstring
    archive = zipfile.ZipFile(io.BytesIO(raw))
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if len(entries) > MAX_ZIP_ENTRIES or len(names) != len(set(names)):
        raise Rejected('zip_limit: ZIP条目过多或重名')
    if sum(e.file_size for e in entries) > MAX_ZIP_TOTAL:
        raise Rejected('zip_limit: ZIP解压总量超过64MiB')
    required = 'word/document.xml' if kind == 'docx' else 'xl/workbook.xml'
    if '[Content_Types].xml' not in names or required not in names:
        raise Rejected('office_format: DOCX/XLSX包结构不符；不支持旧DOC/XLS')
    cells = 0
    for entry in entries:
        path = PurePosixPath(entry.filename)
        if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                or '\x00' in entry.filename or (entry.external_attr >> 16) & 0o170000 == 0o120000):
            raise Rejected('zip_path: ZIP包含不安全路径或链接')
        if (entry.flag_bits & 1 or entry.file_size > MAX_ZIP_MEMBER
                or entry.file_size > max(1, entry.compress_size) * MAX_ZIP_RATIO
                or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
            raise Rejected('zip_limit: ZIP加密、条目超过16MiB、压缩比超过200或压缩格式不支持')
        name = entry.filename.lower()
        if name.endswith('.bin') or any(p in name for p in ('vbaproject', '/activex/', '/embeddings/')):
            raise Rejected('office_active_content: 不支持宏或嵌入可执行对象')
        with archive.open(entry) as stream:
            data = stream.read(MAX_ZIP_MEMBER + 1)
        if len(data) != entry.file_size or len(data) > MAX_ZIP_MEMBER:
            raise Rejected('zip_limit: ZIP实际解压量不符')
        _check_secret(data.decode('utf-8', errors='replace'), secret_pattern)
        if name.endswith(('.xml', '.rels')):
            # Also rejects UTF-16 DTD/entity attacks before lxml/openpyxl see it.
            root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
            _check_secret(''.join(root.itertext()), secret_pattern)
            for element in root.iter():
                for key, value in element.attrib.items():
                    _check_secret(key + '=' + value, secret_pattern)
            if 'macroenabled' in data.decode('utf-8', errors='replace').lower():
                raise Rejected('office_active_content: 不支持宏文档')
            if name.startswith('xl/worksheets/'):
                row_index = 0
                seen_cells = set()
                for element in root.iter():
                    if element.tag.endswith('}row'):
                        number = int(element.get('r', '0'))
                        if not 1 <= number <= MAX_ROWS:
                            raise Rejected('sheet_limit: 工作表行号超过10000或无效')
                        if number <= row_index:
                            raise Rejected('sheet_format: 工作表行重复或无序，拒绝可能遗漏正文的解析')
                        row_index = number
                    if element.tag.endswith('}c'):
                        cells += 1
                        coordinate = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]*)', element.get('r', ''))
                        if not coordinate:
                            raise Rejected('sheet_format: 单元格坐标无效')
                        column = 0
                        for letter in coordinate[1]:
                            column = column * 26 + ord(letter) - 64
                        if column > MAX_COLUMNS or int(coordinate[2]) > MAX_ROWS or cells > MAX_CELLS:
                            raise Rejected('sheet_limit: 工作表超过10000行/256列或全文件100000单元格')
                        if int(coordinate[2]) != row_index or coordinate[0] in seen_cells:
                            raise Rejected('sheet_format: 单元格重复或与所在行不符，未返回部分正文')
                        seen_cells.add(coordinate[0])
    return archive


def document(raw, kind, secret_pattern=None):
    text = Text()
    details = {'format': kind, 'pagination': 'text_offset/text_version', 'ocr': 'not_performed'}
    if kind == 'pdf':
        from pypdf import PdfReader
        if not raw.startswith(b'%PDF-'):
            raise Rejected('pdf_format: PDF签名不符')
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise Rejected('pdf_encrypted: 不支持加密PDF')
        if len(reader.pages) > MAX_PAGES:
            raise Rejected('pdf_limit: PDF超过200页；请拆分文档')
        empty = []
        for index, page in enumerate(reader.pages, 1):
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > MAX_ZIP_MEMBER:
                raise Rejected('pdf_limit: PDF单页解压内容超过16MiB')
            body = page.extract_text() or ''
            if not body.strip():
                empty.append(index)
            text.add(f'\n[Page {index}]\n{body}\n')
        details.update(pages=len(reader.pages), pages_without_text=empty,
                       note='仅提取PDF文本层；无文本页可能是扫描件或空白页，未做OCR；不读取批注/附件，不执行脚本或链接。')
    else:
        archive = checked_zip(raw, kind, secret_pattern)
        try:
            if kind == 'docx':
                from docx import Document
                doc = Document(io.BytesIO(raw))
                # XML walk retains body table order, hyperlinks and nested tables.
                w = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                def walk(node):
                    if node.tag == w + 't':
                        text.add(node.text or '')
                    elif node.tag == w + 'tab':
                        text.add('\t')
                    elif node.tag in {w + 'br', w + 'cr'}:
                        text.add('\n')
                    else:
                        for child in node:
                            walk(child)
                        if node.tag in {w + 'p', w + 'tr'}:
                            text.add('\n')
                        elif node.tag == w + 'tc':
                            text.add('\t')
                walk(doc.element.body)
                details['note'] = '提取主文档段落/表格/链接显示文字；不含页眉页脚、脚注、批注、修订语义、公式图形和嵌入对象；不做OCR或访问链接。'
            elif kind == 'xlsx':
                from openpyxl import load_workbook
                book = load_workbook(io.BytesIO(raw), read_only=True, data_only=False,
                                     keep_vba=False, keep_links=False)
                try:
                    if len(book.sheetnames) > MAX_SHEETS:
                        raise Rejected('sheet_limit: 工作簿超过100个工作表')
                    if len(book.worksheets) != len(book.sheetnames):
                        raise Rejected('sheet_format: 不支持图表工作表；未返回部分工作簿')
                    count = 0
                    for sheet in book.worksheets:
                        text.add(f'\n[Sheet {json.dumps(sheet.title, ensure_ascii=False)}; {sheet.sheet_state}]\n')
                        # Do not trust declared dimensions (can understate data).
                        sheet.reset_dimensions()
                        for row_num, row in enumerate(sheet.iter_rows(), 1):
                            if row_num > MAX_ROWS or len(row) > MAX_COLUMNS:
                                raise Rejected('sheet_limit: 工作表超过10000行/256列')
                            for cell in row:
                                if cell.value is None:
                                    continue
                                count += 1
                                if count > MAX_CELLS:
                                    raise Rejected('sheet_limit: 工作簿超过100000个单元格')
                                value = cell.value
                                if not isinstance(value, (str, int, float, bool)):
                                    if cell.data_type == 'f':
                                        raise Rejected('sheet_formula: 暂不支持此数组/数据表公式表示')
                                    value = str(value)
                                label = 'formula' if cell.data_type == 'f' else 'value'
                                text.add(f'{cell.coordinate} ({label}): {json.dumps(value, ensure_ascii=False)}\n')
                    details.update(sheets=len(book.worksheets), nonempty_cells=count,
                                   formulas='literal_not_evaluated; cached_values_not_used',
                                   note='读取全部工作表（含隐藏表）的非空单元格及坐标；公式仅显示原文，未计算，不采用可能缺失或过期的缓存；不加载外链、宏、图表、批注或图片。')
                finally:
                    book.close()
            else:
                raise Rejected('unsupported_format: 不支持此文档格式')
        finally:
            archive.close()
    body = text.value()
    _check_secret(body, secret_pattern)
    return {'text': body, 'document': details}


def picture(raw, expected_mime):
    import warnings
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    allowed = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'GIF': 'image/gif', 'WEBP': 'image/webp'}
    with Image.open(io.BytesIO(raw), formats=list(allowed)) as check:
        width, height = check.size
        if width * height > MAX_PIXELS:
            raise Rejected('image_pixels: 图片超过4000万像素')
        if allowed.get(check.format) != expected_mime:
            raise Rejected('image_format: 图片解码格式与MIME不符')
        check.verify()
    with Image.open(io.BytesIO(raw), formats=list(allowed)) as original:
        original.seek(0)
        original.load()  # Fail on truncated/corrupt input, even if the header is valid.
        animated = bool(getattr(original, 'is_animated', False))
        oriented = ImageOps.exif_transpose(original)
        oriented.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
        output = oriented.convert('RGBA' if 'A' in oriented.getbands() or 'transparency' in oriented.info else 'RGB')
        # Fresh encoding strips metadata/trailing payloads. Never echo input base64.
        output.info.clear()
        while True:
            buffer = io.BytesIO()
            output.save(buffer, format='PNG')
            encoded = buffer.getvalue()
            if len(encoded) <= MAX_OUTPUT_IMAGE:
                break
            output = output.resize((max(1, output.width * 3 // 4), max(1, output.height * 3 // 4)), Image.Resampling.LANCZOS)
        return {'image': {'type': 'image', 'mimeType': 'image/png',
                          'data': base64.b64encode(encoded).decode('ascii')},
                'image_info': {'original_width': width, 'original_height': height,
                               'output_width': output.width, 'output_height': output.height,
                               'input_bytes': len(raw), 'output_bytes': len(encoded),
                               'resized': output.size != (width, height), 'reencoded': True,
                               'animated': animated, 'frame': 0,
                               'note': '按EXIF方向解码，最长边2560且编码不超过4MiB；移除元数据；动图仅展示首帧，未做OCR。'}}


def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    except (ImportError, OSError, ValueError):
        print(json.dumps({'error': 'parser_limits_unavailable: 宿主不能设置解析资源限制'}))
        return
    try:
        header = json.loads(sys.stdin.buffer.readline(4096))
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise Rejected('input_limit: 文件超过20MiB')
        kind = header['kind']
        result = (picture(raw, kind) if kind.startswith('image/') else
                  document(raw, kind, header.get('secret_pattern')))
    except ImportError:
        result = {'error': 'parser_dependencies_missing: 请由宿主安装项目 .[slack] 依赖，未解析'}
    except Rejected as exc:
        result = {'error': str(exc)}
    except MemoryError:
        result = {'error': 'parser_resource_limit: 超出解析内存限制，未返回部分正文'}
    except Exception:
        result = {'error': 'invalid_format: 文件损坏、不支持、含不安全XML或超出解析限制，未返回部分正文'}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
