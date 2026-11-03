"""試験用の xlsx 書出し（合成データ専用）。

openpyxl では数式のキャッシュ値（計算結果）を保存できず、コメント・ピボット・外部リンク等の
部品も自由に置けないため、試験に必要な範囲を自前で書く。Excel で開ける形式で出力する。

セルの値:
  str                文字列（共有文字列）
  RichText           書式区間・ふりがな付きの文字列
  InlineText         インライン文字列
  int/float/Decimal  数値
  date               日付（シリアル値＋表示形式 yyyy/mm/dd）
  NumCode            数値＋先頭ゼロの表示形式
  Formula            数式（cached にキャッシュ値。shared=True なら列ごとに共有数式にする）
  bool               真偽
  None               セルなし
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from tests.synthetic.generator import Formula, NumCode

_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
_EPOCH = date(1899, 12, 30)
_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CHART_CT = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml"
_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


@dataclass(frozen=True)
class RichText:
    runs: tuple[tuple[str, bool], ...]  # (文字列, 太字)
    phonetic: str | None = None  # ふりがな（rPh）

    @property
    def text(self) -> str:
        return "".join(t for t, _ in self.runs)


@dataclass(frozen=True)
class InlineText:
    text: str


@dataclass
class Comment:
    ref: str
    author: str
    text: str


@dataclass
class SheetSpec:
    name: str
    rows: list[list[Any]]
    state: str = "visible"
    comments: list[Comment] = field(default_factory=list)
    header_footer: str | None = None
    validation_list: tuple[str, str] | None = None  # (範囲, 一覧の式)
    image: bool = False
    chart_title: str | None = None


@dataclass
class ExtraPart:
    """任意の部品（ピボット・外部リンク・未知の部品等の模擬）。ブックからの関係定義を付ける。"""

    name: str
    content_type: str
    data: bytes
    rel_type: str | None = None


@dataclass
class BookSpec:
    sheets: list[SheetSpec]
    defined_names: list[tuple[str, str]] = field(default_factory=list)
    creator: str = "synthetic"
    company: str | None = None
    extra_parts: list[ExtraPart] = field(default_factory=list)
    thumbnail: bool = False
    abs_path: str | None = None


def col_letter(col: int) -> str:
    out = ""
    n = col + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


class _Styles:
    def __init__(self) -> None:
        self.formats: list[str] = []  # 追加した表示形式（xf 番号 = 位置 + 1）

    def xf(self, fmt: str) -> int:
        if fmt not in self.formats:
            self.formats.append(fmt)
        return self.formats.index(fmt) + 1

    def xml(self) -> str:
        num_fmts = "".join(
            f'<numFmt numFmtId="{164 + i}" formatCode={quoteattr(f)}/>' for i, f in enumerate(self.formats)
        )
        xfs = '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' + "".join(
            f'<xf numFmtId="{164 + i}" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
            for i in range(len(self.formats))
        )
        fmts = f'<numFmts count="{len(self.formats)}">{num_fmts}</numFmts>' if self.formats else ""
        return (
            f'{_HEAD}<styleSheet xmlns="{_MAIN}">{fmts}'
            '<fonts count="2"><font><sz val="11"/><name val="游ゴシック"/><family val="3"/><charset val="128"/></font>'
            '<font><b/><sz val="11"/><name val="游ゴシック"/><family val="3"/><charset val="128"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            f'<cellXfs count="{len(self.formats) + 1}">{xfs}</cellXfs>'
            '<cellStyles count="1"><cellStyle name="標準" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"
        )


class _Strings:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.index: dict[str, int] = {}

    def add(self, value: str | RichText) -> int:
        if isinstance(value, RichText):
            runs = "".join(
                f'<r><rPr>{"<b/>" if bold else ""}<sz val="11"/><rFont val="游ゴシック"/></rPr>'
                f'<t xml:space="preserve">{escape(t)}</t></r>'
                for t, bold in value.runs
            )
            phonetic = (
                f'<rPh sb="0" eb="{len(value.text)}"><t>{escape(value.phonetic)}</t></rPh>'
                '<phoneticPr fontId="0"/>' if value.phonetic else ""
            )
            key = "rich:" + runs + phonetic
            xml = f"<si>{runs}{phonetic}</si>"
        else:
            key = "plain:" + value
            xml = f'<si><t xml:space="preserve">{escape(value)}</t></si>'
        if key not in self.index:
            self.index[key] = len(self.items)
            self.items.append(xml)
        return self.index[key]

    def xml(self, count: int) -> str:
        items = "".join(self.items)
        return f'{_HEAD}<sst xmlns="{_MAIN}" count="{count}" uniqueCount="{len(self.items)}">{items}</sst>'


def _number(v: Any) -> str:
    if isinstance(v, bool):
        raise TypeError
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral() else str(v)
    return repr(v) if isinstance(v, float) else str(v)


def _serial(d: date) -> int:
    return (d - _EPOCH).days


def _sheet_xml(spec: SheetSpec, strings: _Strings, styles: _Styles, counter: list[int],
               rel_ids: dict[str, str]) -> str:
    out = []
    shared: dict[int, tuple[int, int]] = {}  # 列 → (si, 最初の行)
    col_ranges: dict[int, tuple[int, int]] = {}
    for r, row in enumerate(spec.rows):
        for c, v in enumerate(row):
            if isinstance(v, Formula) and v.shared:
                first, last = col_ranges.get(c, (r, r))
                col_ranges[c] = (min(first, r), max(last, r))
    next_si = 0
    for r, row in enumerate(spec.rows):
        cells = []
        for c, v in enumerate(row):
            if v is None:
                continue
            ref = f"{col_letter(c)}{r + 1}"
            if isinstance(v, Formula):
                if v.shared:
                    if c not in shared:
                        shared[c] = (next_si, r)
                        next_si += 1
                        first, last = col_ranges[c]
                        f = (f'<f t="shared" ref="{col_letter(c)}{first + 1}:{col_letter(c)}{last + 1}" '
                             f'si="{shared[c][0]}">{escape(v.text.lstrip("="))}</f>')
                    else:
                        f = f'<f t="shared" si="{shared[c][0]}"/>'
                else:
                    f = f"<f>{escape(v.text.lstrip('='))}</f>"
                cached = v.cached
                if cached is None:
                    cells.append(f'<c r="{ref}">{f}</c>')
                elif isinstance(cached, str):
                    cells.append(f'<c r="{ref}" t="str">{f}<v>{escape(cached)}</v></c>')
                elif isinstance(cached, bool):
                    cells.append(f'<c r="{ref}" t="b">{f}<v>{int(cached)}</v></c>')
                else:
                    cells.append(f'<c r="{ref}">{f}<v>{_number(cached)}</v></c>')
            elif isinstance(v, (str, RichText)):
                counter[0] += 1
                cells.append(f'<c r="{ref}" t="s"><v>{strings.add(v)}</v></c>')
            elif isinstance(v, InlineText):
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(v.text)}</t></is></c>')
            elif isinstance(v, bool):
                cells.append(f'<c r="{ref}" t="b"><v>{int(v)}</v></c>')
            elif isinstance(v, NumCode):
                cells.append(f'<c r="{ref}" s="{styles.xf("0" * v.digits)}"><v>{v.value}</v></c>')
            elif isinstance(v, datetime):
                raise TypeError("datetime is not supported; use date")
            elif isinstance(v, date):
                cells.append(f'<c r="{ref}" s="{styles.xf("yyyy/mm/dd")}"><v>{_serial(v)}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{_number(v)}</v></c>')
        out.append(f'<row r="{r + 1}">{"".join(cells)}</row>')
    width = max((len(r) for r in spec.rows), default=1)
    dim = f"A1:{col_letter(max(width - 1, 0))}{max(len(spec.rows), 1)}"
    extra = ""
    if spec.validation_list:
        sqref, formula = spec.validation_list
        extra += (f'<dataValidations count="1"><dataValidation type="list" allowBlank="1" sqref="{sqref}">'
                  f"<formula1>{escape(formula)}</formula1></dataValidation></dataValidations>")
    extra += '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
    if spec.header_footer:
        extra += f"<headerFooter><oddHeader>{escape(spec.header_footer)}</oddHeader></headerFooter>"
    if "drawing" in rel_ids:
        extra += f'<drawing r:id="{rel_ids["drawing"]}"/>'
    if "vml" in rel_ids:
        extra += f'<legacyDrawing r:id="{rel_ids["vml"]}"/>'
    return (
        f'{_HEAD}<worksheet xmlns="{_MAIN}" xmlns:r="{_REL}">'
        f'<dimension ref="{dim}"/><sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        f'<sheetFormatPr defaultRowHeight="18.75"/><sheetData>{"".join(out)}</sheetData>{extra}</worksheet>'
    )


def _comments_xml(comments: list[Comment]) -> str:
    authors = sorted({c.author for c in comments})
    items = "".join(
        f'<comment ref="{c.ref}" authorId="{authors.index(c.author)}"><text><r><t>{escape(c.text)}</t></r>'
        "</text></comment>"
        for c in comments
    )
    author_xml = "".join(f"<author>{escape(a)}</author>" for a in authors)
    return (f'{_HEAD}<comments xmlns="{_MAIN}"><authors>{author_xml}</authors>'
            f"<commentList>{items}</commentList></comments>")


def _vml_xml(comments: list[Comment]) -> str:
    shapes = []
    for i, c in enumerate(comments):
        col = 0
        letters = "".join(ch for ch in c.ref if ch.isalpha())
        for ch in letters:
            col = col * 26 + ord(ch) - 64
        row = int("".join(ch for ch in c.ref if ch.isdigit())) - 1
        shapes.append(
            f'<v:shape id="_x0000_s{1025 + i}" type="#_x0000_t202" style="position:absolute;margin-left:59.25pt;'
            'margin-top:1.5pt;width:108pt;height:59.25pt;z-index:1;visibility:hidden" fillcolor="#ffffe1" '
            'o:insetmode="auto"><v:fill color2="#ffffe1"/><v:shadow on="t" color="black" obscured="t"/>'
            '<v:path o:connecttype="none"/><v:textbox style="mso-direction-alt:auto"><div style="text-align:left">'
            '</div></v:textbox><x:ClientData ObjectType="Note"><x:MoveWithCells/><x:SizeWithCells/>'
            f"<x:Anchor>1, 15, 0, 2, 3, 15, 4, 2</x:Anchor><x:AutoFill>False</x:AutoFill><x:Row>{row}</x:Row>"
            f"<x:Column>{col - 1}</x:Column></x:ClientData></v:shape>"
        )
    return (
        '<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:x="urn:schemas-microsoft-com:office:excel"><o:shapelayout v:ext="edit"><o:idmap v:ext="edit" '
        'data="1"/></o:shapelayout><v:shapetype id="_x0000_t202" coordsize="21600,21600" o:spt="202" '
        'path="m,l,21600r21600,l21600,xe"><v:stroke joinstyle="miter"/><v:path gradientshapeok="t" '
        f'o:connecttype="rect"/></v:shapetype>{"".join(shapes)}</xml>'
    )


_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fe"
    "a7d6a4a50000000049454e44ae426082"
)


def _drawing_xml(image_rid: str | None, chart_rid: str | None) -> str:
    xdr = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    anchors = []
    if image_rid:
        anchors.append(
            '<xdr:oneCellAnchor><xdr:from><xdr:col>5</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row>'
            '<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:ext cx="952500" cy="952500"/><xdr:pic><xdr:nvPicPr>'
            '<xdr:cNvPr id="2" name="図 1"/><xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr>'
            f'</xdr:nvPicPr><xdr:blipFill><a:blip r:embed="{image_rid}"/><a:stretch><a:fillRect/></a:stretch>'
            '</xdr:blipFill><xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic>'
            "<xdr:clientData/></xdr:oneCellAnchor>"
        )
    if chart_rid:
        anchors.append(
            '<xdr:twoCellAnchor><xdr:from><xdr:col>6</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row>'
            '<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>12</xdr:col><xdr:colOff>0</xdr:colOff>'
            '<xdr:row>15</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to><xdr:graphicFrame macro="">'
            '<xdr:nvGraphicFramePr><xdr:cNvPr id="3" name="グラフ 1"/><xdr:cNvGraphicFramePr/>'
            '</xdr:nvGraphicFramePr><xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm><a:graphic>'
            '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
            '<c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
            f'r:id="{chart_rid}"/></a:graphicData></a:graphic></xdr:graphicFrame><xdr:clientData/>'
            "</xdr:twoCellAnchor>"
        )
    return f'{_HEAD}<xdr:wsDr xmlns:xdr="{xdr}" xmlns:a="{a}" xmlns:r="{_REL}">{"".join(anchors)}</xdr:wsDr>'


def _chart_xml(sheet: str, title: str, categories: list[str], values: list[float]) -> str:
    n = len(categories)
    cats = "".join(f'<c:pt idx="{i}"><c:v>{escape(v)}</c:v></c:pt>' for i, v in enumerate(categories))
    vals = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>' for i, v in enumerate(values))
    q = "'" + sheet.replace("'", "''") + "'"
    return (
        f'{_HEAD}<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        f'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="{_REL}"><c:chart>'
        f'<c:title><c:tx><c:rich><a:bodyPr/><a:p><a:r><a:t>{escape(title)}</a:t></a:r></a:p></c:rich></c:tx>'
        '<c:overlay val="0"/></c:title><c:autoTitleDeleted val="0"/><c:plotArea><c:barChart>'
        '<c:barDir val="col"/><c:grouping val="clustered"/><c:varyColors val="0"/><c:ser><c:idx val="0"/>'
        f'<c:order val="0"/><c:cat><c:strRef><c:f>{escape(q)}!$A$2:$A${n + 1}</c:f><c:strCache>'
        f'<c:ptCount val="{n}"/>{cats}</c:strCache></c:strRef></c:cat><c:val><c:numRef>'
        f'<c:f>{escape(q)}!$B$2:$B${n + 1}</c:f><c:numCache><c:formatCode>General</c:formatCode>'
        f'<c:ptCount val="{n}"/>{vals}</c:numCache></c:numRef></c:val></c:ser><c:axId val="1"/><c:axId val="2"/>'
        '</c:barChart><c:catAx><c:axId val="1"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
        '<c:delete val="0"/><c:axPos val="b"/><c:crossAx val="2"/></c:catAx><c:valAx><c:axId val="2"/>'
        '<c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="0"/><c:axPos val="l"/>'
        '<c:crossAx val="1"/></c:valAx></c:plotArea><c:plotVisOnly val="1"/></c:chart></c:chartSpace>'
    )


def build_xlsx(book: BookSpec) -> bytes:
    styles = _Styles()
    strings = _Strings()
    counter = [0]
    parts: dict[str, bytes] = {}
    overrides: dict[str, str] = {}
    wb_rels: list[tuple[str, str]] = []  # (type, target)

    for i, sheet in enumerate(book.sheets, start=1):
        sheet_rels: list[tuple[str, str, str]] = []  # (id, type, target)
        rel_ids: dict[str, str] = {}
        if sheet.comments:
            parts[f"xl/comments{i}.xml"] = _comments_xml(sheet.comments).encode()
            overrides[f"xl/comments{i}.xml"] = f"{_CT}.comments+xml"
            parts[f"xl/drawings/vmlDrawing{i}.vml"] = _vml_xml(sheet.comments).encode()
            sheet_rels.append(("rId1", f"{_REL}/comments", f"../comments{i}.xml"))
            sheet_rels.append(("rId2", f"{_REL}/vmlDrawing", f"../drawings/vmlDrawing{i}.vml"))
            rel_ids["vml"] = "rId2"
        if sheet.image or sheet.chart_title:
            drawing_rels = []
            image_rid = chart_rid = None
            if sheet.image:
                parts[f"xl/media/image{i}.png"] = _PNG_1PX
                drawing_rels.append(("rId1", f"{_REL}/image", f"../media/image{i}.png"))
                image_rid = "rId1"
            if sheet.chart_title:
                cats = [str(row[0]) for row in sheet.rows[1:]]
                vals = [float(row[1]) for row in sheet.rows[1:]]
                parts[f"xl/charts/chart{i}.xml"] = _chart_xml(sheet.name, sheet.chart_title, cats, vals).encode()
                overrides[f"xl/charts/chart{i}.xml"] = _CHART_CT
                drawing_rels.append(("rId2", f"{_REL}/chart", f"../charts/chart{i}.xml"))
                chart_rid = "rId2"
            parts[f"xl/drawings/drawing{i}.xml"] = _drawing_xml(image_rid, chart_rid).encode()
            overrides[f"xl/drawings/drawing{i}.xml"] = "application/vnd.openxmlformats-officedocument.drawing+xml"
            parts[f"xl/drawings/_rels/drawing{i}.xml.rels"] = _rels_xml(drawing_rels).encode()
            sheet_rels.append(("rId3", f"{_REL}/drawing", f"../drawings/drawing{i}.xml"))
            rel_ids["drawing"] = "rId3"
        parts[f"xl/worksheets/sheet{i}.xml"] = _sheet_xml(sheet, strings, styles, counter, rel_ids).encode()
        overrides[f"xl/worksheets/sheet{i}.xml"] = f"{_CT}.worksheet+xml"
        if sheet_rels:
            parts[f"xl/worksheets/_rels/sheet{i}.xml.rels"] = _rels_xml(sheet_rels).encode()
        wb_rels.append((f"{_REL}/worksheet", f"worksheets/sheet{i}.xml"))

    for extra in book.extra_parts:
        parts[extra.name] = extra.data
        overrides[extra.name] = extra.content_type
        if extra.rel_type:
            wb_rels.append((extra.rel_type, extra.name.removeprefix("xl/")))

    wb_rels.append((f"{_REL}/styles", "styles.xml"))
    parts["xl/styles.xml"] = styles.xml().encode()
    overrides["xl/styles.xml"] = f"{_CT}.styles+xml"
    wb_rels.append((f"{_REL}/sharedStrings", "sharedStrings.xml"))
    parts["xl/sharedStrings.xml"] = strings.xml(counter[0]).encode()
    overrides["xl/sharedStrings.xml"] = f"{_CT}.sharedStrings+xml"

    sheets_xml = "".join(
        f'<sheet name={quoteattr(s.name)} sheetId="{i}"'
        + (f' state="{s.state}"' if s.state != "visible" else "")
        + f' r:id="rId{i}"/>'
        for i, s in enumerate(book.sheets, start=1)
    )
    names = "".join(f"<definedName name={quoteattr(n)}>{escape(ref)}</definedName>" for n, ref in book.defined_names)
    names_xml = f"<definedNames>{names}</definedNames>" if names else ""
    abs_path = (
        '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        '<mc:Choice Requires="x15"><x15ac:absPath xmlns:x15ac="http://schemas.microsoft.com/office/'
        f'spreadsheetml/2010/11/ac" url={quoteattr(book.abs_path)}/></mc:Choice></mc:AlternateContent>'
        if book.abs_path else ""
    )
    parts["xl/workbook.xml"] = (
        f'{_HEAD}<workbook xmlns="{_MAIN}" xmlns:r="{_REL}"><workbookPr defaultThemeVersion="166925"/>{abs_path}'
        f'<bookViews><workbookView xWindow="0" yWindow="0" windowWidth="16000" windowHeight="9000"/></bookViews>'
        f'<sheets>{sheets_xml}</sheets>{names_xml}<calcPr calcId="191029"/></workbook>'
    ).encode()
    overrides["xl/workbook.xml"] = f"{_CT}.sheet.main+xml"
    parts["xl/_rels/workbook.xml.rels"] = _rels_xml(
        [(f"rId{i}", t, target) for i, (t, target) in enumerate(wb_rels, start=1)]
    ).encode()

    company = f"<Company>{escape(book.company)}</Company>" if book.company else ""
    parts["docProps/app.xml"] = (
        f'{_HEAD}<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        f"<Application>Microsoft Excel</Application>{company}</Properties>"
    ).encode()
    overrides["docProps/app.xml"] = "application/vnd.openxmlformats-officedocument.extended-properties+xml"
    parts["docProps/core.xml"] = (
        f'{_HEAD}<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:creator>{escape(book.creator)}</dc:creator><cp:lastModifiedBy>{escape(book.creator)}"
        '</cp:lastModifiedBy><dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:created>'
        "</cp:coreProperties>"
    ).encode()
    overrides["docProps/core.xml"] = "application/vnd.openxmlformats-package.core-properties+xml"
    root_rels = [
        ("rId1", f"{_REL}/officeDocument", "xl/workbook.xml"),
        ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
         "docProps/core.xml"),
        ("rId3", f"{_REL}/extended-properties", "docProps/app.xml"),
    ]
    if book.thumbnail:
        parts["docProps/thumbnail.png"] = _PNG_1PX
        root_rels.append(("rId4", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail",
                          "docProps/thumbnail.png"))
    parts["_rels/.rels"] = _rels_xml(root_rels).encode()

    ct = [
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Default Extension="png" ContentType="image/png"/>',
        '<Default Extension="vml" ContentType="application/vnd.openxmlformats-officedocument.vmlDrawing"/>',
    ] + [f'<Override PartName="/{n}" ContentType="{t}"/>' for n, t in overrides.items()]
    content_types = f'{_HEAD}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">{"".join(ct)}</Types>'

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in [("[Content_Types].xml", content_types.encode())] + sorted(parts.items()):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


def _rels_xml(rels: list[tuple[str, str, str]]) -> str:
    items = "".join(f'<Relationship Id="{i}" Type="{t}" Target={quoteattr(target)}/>' for i, t, target in rels)
    return f'{_HEAD}<Relationships xmlns="{_PKG_REL}">{items}</Relationships>'
