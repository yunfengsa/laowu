"""Real, synthetic PDF/OOXML/PNG fixtures; no optional libraries to build them."""
import io
import os
import struct
from xml.sax.saxutils import escape
import zipfile
import zlib


def pdf(pages=('PDF evidence alpha', 'PDF evidence omega')):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>']
    kids = ' '.join(f'{3 + i * 2} 0 R' for i in range(len(pages)))
    objects.append(f'<< /Type /Pages /Count {len(pages)} /Kids [{kids}] >>'.encode())
    font_id = 3 + len(pages) * 2
    for index, text in enumerate(pages):
        stream = ('BT /F1 12 Tf 72 720 Td (' + text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') + ') Tj ET').encode('ascii')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {4 + index * 2} 0 R >>'.encode())
        objects.append(f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n')
    start = len(out)
    out.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        out.extend(f'{offset:010d} 00000 n \n'.encode())
    out.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    return bytes(out)


REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
OFFICE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
CONTENT = 'http://schemas.openxmlformats.org/package/2006/content-types'


def package(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def replace_member(raw, name, body):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = {n: archive.read(n) for n in archive.namelist()}
    entries[name] = body
    return package(entries)


def docx(text='DOCX正文证据 alpha', tail='DOCX表格 omega'):
    return package({
        '[Content_Types].xml': f'<Types xmlns="{CONTENT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        '_rels/.rels': f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="{OFFICE}/officeDocument" Target="word/document.xml"/></Relationships>',
        'word/document.xml': f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>{escape(tail)}</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>',
    })


def xlsx(text='XLSX正文证据 alpha', coordinate='A3', extra=''):
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    return package({
        '[Content_Types].xml': f'<Types xmlns="{CONTENT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="{OFFICE}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': f'<workbook xmlns="{ns}" xmlns:r="{OFFICE}"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/><sheet name="Hidden" sheetId="2" r:id="rId2" state="hidden"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="{OFFICE}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{OFFICE}/worksheet" Target="worksheets/sheet2.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': f'<worksheet xmlns="{ns}"><dimension ref="A1"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>{escape(text)}</t></is></c></row><row r="2"><c r="B2"><v>42</v></c><c r="C2"><f>SUM(B2,1)</f><v>43</v></c></row><row r="3"><c r="{coordinate}" t="inlineStr"><is><t>tail omega</t></is></c></row>{extra}</sheetData></worksheet>',
        'xl/worksheets/sheet2.xml': f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>hidden evidence</t></is></c></row></sheetData></worksheet>',
    })


def png(width=1300, height=1200, *, pixels=True):
    def chunk(name, body):
        return struct.pack('>I', len(body)) + name + body + struct.pack('>I', zlib.crc32(name + body))
    body = b''.join(b'\x00' + os.urandom(width * 3) for _ in range(height)) if pixels else b'\x00'
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(body)) + chunk(b'IEND', b''))
