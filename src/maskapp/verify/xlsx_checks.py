"""xlsx の出力の検証（設計書 07 章 1、V1・V2・V5・V7・V10）。

書き出したファイルを自前の読込器で読み直し、元のブックと計画から期待されるセルの状態と比べる。
さらに独立した第二の読込器（openpyxl）でも読み、同じ値が読めることを確かめる。
残存検査はセルだけでなく、ZIP 内の全 XML 部品（シート名・名前定義・条件付き書式・入力規則・
ヘッダーフッター・グラフ・コメント・関係定義のリンク先等）を対象にする。
"""

from __future__ import annotations

import io
import re
import warnings
from datetime import date, datetime, time

from lxml import etree

from maskapp.config.model import Action
from maskapp.formats.xlsx.book import MAIN_NS, Cell, CellEdit, XlsxBook, text_nodes, text_part_kind
from maskapp.formats.xlsx.package import Package, XlsxFormatError, classify, parse_xml, source_of_rels
from maskapp.transform.planner import Plan, RunFile
from maskapp.transform.xlsx_plan import BookPlan
from maskapp.verify.checks import Finding, ResidualCheck

_M = f"{{{MAIN_NS}}}"
_NUMBER_ONLY = re.compile(r"^\s*-?\d+(\.\d+)?([eE][-+]?\d+)?\s*$")
_SCHEMA_URI = re.compile(r"^(https?://(schemas\.openxmlformats\.org|schemas\.microsoft\.com|purl\.org|www\.w3\.org)"
                         r"|urn:schemas-microsoft-com)", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")
_MAX_CELL_FINDINGS = 200  # 種類ごとに記録するセル単位の不合格の上限（件数は detail に残す）


def package_problems(pkg: Package) -> list[str]:
    """V10：内容種別・XML の整形式・関係定義の参照先。"""
    problems: set[str] = set()
    for name in pkg.order:
        if name == "[Content_Types].xml":
            continue
        ct = pkg.content_type(name)
        if not ct:
            problems.add("missing_content_type")
        if name.endswith(".rels") or ct.endswith("+xml") or ct.endswith("/xml"):
            try:
                parse_xml(pkg.data[name])
            except XlsxFormatError:
                problems.add("malformed_xml")
    for rels in [n for n in pkg.order if n.endswith(".rels")]:
        source = "" if rels == "_rels/.rels" else source_of_rels(rels)
        try:
            targets = pkg.relationships(source)
        except XlsxFormatError:
            continue
        if any(not r.external and r.target not in pkg.data for r in targets):
            problems.add("dangling_relationship")
    if any(name not in pkg.data for name in pkg.overrides):
        problems.add("override_without_part")
    return sorted(problems)


def _same_value(cell: Cell, value) -> bool:
    if value is None or value == "":
        return cell.text == "" or cell.kind == "empty"
    if cell.kind == "string":
        return value == cell.text
    if cell.kind == "bool":
        return isinstance(value, bool) and ("TRUE" if value else "FALSE") == cell.text
    if cell.kind == "error":
        return str(value) == cell.text
    if cell.kind == "number":
        if isinstance(value, (datetime, date, time)):
            from openpyxl.utils.datetime import to_excel

            value = to_excel(value)
        try:
            a, b = float(cell.text), float(value)
        except (TypeError, ValueError):
            return False
        return abs(a - b) <= 1e-9 * max(1.0, abs(a))
    return True  # 日付型（t="d"）は比較しない


def second_reader(data: bytes, out: XlsxBook) -> tuple[str | None, int]:
    """openpyxl で読み直し、(失敗の種類, 値が一致しないセル数) を返す。"""
    from openpyxl import load_workbook

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - 第二の読込器が読めないことを検出する
        return "second_reader_failed", 0
    mismatches = 0
    try:
        for sheet in out.sheets:
            if sheet.name not in wb.sheetnames:
                return "second_reader_sheet_missing", 0
            values = {}
            for row in wb[sheet.name].iter_rows():
                for c in row:
                    if getattr(c, "value", None) is not None:
                        values[(c.row - 1, c.column - 1)] = c.value
            for pos, cell in sheet.cells.items():
                if not _same_value(cell, values.pop(pos, None)):
                    mismatches += 1
            mismatches += len(values)
    finally:
        wb.close()
    return None, mismatches


def _formula_signature(cell: Cell, new_text: str | None = None):
    f = cell.formula
    if f is None:
        return None
    return (new_text if new_text is not None and f.text is not None else f.text, f.kind, f.ref, f.si)


def _cell_problem(a: Cell, b: Cell, edit: CellEdit | None) -> str | None:
    """元のセル a・出力のセル b・計画の変更 edit から、期待と違う点を返す。"""
    if b.style != a.style:
        return "style_changed"
    if edit is None:
        if (a.kind, a.text, _formula_signature(a)) != (b.kind, b.text, _formula_signature(b)):
            return "kept_value_changed"
        return None
    if edit.clear:
        return None if b.text == "" and b.formula is None else "clear_not_applied"
    if edit.to_value:
        if b.formula is not None:
            return "formula_not_converted"
    elif _formula_signature(b) != _formula_signature(a, edit.formula):
        return "formula_changed"
    if edit.value is not None:
        if b.text != edit.value:
            return "planned_value_mismatch"
        if a.text and b.text == a.text:
            return "replace_not_applied"
    elif edit.drop_cache:
        if b.text != "":
            return "cache_not_cleared"
    elif b.text != a.text:
        return "kept_value_changed"
    return None


def _scan_part_tree(root: etree._Element, residual: ResidualCheck, location: dict) -> None:
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for text in (el.text, el.tail):
            if text and text.strip():
                residual.check(text, location, numeric_context=bool(_NUMBER_ONLY.match(text)))
        for attr, value in el.attrib.items():
            if value and not _SCHEMA_URI.match(value):
                residual.check(value, {**location, "attribute": etree.QName(attr).localname},
                               numeric_context=bool(_NUMBER_ONLY.match(value)))


def _check_part_texts(out: XlsxBook, name: str, root: etree._Element, bp: BookPlan, residual: ResidualCheck,
                      findings: list[Finding], location: dict) -> None:
    """文章内の規則で処理した文字（ヘッダー・フッター、グラフ）：計画どおりの文字か確かめ、
    置換しなかった部分だけを残存検査する。検査した要素は部品全体の検査から外す。"""
    checked = bp.checked_part_texts(name)
    if not checked:
        return
    kind = text_part_kind(out, name)
    nodes = text_nodes(root, kind) if kind else []
    planned = bp.edits.part_texts.get(name, {})
    for i, kept in checked.items():
        if i >= len(nodes):
            findings.append(Finding("transform", "block", "planned_value_mismatch", location))
            continue
        if i in planned and nodes[i].text != planned[i]:
            findings.append(Finding("transform", "block", "planned_value_mismatch", location))
        for part in kept:
            residual.check(part, location, ignore_short=True)
        nodes[i].text = None


def _scan_parts(out: XlsxBook, residual: ResidualCheck, file_id: int, bp: BookPlan,
                findings: list[Finding]) -> None:
    worksheet_parts = {s.part for s in out.sheets}
    for name in out.pkg.order:
        info = classify(name, out.pkg.content_type(name))
        if info.handling in ("image", "binary"):
            continue
        location = {"file_id": file_id, "part": name}
        data = out.pkg.data[name]
        try:
            root = parse_xml(data)
        except XlsxFormatError:
            # VML 等の整形式でない部品は、タグを除いた文字列を検査する
            residual.check(_TAG.sub(" ", data.decode("utf-8", errors="replace")), location)
            continue
        _check_part_texts(out, name, root, bp, residual, findings, location)
        if name in worksheet_parts:
            sheet_data = root.find(f"{_M}sheetData")
            if sheet_data is not None:
                root.remove(sheet_data)  # セルは別にセル単位で検査する
        elif name == out.sst_part:
            for rph in root.iter(f"{_M}rPh"):
                for t in rph.iter(f"{_M}t"):
                    if t.text:
                        residual.check(t.text, {**location, "phonetic": True})
            continue
        elif name == out.workbook_part:
            for sheet in out.sheets:
                residual.check(sheet.name, {"file_id": file_id, "sheet": sheet.idx}, kind="residual_sheet_name")
            sheets_el = root.find(f"{_M}sheets")
            if sheets_el is not None:
                root.remove(sheets_el)
        _scan_part_tree(root, residual, location)


def verify_book(plan: Plan, rf: RunFile, src: XlsxBook, data: bytes, findings: list[Finding],
                residual: ResidualCheck) -> XlsxBook | None:
    loc = {"file_id": rf.file_id}
    bp: BookPlan = plan.books[rf.file_id]
    try:
        out = XlsxBook.parse(data)
    except XlsxFormatError:
        findings.append(Finding("transform", "block", "output_unreadable", loc))
        return None

    # V10 健全性・第二の読込器
    for problem in package_problems(out.pkg):
        findings.append(Finding("transform", "block", "package_invalid", loc, {"problem": problem}))
    failure, mismatches = second_reader(data, out)
    if failure:
        findings.append(Finding("transform", "block", failure, loc))
    elif mismatches:
        findings.append(Finding("transform", "block", "second_reader_mismatch", loc, {"cells": mismatches}))

    # V1 構造
    if [(s.name, s.state) for s in out.sheets] != [(s.name, s.state) for s in src.sheets]:
        findings.append(Finding("transform", "block", "sheet_structure_changed", loc))
        return None

    # V2・V4・V5 セル単位
    counts: dict[str, int] = {}
    for s_src, s_out in zip(src.sheets, out.sheets, strict=True):
        if set(s_src.cells) != set(s_out.cells):
            findings.append(Finding("transform", "block", "cell_set_changed", {**loc, "sheet": s_src.idx}))
        for (r, c), a in s_src.cells.items():
            b = s_out.cells.get((r, c))
            if b is None:
                continue
            problem = _cell_problem(a, b, bp.edits.cells.get((s_src.idx, r, c)))
            if problem:
                counts[problem] = counts.get(problem, 0) + 1
                if counts[problem] <= _MAX_CELL_FINDINGS:
                    findings.append(Finding("transform", "block", problem,
                                            {**loc, "sheet": s_src.idx, "row": r, "column": c}))
    for problem, count in counts.items():
        if count > _MAX_CELL_FINDINGS:
            findings.append(Finding("transform", "block", problem, loc, {"total": count}))

    # V7 残存（セル・数式）
    units = {fp.sheet: fp for fp in plan.units_of(rf.file_id)}
    for sheet in out.sheets:
        fp = units.get(sheet.idx)
        keep_cols = {c for c, spec in enumerate(fp.template.columns) if spec.action is Action.KEEP} if fp else set()
        for (r, c), cell in sheet.cells.items():
            key = (sheet.idx, r, c)
            location = {**loc, "sheet": sheet.idx, "row": r, "column": c}
            if key in bp.freetext:
                for part in bp.freetext[key]:
                    residual.check(part, location, ignore_short=True)
            elif cell.text and key not in bp.generated:
                numeric = (cell.kind == "number" and fp is not None and c in keep_cols
                           and r > fp.template.header_row and not fp.is_outside(r))
                residual.check(cell.text, location, numeric_context=numeric)
            if cell.formula is not None and cell.formula.text and key not in bp.blocked_formulas:
                residual.check(cell.formula.text, {**location, "part": "formula"})
    # V7 残存（セル以外の全部品）・V9 シート名
    _scan_parts(out, residual, rf.file_id, bp, findings)
    return out
