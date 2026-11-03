"""xlsx の読込・書込・数式の字句解析の試験（設計書 06 章 2〜4）。"""

from __future__ import annotations

import io
import zipfile

import pytest

from maskapp.formats.xlsx.book import BookEdits, XlsxBook, decode_text, encode_text, render
from maskapp.formats.xlsx.formula import analyze, replace_constants, tokenize
from maskapp.formats.xlsx.package import Package, classify
from tests.synthetic.generator import Formula, NumCode
from tests.synthetic.ooxml import BookSpec, InlineText, RichText, SheetSpec, build_xlsx

# ---------------------------------------------------------------- 数式の字句解析


def test_string_constants_and_sheet_references():
    text = 'SUMIF(\'売上 明細\'!C:C,"さくら""商事",売上!D:D)&"様"'
    info = analyze(text)
    assert [t.value for t in info.constants] == ['さくら"商事', "様"]
    assert info.sheets == ["売上 明細", "売上"]
    assert info.functions == {"SUMIF"}
    assert info.concatenates and info.manipulates_text


def test_replace_constants_keeps_everything_else():
    text = 'IF(B2="さくら",顧客!B2,"さくら以外")'
    info = analyze(text)
    first = info.constants[0]
    assert replace_constants(text, info, {first.start: '顧客"0001'}) == 'IF(B2="顧客""0001",顧客!B2,"さくら以外")'


def test_external_and_structured_references():
    assert analyze("[1]Sheet1!A1+1").external
    assert analyze("'[売上.xlsx]Sheet1'!A1").external
    info = analyze("SUM(売上表[金額])+COUNTA(売上表[[#This Row],[得意先名]])")
    assert not info.external
    assert [t.kind for t in info.tokens if t.kind == "struct"] == ["struct", "struct"]


def test_function_prefix_and_text_functions():
    assert analyze("_xlfn.TEXTJOIN(\",\",TRUE,A1:A3)").functions == {"TEXTJOIN"}
    assert analyze("_xlfn.XLOOKUP(A2,顧客!A:A,顧客!B:B)").manipulates_text is False
    assert analyze("LEFT(B2,3)").manipulates_text


def test_errors_and_operators():
    kinds = [t.kind for t in tokenize('IFERROR(1/0,#DIV/0!)<>"x"')]
    assert "error" in kinds and kinds.count("string") == 1


# ---------------------------------------------------------------- 読込


def _book(*sheets: SheetSpec, **kw) -> XlsxBook:
    return XlsxBook.parse(build_xlsx(BookSpec(list(sheets), **kw)))


def test_cell_kinds_and_matching_text():
    book = _book(SheetSpec("s", [
        ["文字", 12, NumCode(12, 4), True, InlineText("インライン"), RichText((("太", True), ("字", False)))],
        [Formula("=A1&\"x\"", "文字x"), Formula("=B1*2", 24), Formula("=B1", None)],
    ]))
    cells = book.sheets[0].cells
    assert [cells[(0, c)].text for c in range(6)] == ["文字", "12", "0012", "TRUE", "インライン", "太字"]
    assert cells[(0, 2)].kind == "number"
    assert cells[(0, 5)].rich
    assert (cells[(1, 0)].text, cells[(1, 0)].formula.text) == ("文字x", 'A1&"x"')
    assert cells[(1, 1)].text == "24"
    assert cells[(1, 2)].kind == "empty" and cells[(1, 2)].formula is not None


def test_escaped_characters():
    assert decode_text("a_x000D_b") == "a\rb"
    assert encode_text("_x0041_") == "_x005F_x0041_"
    assert decode_text(encode_text("a\x01_x0041_")) == "a\x01_x0041_"


def test_classification_of_parts():
    assert classify("xl/pivotTables/p.xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.pivotTable+xml").handling == "block"
    assert classify("xl/media/image1.png", "image/png").handling == "image"
    assert classify("docProps/thumbnail.jpeg", "image/jpeg").handling == "remove"
    assert classify("customXml/item1.xml", "application/xml").handling == "block"
    assert classify("xl/x.bin", "application/octet-stream").feature == "unknown_part"
    assert classify("_rels/.rels", "application/vnd.openxmlformats-package.relationships+xml").handling == "keep"


# ---------------------------------------------------------------- 書込


def test_render_without_edits_keeps_cells_and_untouched_parts():
    raw = build_xlsx(BookSpec([SheetSpec("s", [["a", 1], [Formula("=B1+1", 2), "b"]])]))
    book = XlsxBook.parse(raw)
    out = render(book, BookEdits())
    again = XlsxBook.parse(out)
    assert again.sheets[0].grid() == book.sheets[0].grid()
    src, dst = Package.read(raw), Package.read(out)
    assert dst.data["xl/styles.xml"] == src.data["xl/styles.xml"]  # 書き換えない部品は元のバイト列
    assert render(book, BookEdits()) == out  # 同じ入力から同じ出力


def test_shared_strings_are_rebuilt_without_unreferenced_entries():
    raw = build_xlsx(BookSpec([SheetSpec("s", [["さくら商事"], ["維持"]])]))
    # 元の共有文字列表に、どのセルからも参照されない文字列（過去の編集の残骸）を足す
    pkg = Package.read(raw)
    pkg.data["xl/sharedStrings.xml"] = pkg.data["xl/sharedStrings.xml"].replace(
        b"</sst>", "<si><t>残骸の顧客名</t></si></sst>".encode())
    raw = pkg.write(rewrite_content_types=False)
    book = XlsxBook.parse(raw)
    edits = BookEdits()
    edits.edit(0, 0, 0).value = "顧客0001"
    out = render(book, edits)
    sst = Package.read(out).data["xl/sharedStrings.xml"].decode()
    assert "残骸の顧客名" not in sst and "さくら商事" not in sst
    assert XlsxBook.parse(out).sheets[0].grid() == [["顧客0001"], ["維持"]]


def test_numeric_code_stays_numeric_when_display_matches():
    book = _book(SheetSpec("s", [[NumCode(12, 4), NumCode(7, 4), 20001]]))
    edits = BookEdits()
    edits.edit(0, 0, 0).value = "9001"
    edits.edit(0, 0, 1).value = "X0007"  # 数字だけでない → 文字列セル
    edits.edit(0, 0, 2).value = "90001"
    cells = XlsxBook.parse(render(book, edits)).sheets[0].cells
    assert (cells[(0, 0)].kind, cells[(0, 0)].text, cells[(0, 0)].style) == ("number", "9001", 1)
    assert (cells[(0, 1)].kind, cells[(0, 1)].text) == ("string", "X0007")
    assert (cells[(0, 2)].kind, cells[(0, 2)].text) == ("number", "90001")


def test_formula_cache_constant_clear_and_to_value():
    book = _book(SheetSpec("s", [
        ["さくら", Formula("=A1", "さくら"), Formula('=IF(A1="さくら",1,0)', 1),
         Formula('=COUNTIF(A:A,"*さくら*")', 1), Formula("=A1", "さくら"), Formula("=A1", "さくら")],
    ]))
    edits = BookEdits()
    edits.edit(0, 0, 0).value = "顧客0001"
    edits.edit(0, 0, 1).value = "顧客0001"
    edits.edit(0, 0, 2).formula = 'IF(A1="顧客0001",1,0)'
    edits.edit(0, 0, 3).to_value = True
    edits.edit(0, 0, 4).drop_cache = True
    edits.edit(0, 0, 5).clear = True
    edits.full_calc_on_load = True
    edits.drop_calc_chain = True
    out = render(book, edits)
    cells = XlsxBook.parse(out).sheets[0].cells
    assert (cells[(0, 1)].formula.text, cells[(0, 1)].text) == ("A1", "顧客0001")
    assert (cells[(0, 2)].formula.text, cells[(0, 2)].text) == ('IF(A1="顧客0001",1,0)', "1")
    assert (cells[(0, 3)].formula, cells[(0, 3)].text) == (None, "1")
    assert (cells[(0, 4)].formula.text, cells[(0, 4)].text) == ("A1", "")
    assert (cells[(0, 5)].formula, cells[(0, 5)].text, cells[(0, 5)].kind) == (None, "", "empty")
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        assert b'fullCalcOnLoad="1"' in zf.read("xl/workbook.xml")


def test_shared_formula_group_text_is_replaced_once():
    book = _book(SheetSpec("s", [
        ["さくら", 1], ["もみじ", 2],
        [Formula('=SUMIF(A1:A2,"さくら",B1:B2)', 1, shared=True)],
        [Formula('=SUMIF(A1:A2,"さくら",B1:B2)', 1, shared=True)],
    ]))
    master = book.sheets[0].cells[(2, 0)]
    assert master.formula.kind == "shared" and book.sheets[0].cells[(3, 0)].formula.text is None
    edits = BookEdits()
    edits.edit(0, 2, 0).formula = 'SUMIF(A1:A2,"顧客0001",B1:B2)'
    cells = XlsxBook.parse(render(book, edits)).sheets[0].cells
    assert cells[(2, 0)].formula.text == 'SUMIF(A1:A2,"顧客0001",B1:B2)'
    assert cells[(3, 0)].formula.si == cells[(2, 0)].formula.si


def test_remove_parts_cleans_relationships():
    raw = build_xlsx(BookSpec([SheetSpec("s", [["a"]], image=True)], thumbnail=True))
    pkg = Package.read(raw)
    removed = pkg.remove_parts(["xl/drawings/drawing1.xml"])
    assert set(removed) == {"xl/drawings/drawing1.xml", "xl/media/image1.png"}
    assert all(r.target in pkg.data for r in pkg.relationships("xl/worksheets/sheet1.xml"))


@pytest.mark.parametrize("bad", [b"", b"\x00" * 10])
def test_garbage_is_rejected(bad):
    from maskapp.formats.xlsx.package import XlsxFormatError

    with pytest.raises(XlsxFormatError):
        XlsxBook.parse(bad)
