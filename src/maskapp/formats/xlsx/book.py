"""xlsx のブック（シート・セル・共有文字列）の読込と、部品単位の書込（設計書 06 章 2〜4）。

読込:
  セルは「照合用の文字列」（text）を持つ。文字列セルは文字列、数値セルは格納値の文字列。
  ただし表示形式が "0000" のような先頭ゼロ埋めなら、表示どおり（12 → "0012"）にする。
  CSV では同じコードが文字列 "0012" で現れるため、表示どおりにしないと照合できない。
  数式セルはキャッシュ値（最後に計算した結果）を text にする。

書込（render）:
  - 変更するセルだけ <c> 要素を書き換える。数式は残し、キャッシュ値を置き換える。
  - 共有文字列表は全体を作り直す（どのセルからも参照されない元の文字列を残さない）。
  - 文書プロパティは空に初期化し、サムネイル・ユーザー定義プロパティは除く。
  - ヘッダー・フッター、グラフの文字（系列名・項目名のキャッシュ、タイトル）は text_nodes で
    取り出した要素の文字を置き換える（要素の順番で指定する）。
  - 書き換えない部品は元のバイト列のまま格納する。
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field

from lxml import etree

from maskapp.formats.xlsx.package import (
    MAIN_NS,
    OFFICE_REL,
    STRICT_MAIN_NS,
    Package,
    XlsxFormatError,
    parse_xml,
    serialize_xml,
)

_M = f"{{{MAIN_NS}}}"
_R_ID = f"{{{OFFICE_REL}}}id"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_ABS_PATH = "{http://schemas.microsoft.com/office/spreadsheetml/2010/11/ac}absPath"
_CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_HEADER_FOOTER = ("oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter")
_STRING_CACHES = ("strCache", "strLit", "lvl")
_SST_TYPE = f"{OFFICE_REL}/sharedStrings"
_SST_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"

_CELL_REF = re.compile(r"^([A-Z]{1,3})(\d+)$")
_ESCAPED = re.compile(r"_x([0-9A-Fa-f]{4})_")
_NEEDS_ESCAPE = re.compile(r"_x[0-9A-Fa-f]{4}_|[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ZERO_PAD = re.compile(r'^"?(0+)"?$')
_INTEGER = re.compile(r"^-?\d+$")
_MAX_EXCEL_DIGITS = 15

_CORE_EMPTY = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    b'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    b'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
    b'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>'
)
_APP_EMPTY = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
    b'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
    b"<Application>Microsoft Excel</Application></Properties>"
)

# 既定の表示形式（番号 → 書式）のうち、先頭ゼロ埋めの判定に使うもの
_BUILTIN_FORMATS = {0: "General", 1: "0", 49: "@"}


def column_letter(col: int) -> str:
    out = ""
    n = col + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def cell_ref(row: int, col: int) -> str:
    return f"{column_letter(col)}{row + 1}"


def parse_ref(ref: str) -> tuple[int, int]:
    m = _CELL_REF.match(ref.replace("$", ""))
    if not m:
        raise XlsxFormatError("bad_cell_reference")
    col = 0
    for ch in m.group(1):
        col = col * 26 + (ord(ch) - 64)
    return int(m.group(2)) - 1, col - 1


def decode_text(text: str) -> str:
    return _ESCAPED.sub(lambda m: chr(int(m.group(1), 16)), text)


def encode_text(text: str) -> str:
    return _NEEDS_ESCAPE.sub(
        lambda m: "_x005F" + m.group(0) if m.group(0).startswith("_x") else f"_x{ord(m.group(0)):04X}_", text
    )


def _rich_text(el: etree._Element) -> tuple[str, bool, bool]:
    """<si> / <is> の文字列。(文字列, 書式区間があるか, ふりがなを含むか)。"""
    parts: list[str] = []
    rich = False
    phonetic = False
    for child in el:
        tag = etree.QName(child).localname
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "r":
            rich = True
            t = child.find(f"{_M}t")
            if t is not None:
                parts.append(t.text or "")
        elif tag == "rPh":
            phonetic = True
    return decode_text("".join(parts)), rich, phonetic


@dataclass
class FormulaCell:
    text: str | None  # 共有数式の子セルは None（親セルの式を使う）
    kind: str  # normal / shared / array / dataTable
    ref: str | None
    si: str | None


@dataclass
class Cell:
    row: int
    col: int
    kind: str  # string / number / bool / error / date / empty
    text: str
    style: int
    formula: FormulaCell | None
    rich: bool = False
    phonetic: bool = False
    t: str | None = None

    @property
    def ref(self) -> str:
        return cell_ref(self.row, self.col)


@dataclass
class Sheet:
    idx: int
    name: str
    state: str  # visible / hidden / veryHidden
    part: str
    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)

    def grid(self) -> list[list[str]]:
        """照合用の表（行・列とも 0 起点、A 列から）。空のセルは ""。"""
        if not self.cells:
            return []
        max_row = max(r for r, _ in self.cells)
        rows: list[list[str]] = [[] for _ in range(max_row + 1)]
        for (r, c), cell in sorted(self.cells.items()):
            if not cell.text:
                continue
            row = rows[r]
            if len(row) <= c:
                row.extend([""] * (c + 1 - len(row)))
            row[c] = cell.text
        return rows

    def shared_formula_text(self, si: str) -> str | None:
        for cell in self.cells.values():
            f = cell.formula
            if f is not None and f.kind == "shared" and f.si == si and f.text is not None:
                return f.text
        return None


@dataclass
class _Styles:
    formats: list[str]  # セル書式番号 → 表示形式

    def zero_pad_width(self, style: int) -> int | None:
        if 0 <= style < len(self.formats):
            m = _ZERO_PAD.match(self.formats[style])
            if m:
                return len(m.group(1))
        return None


def _read_styles(pkg: Package, workbook_part: str) -> _Styles:
    parts = pkg.related(workbook_part, "styles")
    if not parts or parts[0] not in pkg.data:
        return _Styles([])
    root = parse_xml(pkg.data[parts[0]])
    custom = {
        int(el.get("numFmtId", "-1")): el.get("formatCode", "")
        for el in root.iter(f"{_M}numFmt")
    }
    formats = []
    xfs = root.find(f"{_M}cellXfs")
    for xf in xfs if xfs is not None else []:
        fid = int(xf.get("numFmtId", "0"))
        formats.append(custom.get(fid, _BUILTIN_FORMATS.get(fid, f"builtin:{fid}")))
    return _Styles(formats)


@dataclass
class XlsxBook:
    raw: bytes
    pkg: Package
    workbook_part: str
    sheets: list[Sheet]
    sst_part: str | None
    sst: list[tuple[str, bool, bool]]  # (文字列, 書式区間, ふりがな)
    styles: _Styles
    defined_names: list[str]

    @classmethod
    def parse(cls, raw: bytes) -> XlsxBook:
        pkg = Package.read(raw)
        workbook_part = pkg.main_part()
        if workbook_part not in pkg.data:
            raise XlsxFormatError("missing_workbook")
        wb_root = parse_xml(pkg.data[workbook_part])
        ns = etree.QName(wb_root).namespace
        if ns == STRICT_MAIN_NS:
            raise XlsxFormatError("strict_ooxml")
        if ns != MAIN_NS:
            raise XlsxFormatError("not_a_workbook")
        rels = {r.id: r for r in pkg.relationships(workbook_part)}

        sst_part = next(iter(pkg.related(workbook_part, "sharedStrings")), None)
        sst: list[tuple[str, bool, bool]] = []
        if sst_part and sst_part in pkg.data:
            sst = [_rich_text(si) for si in parse_xml(pkg.data[sst_part]).iter(f"{_M}si")]
        styles = _read_styles(pkg, workbook_part)

        sheets: list[Sheet] = []
        sheets_el = wb_root.find(f"{_M}sheets")
        for idx, el in enumerate(sheets_el if sheets_el is not None else []):
            rel = rels.get(el.get(_R_ID, ""))
            if rel is None or rel.external:
                raise XlsxFormatError("broken_sheet_reference")
            sheet = Sheet(idx, el.get("name", ""), el.get("state", "visible"), rel.target)
            if rel.kind == "worksheet":
                if rel.target not in pkg.data:
                    raise XlsxFormatError("broken_sheet_reference")
                _read_cells(sheet, parse_xml(pkg.data[rel.target]), sst, styles)
            sheets.append(sheet)
        names_el = wb_root.find(f"{_M}definedNames")
        defined = [el.get("name", "") for el in names_el] if names_el is not None else []
        return cls(raw, pkg, workbook_part, sheets, sst_part, sst, styles, defined)

    def sheet(self, idx: int) -> Sheet:
        return self.sheets[idx]


def _read_cells(sheet: Sheet, root: etree._Element, sst: list, styles: _Styles) -> None:
    data = root.find(f"{_M}sheetData")
    if data is None:
        return
    r = -1
    for row_el in data.iter(f"{_M}row"):
        r = int(row_el.get("r")) - 1 if row_el.get("r") else r + 1
        c = -1
        for el in row_el.iter(f"{_M}c"):
            ref = el.get("r")
            if ref:
                rr, c = parse_ref(ref)
                if rr != r:
                    raise XlsxFormatError("bad_cell_reference")
            else:
                c += 1
            sheet.cells[(r, c)] = _read_cell(el, r, c, sst, styles)


def _read_cell(el: etree._Element, r: int, c: int, sst: list, styles: _Styles) -> Cell:
    t = el.get("t")
    style = int(el.get("s", "0"))
    v_el = el.find(f"{_M}v")
    v = v_el.text if v_el is not None and v_el.text is not None else None
    f_el = el.find(f"{_M}f")
    formula = None
    if f_el is not None:
        formula = FormulaCell(
            f_el.text if f_el.text else (None if f_el.get("t") == "shared" else ""),
            f_el.get("t", "normal"), f_el.get("ref"), f_el.get("si"),
        )
    rich = phonetic = False
    if t == "s":
        if v is None:
            raise XlsxFormatError("bad_shared_string_index")
        try:
            text, rich, phonetic = sst[int(v)]
        except (ValueError, IndexError):
            raise XlsxFormatError("bad_shared_string_index") from None
        kind = "string"
    elif t == "inlineStr":
        is_el = el.find(f"{_M}is")
        text, rich, phonetic = _rich_text(is_el) if is_el is not None else ("", False, False)
        kind = "string"
    elif t == "str":
        text, kind = decode_text(v or ""), "string"
    elif t == "b":
        text, kind = ("TRUE" if v == "1" else "FALSE") if v is not None else "", "bool"
    elif t == "e":
        text, kind = v or "", "error"
    elif t == "d":
        text, kind = v or "", "date"
    else:
        text, kind = v or "", "number"
        width = styles.zero_pad_width(style)
        if v is not None and width and _INTEGER.match(v) and not v.startswith("-"):
            text = v.zfill(width)
    if v is None and t not in ("s", "inlineStr"):
        kind = "empty"
    return Cell(r, c, kind, text, style, formula, rich, phonetic, t)


# ---------------------------------------------------------------- 書込


@dataclass
class CellEdit:
    """1セルへの変更。value は新しいセル値（数式セルではキャッシュ値）。"""

    value: str | None = None
    clear: bool = False
    formula: str | None = None  # 新しい数式文字列（文字列定数を置換したもの）
    drop_cache: bool = False
    to_value: bool = False


@dataclass
class BookEdits:
    cells: dict[tuple[int, int, int], CellEdit] = field(default_factory=dict)  # (シート, 行, 列)
    remove_comments: bool = False
    remove_charts: bool = False
    full_calc_on_load: bool = False
    drop_calc_chain: bool = False
    # 部品の文字の置換：部品名 → {text_nodes の順番: 新しい文字}
    part_texts: dict[str, dict[int, str]] = field(default_factory=dict)

    def edit(self, sheet: int, row: int, col: int) -> CellEdit:
        return self.cells.setdefault((sheet, row, col), CellEdit())


def text_part_kind(book: XlsxBook, name: str) -> str | None:
    """文字を置換できる部品の種類："worksheet"（ヘッダー・フッター）、"chart"（グラフ）。"""
    if name in {s.part for s in book.sheets}:
        return "worksheet"
    if "drawingml.chart+xml" in book.pkg.content_type(name):
        return "chart"
    return None


def text_nodes(root: etree._Element, kind: str) -> list[etree._Element]:
    """置換の対象にする文字の要素（文書順）。"""
    if kind == "worksheet":
        hf = root.find(f"{_M}headerFooter")
        if hf is None:
            return []
        return [el for el in hf if isinstance(el.tag, str) and etree.QName(el).localname in _HEADER_FOOTER]
    nodes = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        q = etree.QName(el)
        if q.namespace == _CHART_NS and q.localname == "v":
            pt = el.getparent()
            cache = pt.getparent() if pt is not None else None
            if (pt is not None and etree.QName(pt).localname == "pt" and cache is not None
                    and etree.QName(cache).localname in _STRING_CACHES):
                nodes.append(el)
        elif q.namespace == _DRAWING_NS and q.localname == "t":
            nodes.append(el)
    return nodes


def _apply_part_texts(root: etree._Element, kind: str, texts: dict[int, str]) -> None:
    nodes = text_nodes(root, kind)
    for i, text in texts.items():
        nodes[i].text = text


def _child(el: etree._Element, local: str) -> etree._Element | None:
    return el.find(f"{_M}{local}")


def _remove_child(el: etree._Element, local: str) -> None:
    child = _child(el, local)
    if child is not None:
        el.remove(child)


def _set_v(el: etree._Element, text: str) -> None:
    v = _child(el, "v")
    if v is None:
        v = etree.Element(f"{_M}v")
        # <c> の子の順序は f, v, is, extLst
        f = _child(el, "f")
        if f is not None:
            f.addnext(v)
        else:
            el.insert(0, v)
    v.text = text


def _number_text(cell: Cell, value: str, styles: _Styles) -> str | None:
    """数値セルのまま書けるなら格納値を返す（表示形式で同じ表示になる場合だけ）。"""
    if cell.kind != "number" or not value.isdigit() or len(value) > _MAX_EXCEL_DIGITS:
        return None
    stored = str(int(value))
    width = styles.zero_pad_width(cell.style)
    shown = stored.zfill(width) if width else stored
    return stored if shown == value else None


def _apply_edit(el: etree._Element, cell: Cell, edit: CellEdit, styles: _Styles,
                new_strings: dict[etree._Element, str]) -> None:
    if edit.clear:
        for local in ("f", "v", "is"):
            _remove_child(el, local)
        for attr in ("t", "cm", "vm"):
            el.attrib.pop(attr, None)
        return
    has_formula = cell.formula is not None and not edit.to_value
    if edit.to_value:
        _remove_child(el, "f")
    if edit.formula is not None and has_formula:
        f = _child(el, "f")
        f.text = edit.formula
    if edit.drop_cache and has_formula:
        _remove_child(el, "v")
        el.attrib.pop("t", None)
    value = edit.value
    if value is None and edit.to_value and cell.t == "str":
        value = cell.text  # 数式の文字列結果は、値にすると共有文字列になる
    if value is None:
        return
    number = _number_text(cell, value, styles)
    if has_formula:
        if number is not None:
            el.attrib.pop("t", None)
            _set_v(el, number)
        else:
            el.set("t", "str")
            _set_v(el, encode_text(value))
        return
    _remove_child(el, "is")
    if number is not None:
        el.attrib.pop("t", None)
        _set_v(el, number)
    elif value == "":
        _remove_child(el, "v")
        el.attrib.pop("t", None)
    else:
        el.set("t", "s")
        _set_v(el, "")
        new_strings[el] = value


def _new_si(text: str, first_run_props: etree._Element | None) -> etree._Element:
    si = etree.Element(f"{_M}si")
    target = si
    if first_run_props is not None:
        target = etree.SubElement(si, f"{_M}r")
        target.append(first_run_props)
    t = etree.SubElement(target, f"{_M}t")
    t.text = encode_text(text)
    if text != text.strip():
        t.set(_XML_SPACE, "preserve")
    return si


def _first_run_props(si: etree._Element | None) -> etree._Element | None:
    if si is None:
        return None
    r = si.find(f"{_M}r")
    if r is None:
        return None
    props = r.find(f"{_M}rPr")
    return deepcopy(props) if props is not None else None


def _rebuild_shared_strings(pkg: Package, book: XlsxBook, trees: dict[str, etree._Element],
                            new_strings: dict[etree._Element, str], original_si: list[etree._Element],
                            inline_rich: dict[etree._Element, etree._Element | None]) -> None:
    entries: list[etree._Element] = []
    index: dict[tuple, int] = {}
    total = 0
    for sheet in book.sheets:
        root = trees.get(sheet.part)
        if root is None:
            continue
        data = root.find(f"{_M}sheetData")
        if data is None:
            continue
        for el in data.iter(f"{_M}c"):
            if el.get("t") != "s":
                continue
            total += 1
            v = _child(el, "v")
            if el in new_strings:
                text = new_strings[el]
                old = inline_rich.get(el)
                key: tuple = ("new", text, old is not None)
                if key not in index:
                    index[key] = len(entries)
                    entries.append(_new_si(text, old))
            else:
                old_idx = int(v.text)
                key = ("old", old_idx)
                if key not in index:
                    index[key] = len(entries)
                    entries.append(deepcopy(original_si[old_idx]))
            v.text = str(index[key])
    if book.sst_part is None and not entries:
        return
    root = etree.Element(f"{_M}sst", nsmap={None: MAIN_NS})
    root.set("count", str(total))
    root.set("uniqueCount", str(len(entries)))
    for si in entries:
        root.append(si)
    if book.sst_part is None:
        part = "xl/sharedStrings.xml"
        pkg.put(part, serialize_xml(root), _SST_CT)
        pkg.add_relationship(book.workbook_part, _SST_TYPE, part)
    else:
        pkg.put(book.sst_part, serialize_xml(root))


def _set_full_calc(wb_root: etree._Element) -> None:
    calc = wb_root.find(f"{_M}calcPr")
    if calc is None:
        calc = etree.Element(f"{_M}calcPr")
        anchor = None
        for local in ("sheets", "functionGroups", "externalReferences", "definedNames"):
            found = wb_root.find(f"{_M}{local}")
            if found is not None:
                anchor = found
        if anchor is None:
            wb_root.append(calc)
        else:
            anchor.addnext(calc)
    calc.set("fullCalcOnLoad", "1")


def _remove_abs_path(wb_root: etree._Element) -> bool:
    """ブックに記録された保存場所（フォルダのパス）を除く。"""
    removed = False
    for el in list(wb_root.iter(_ABS_PATH)):
        target = el
        parent = el.getparent()
        while parent is not None and parent is not wb_root:
            if etree.QName(parent).localname == "AlternateContent" and etree.QName(parent).namespace == _MC_NS:
                target = parent
                break
            parent = parent.getparent()
        if target.getparent() is not None:
            target.getparent().remove(target)
            removed = True
    return removed


def _comment_parts(pkg: Package, book: XlsxBook) -> list[str]:
    parts = []
    for sheet in book.sheets:
        for rel in pkg.relationships(sheet.part):
            if rel.external:
                continue
            if rel.kind in ("comments", "threadedComment", "vmlDrawing"):
                parts.append(rel.target)
    for rel in pkg.relationships(book.workbook_part):
        if rel.kind == "person" and not rel.external:
            parts.append(rel.target)
    return parts


def _remove_charts(pkg: Package) -> list[str]:
    charts: list[str] = []
    for part in list(pkg.order):
        if "drawing+xml" not in pkg.content_type(part) or "chart" in pkg.content_type(part):
            continue
        rels = {r.id: r for r in pkg.relationships(part)}
        root = parse_xml(pkg.data[part])
        changed = False
        for anchor in list(root):
            chart_refs = [el for el in anchor.iter(f"{{{_CHART_NS}}}chart")]
            if not chart_refs:
                continue
            for ref in chart_refs:
                rel = rels.get(ref.get(_R_ID, ""))
                if rel is not None and not rel.external:
                    charts.append(rel.target)
            root.remove(anchor)
            changed = True
        if changed:
            pkg.put(part, serialize_xml(root))
    return charts


def _drop_dangling_sheet_links(pkg: Package, part: str, root: etree._Element) -> bool:
    ids = {r.id for r in pkg.relationships(part)}
    changed = False
    for local in ("legacyDrawing", "legacyDrawingHF", "drawing"):
        el = _child(root, local)
        if el is not None and el.get(_R_ID) not in ids:
            root.remove(el)
            changed = True
    return changed


def render(book: XlsxBook, edits: BookEdits) -> bytes:
    """変更を適用した xlsx のバイト列を作る。book 自体は変更しない。"""
    fresh = XlsxBook.parse(book.raw)
    pkg = fresh.pkg

    # 1. 除く部品
    to_remove: list[str] = []
    for info in pkg.parts():
        if info.handling == "remove":
            to_remove.append(info.name)
    if edits.remove_comments:
        to_remove += _comment_parts(pkg, fresh)
    if edits.remove_charts:
        to_remove += _remove_charts(pkg)
    if edits.drop_calc_chain:
        to_remove += pkg.related(fresh.workbook_part, "calcChain")
    removed = set(pkg.remove_parts(to_remove)) if to_remove else set()

    # 2. セル
    original_si: list[etree._Element] = []
    if fresh.sst_part:
        original_si = list(parse_xml(pkg.data[fresh.sst_part]).iter(f"{_M}si"))
    trees: dict[str, etree._Element] = {}
    new_strings: dict[etree._Element, str] = {}
    rich_props: dict[etree._Element, etree._Element | None] = {}
    by_sheet: dict[int, dict[tuple[int, int], CellEdit]] = {}
    for (s, r, c), edit in edits.cells.items():
        by_sheet.setdefault(s, {})[(r, c)] = edit
    for sheet in fresh.sheets:
        if sheet.part not in pkg.data or sheet.part in removed:
            continue
        sheet_edits = by_sheet.get(sheet.idx, {})
        needs_rewrite = bool(sheet_edits) or any(cell.t == "s" for cell in sheet.cells.values())
        root = parse_xml(pkg.data[sheet.part])
        links_changed = _drop_dangling_sheet_links(pkg, sheet.part, root)
        if not needs_rewrite and not links_changed:
            continue
        trees[sheet.part] = root
        if not sheet_edits:
            continue
        data = root.find(f"{_M}sheetData")
        r = -1
        for row_el in data.iter(f"{_M}row"):
            r = int(row_el.get("r")) - 1 if row_el.get("r") else r + 1
            c = -1
            for el in row_el.iter(f"{_M}c"):
                c = parse_ref(el.get("r"))[1] if el.get("r") else c + 1
                edit = sheet_edits.get((r, c))
                if edit is None:
                    continue
                cell = sheet.cells[(r, c)]
                if cell.t == "s" and cell.rich and original_si:
                    rich_props[el] = _first_run_props(original_si[int(_child(el, "v").text)])
                elif cell.t == "inlineStr" and cell.rich:
                    rich_props[el] = _first_run_props(_child(el, "is"))
                _apply_edit(el, cell, edit, fresh.styles, new_strings)
    for el in list(rich_props):
        if el not in new_strings:
            del rich_props[el]

    # 3. 共有文字列表の作り直し
    _rebuild_shared_strings(pkg, fresh, trees, new_strings, original_si, rich_props)

    # 4. 部品の文字（ヘッダー・フッター、グラフ）
    for part, texts in sorted(edits.part_texts.items()):
        if part not in pkg.data or part in removed:
            continue
        kind = text_part_kind(fresh, part)
        if kind == "worksheet":
            if part not in trees:
                trees[part] = parse_xml(pkg.data[part])
            _apply_part_texts(trees[part], kind, texts)
        elif kind == "chart":
            root = parse_xml(pkg.data[part])
            _apply_part_texts(root, kind, texts)
            pkg.put(part, serialize_xml(root))
    for part, root in trees.items():
        pkg.put(part, serialize_xml(root))

    # 5. ブック（再計算の設定・保存場所の記録）
    wb_root = parse_xml(pkg.data[fresh.workbook_part])
    changed = _remove_abs_path(wb_root)
    if edits.full_calc_on_load:
        _set_full_calc(wb_root)
        changed = True
    if changed:
        pkg.put(fresh.workbook_part, serialize_xml(wb_root))

    # 6. 文書プロパティの初期化
    for info in pkg.parts():
        if info.feature != "properties":
            continue
        if "core-properties" in info.content_type:
            pkg.put(info.name, _CORE_EMPTY)
        else:
            pkg.put(info.name, _APP_EMPTY)
    return pkg.write(rewrite_content_types=True)
