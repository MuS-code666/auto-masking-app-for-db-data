"""段階2 受入シナリオ（xlsx）：S1〜S8 を xlsx で、S9・S10・S15（設計書 09 章）。"""

from __future__ import annotations

import zipfile
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

from maskapp.app import Workbench
from maskapp.config import TemplateSpec
from maskapp.formats.xlsx.book import XlsxBook
from maskapp.formats.xlsx.package import XlsxFormatError
from maskapp.ingest import UnsupportedFormatError
from maskapp.project import Project
from maskapp.transform.xlsx_plan import BookOptions
from tests.scenarios.setup import (
    MASTER_ORDER,
    RELATIONS,
    TEMPLATES,
    XLSX_RELATIONS,
    XLSX_TEMPLATES,
    add_files,
    code,
    keep,
    name,
    run_through,
)
from tests.synthetic.books import customers, feature_book, formula_book, risk_book
from tests.synthetic.generator import ALIAS_FULL, DUP_CUSTOMER_NAME, SHARED_NAME
from tests.synthetic.ooxml import build_xlsx

BOOK_TEMPLATES = [
    TemplateSpec("顧客一覧", [code("顧客番号", "CUSTOMER", "顧客コード"), name("顧客名", "CUSTOMER")]),
    TemplateSpec("売上記録", [keep("売上日"), code("得意先CD", "CUSTOMER", "顧客コード"),
                          name("得意先名", "CUSTOMER"), keep("金額")]),
    TemplateSpec("顧客別金額", [name("顧客", "CUSTOMER"), keep("金額")]),
]


def _all_text(path: Path) -> str:
    """出力ファイルの全部品を文字列として連結する（残存の確認用）。"""
    if path.suffix == ".csv":
        return path.read_bytes().decode("cp932", errors="ignore") + path.read_bytes().decode("utf-8", "ignore")
    with zipfile.ZipFile(path) as zf:
        return "\n".join(zf.read(n).decode("utf-8", errors="ignore") for n in zf.namelist())


def _project(tmp_path: Path, templates, relations=()) -> tuple[Project, Workbench]:
    project = Project.create(tmp_path, "xlsx受入")
    wb = Workbench(project)
    wb.save_config(templates, list(relations))
    return project, wb


def _run_book(tmp_path: Path, spec, options: BookOptions | None = None, filename: str = "book.xlsx"):
    path = tmp_path / "in" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_xlsx(spec))
    project, wb = _project(tmp_path, BOOK_TEMPLATES)
    run = wb.start_run()
    file_id = add_files(wb, run, [path])[0]
    if options:
        wb.set_options(run, file_id, options)
    report, staged = run_through(wb, run)
    return project, wb, run, file_id, report, staged


# ---------------------------------------------------------------- S1〜S8（xlsx のマスタ・数式入りの月次）


def test_s1_to_s8_with_xlsx(tmp_path, dataset, dataset_dir):
    project, wb = _project(tmp_path, TEMPLATES + XLSX_TEMPLATES, RELATIONS + XLSX_RELATIONS)
    with project:
        files = [dataset_dir / f"{m}.xlsx" for m in MASTER_ORDER] + [
            dataset_dir / "売上明細_202604.csv", dataset_dir / "仕入明細_202604.csv",
            dataset_dir / "製造原価_202604.xlsx",
        ]
        run = wb.start_run()
        add_files(wb, run, files)
        report, _ = run_through(wb, run)
        assert report.ok, report.blocking[:5]
        assert not [f for f in wb.findings(run) if f["origin"] == "transform" and f["severity"] != "info"]
        wb.commit(run)
        outputs = {p.name: p for p in wb.publish(run, tmp_path / "export")}

        src = XlsxBook.parse(files[-1].read_bytes())
        out = XlsxBook.parse(outputs["製造原価_01.xlsx"].read_bytes())
        s_src, s_out = src.sheets[0], out.sheets[0]
        # S6: 行数・セルの位置・維持列の値と数式が同じ
        assert set(s_src.cells) == set(s_out.cells)
        keep_cols = [i for i, c in enumerate(XLSX_TEMPLATES[0].columns) if c.action.value == "keep"]
        for (r, c), cell in s_src.cells.items():
            if c in keep_cols:
                assert (s_out.cells[(r, c)].text, s_out.cells[(r, c)].kind) == (cell.text, cell.kind)
            if cell.formula is not None:
                assert s_out.cells[(r, c)].formula == cell.formula  # 数式は残る（文字列定数なし）

        # S1: 数式の参照（VLOOKUP）の結果が、置換後の製品一覧で引いた値と一致する
        lookup = {row[0]: row[1] for row in out.sheets[1].grid()[1:]}
        grid = s_out.grid()
        assert all(lookup[row[2]] == row[3] for row in grid[1:])
        # S1: 製品別の原料費の合計が、変換後のコードで一致
        before: dict[str, Decimal] = defaultdict(Decimal)
        after: dict[str, Decimal] = defaultdict(Decimal)
        for row in s_src.grid()[1:]:
            before[row[2]] += Decimal(row[8])
        for row in grid[1:]:
            after[row[2]] += Decimal(row[8])
        assert sorted(before.values()) == sorted(after.values())

        # 担当者コード：マスタ（xlsx）は表示形式 "0000" の数値、月次は文字列。変換後も結合できる
        persons = XlsxBook.parse(outputs["担当者マスタ_01.xlsx"].read_bytes()).sheets[0]
        assert persons.cells[(1, 0)].kind == "number"
        person_codes = {row[0] for row in persons.grid()[1:]}
        assert {row[16] for row in grid[1:]} <= person_codes

        # S3・S4・S5（マスタが xlsx）
        cust = XlsxBook.parse(outputs["顧客マスタ_01.xlsx"].read_bytes()).sheets[0].grid()
        masked = [row[1] for row in cust[1:]]
        assert len(set(masked)) == len(masked)  # 同名の別顧客も別の名称

        # 元の名称・住所・電話・メールが出力のどの部品にも残っていない
        text = "\n".join(_all_text(p) for p in outputs.values())
        leaked = [v for v in dataset.truth["sensitive_values"] if len(v) > 4 and v in text]
        assert leaked == []
        for value in (SHARED_NAME, DUP_CUSTOMER_NAME, ALIAS_FULL):
            assert value not in text


def test_s11_xlsx_rerun_and_restage_give_identical_output(tmp_path, dataset):
    spec, _ = formula_book(dataset)
    path = tmp_path / "in" / "book.xlsx"
    path.parent.mkdir()
    path.write_bytes(build_xlsx(spec))
    project, wb = _project(tmp_path, BOOK_TEMPLATES)
    with project:
        outputs = []
        for i in range(2):
            run = wb.start_run()
            file_id = add_files(wb, run, [path])[0]
            wb.set_options(run, file_id, BookOptions(formulas_to_values=True))
            report, staged = run_through(wb, run)
            assert report.ok
            wb.commit(run)
            if i == 1:
                staged[0].unlink()  # 作業領域を失っても、確定した対応から同じファイルを作り直せる
                wb.restage_committed(run)
            outputs.append(wb.publish(run, tmp_path / f"export{i}")[0].read_bytes())
        assert outputs[0] == outputs[1]


# ---------------------------------------------------------------- S15 数式


def test_s15_formulas(tmp_path, dataset):
    spec, facts = formula_book(dataset)
    project, wb, run, file_id, report, _ = _run_book(tmp_path, spec)
    with project:
        # 部分一致（"*さくら*"）は自動で置換せず停止する
        partial = report.by_kind("formula_constant_partial")
        assert partial and partial[0].detail["cells"] == [{"sheet": 2, "cell": "B7"}]
        assert not report.ok

        # 「値に変換」を選ぶと出力できる
        wb.set_options(run, file_id, BookOptions(formulas_to_values=True))
        report, staged = run_through(wb, run)
        assert report.ok, report.blocking[:5]
        kinds = {f.kind: f for f in report.findings}
        assert kinds["formula_text_function"].detail["cells"] == [{"sheet": 2, "cell": "B6"}]
        assert kinds["formula_cache_cleared"].detail["cells"] == [{"sheet": 2, "cell": "B5"}]
        assert kinds["formula_converted_to_value"].detail["cells"] == [{"sheet": 2, "cell": "B7"}]
        assert kinds["formula_constant_replaced"].detail["count"] == 2

        src = XlsxBook.parse(spec_bytes := build_xlsx(spec))
        out = XlsxBook.parse(staged[0].read_bytes())
        custs = dict(customers(dataset))
        masked = {row[0]: row[1] for row in out.sheets[0].grid()[1:]}  # 変換後のコード → 名称
        aoba_masked = out.sheets[0].grid()[1][1]

        # 参照系：数式は同じ、キャッシュ値は参照先と同じ対応で置換（再計算しても一致）
        for r in range(1, 31):
            a, b = src.sheets[1].cells[(r, 2)], out.sheets[1].cells[(r, 2)]
            assert b.formula == a.formula
            assert b.text == masked[out.sheets[1].cells[(r, 1)].text]
            assert custs[src.sheets[1].cells[(r, 1)].text] == a.text
        summary = {r: out.sheets[2].cells[(r, 1)] for r in range(1, 8)}
        # 条件集計・条件分岐：文字列定数を置換、数値の結果は維持
        assert summary[1].formula.text == f'SUMIF(売上!C:C,"{aoba_masked}",売上!D:D)'
        assert Decimal(summary[1].text) == facts.aoba_total
        assert summary[2].formula.text == f'IF(売上!C2="{aoba_masked}","重点","")' and summary[2].text == "重点"
        # 他シート参照：キャッシュ値を置換。同名の別顧客を指すキャッシュ値は消す（開いたときに再計算）
        assert summary[3].text == aoba_masked
        assert summary[4].formula.text == "顧客!B3" and summary[4].text == ""
        # 文字列操作：数式・キャッシュ値とも維持（警告済み）
        assert summary[5].formula.text == "LEFT(顧客!B2,4)"
        # 部分一致の数式は値に変換
        assert summary[6].formula is None and summary[6].text == "1"
        assert Decimal(summary[7].text) == facts.grand_total
        with zipfile.ZipFile(staged[0]) as zf:
            assert b'fullCalcOnLoad="1"' in zf.read("xl/workbook.xml")
            assert "xl/calcChain.xml" not in zf.namelist()
        text = _all_text(staged[0])
        assert ALIAS_FULL not in text and DUP_CUSTOMER_NAME not in text and SHARED_NAME not in text
        assert len(spec_bytes) > 0


# ---------------------------------------------------------------- S9 残存リスクの検出・明示


@pytest.mark.parametrize("risk,kind,location", [
    ("comment", "comments_present", None),
    ("hidden_sheet", "residual", {"sheet": 1, "row": 1, "column": 0}),
    ("sheet_name", "residual_sheet_name", {"sheet": 1}),
    ("defined_name", "residual", {"part": "xl/workbook.xml"}),
    ("header_footer", "residual", {"part": "xl/worksheets/sheet1.xml"}),
    ("validation_list", "residual", {"part": "xl/worksheets/sheet1.xml"}),
])
def test_s9_residual_risks_are_blocked(tmp_path, dataset, risk, kind, location):
    project, wb, run, _, report, _ = _run_book(tmp_path, risk_book(dataset, risk))
    with project:
        assert not report.ok
        hits = report.by_kind(kind)
        assert hits, [f.kind for f in report.blocking]
        if location:
            assert any(location.items() <= h.location.items() for h in hits)
        if risk == "hidden_sheet":
            assert report.by_kind("hidden_sheet")


def test_s9_comments_can_be_removed(tmp_path, dataset):
    spec = risk_book(dataset, "comment")
    project, wb, run, file_id, report, _ = _run_book(tmp_path, spec, BookOptions(remove_comments=True))
    with project:
        assert report.ok, report.blocking
        assert report.by_kind("comments_removed")
        wb.commit(run)
        out = wb.publish(run, tmp_path / "export")[0]
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert not [n for n in names if "comments" in n or n.endswith(".vml")]
            assert b"legacyDrawing" not in zf.read("xl/worksheets/sheet1.xml")
        assert customers(dataset)[0][1] not in _all_text(out)


def test_s9_properties_location_and_thumbnail_are_cleared(tmp_path, dataset):
    project, wb, run, _, report, staged = _run_book(tmp_path, risk_book(dataset, "properties"))
    with project:
        assert report.ok, report.blocking
        assert {f.detail.get("feature") for f in report.by_kind("part_removed")} == {"thumbnail"}
        with zipfile.ZipFile(staged[0]) as zf:
            assert "docProps/thumbnail.png" not in zf.namelist()
            assert b"absPath" not in zf.read("xl/workbook.xml")
            assert b"creator" not in zf.read("docProps/core.xml")
            assert b"Company" not in zf.read("docProps/app.xml")
        text = _all_text(staged[0])
        assert customers(dataset)[0][1] not in text and customers(dataset)[5][1] not in text


def test_s9_rich_text_and_phonetic_are_replaced(tmp_path, dataset):
    project, wb, run, _, report, staged = _run_book(tmp_path, risk_book(dataset, "rich_text"))
    with project:
        assert report.ok, report.blocking
        assert report.by_kind("rich_text_flattened")[0].detail["count"] == 1
        out = XlsxBook.parse(staged[0].read_bytes())
        cell = out.sheets[0].cells[(1, 1)]
        assert cell.text.startswith("顧客") and cell.rich and not cell.phonetic
        assert "カブシキガイシャ" not in _all_text(staged[0])


def test_s9_output_filename_is_checked(tmp_path, dataset):
    spec = risk_book(dataset, "properties")
    path = tmp_path / "in.xlsx"
    path.write_bytes(build_xlsx(spec))
    project, wb = _project(tmp_path, BOOK_TEMPLATES)
    with project:
        run = wb.start_run()
        file_id = add_files(wb, run, [path])[0]
        wb.plan(run)
        from tests.scenarios.setup import resolve_reviews

        resolve_reviews(wb, run)
        wb.stage(run, {file_id: f"{customers(dataset)[0][1]}.xlsx"})
        assert wb.verify(run).by_kind("residual_filename")


# ---------------------------------------------------------------- S10 未対応の機能


@pytest.mark.parametrize("feature", ["pivot", "external_link", "connection", "macro", "unknown_part"])
def test_s10_unsupported_features_stop(tmp_path, dataset, feature):
    project, wb, run, _, report, _ = _run_book(tmp_path, feature_book(dataset, feature))
    with project:
        assert not report.ok
        assert feature in {f.detail["feature"] for f in report.by_kind("unsupported_feature")}
        assert wb.plan(run).blocking_notes


def test_s10_comments_stop_until_removed(tmp_path, dataset):
    project, wb, run, file_id, report, _ = _run_book(tmp_path, feature_book(dataset, "comments"))
    with project:
        assert report.by_kind("comments_present") and not report.ok
        wb.set_options(run, file_id, BookOptions(remove_comments=True))
        report, _ = run_through(wb, run)
        assert report.ok


def test_s10_chart_cache_blocks_until_chart_removed(tmp_path, dataset):
    project, wb, run, file_id, report, _ = _run_book(tmp_path, feature_book(dataset, "chart"))
    with project:
        residual = [f for f in report.by_kind("residual") if "charts/" in f.location.get("part", "")]
        assert residual and not report.ok
        wb.set_options(run, file_id, BookOptions(remove_charts=True))
        report, staged = run_through(wb, run)
        assert report.ok, report.blocking
        with zipfile.ZipFile(staged[0]) as zf:
            assert not [n for n in zf.namelist() if "charts/" in n]
            assert b"graphicFrame" not in zf.read("xl/drawings/drawing2.xml")


def test_s10_images_need_confirmation(tmp_path, dataset):
    project, wb, run, file_id, report, _ = _run_book(tmp_path, feature_book(dataset, "image"))
    with project:
        assert report.by_kind("images_need_confirmation") and not report.ok
        wb.set_options(run, file_id, BookOptions(accept_images=True))
        report, staged = run_through(wb, run)
        assert report.ok and report.by_kind("images_not_inspected")
        with zipfile.ZipFile(staged[0]) as zf:
            assert "xl/media/image1.png" in zf.namelist()


@pytest.mark.parametrize("case", ["encrypted", "strict", "not_zip", "xlsm"])
def test_s10_unreadable_or_unsupported_files_are_refused(tmp_path, dataset, case):
    project, wb = _project(tmp_path, BOOK_TEMPLATES)
    with project:
        path = tmp_path / ("book.xlsm" if case == "xlsm" else "book.xlsx")
        data = build_xlsx(feature_book(dataset, "comments"))
        if case == "encrypted":
            data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
        elif case == "strict":
            with zipfile.ZipFile(path.with_suffix(".tmp"), "w") as zf, zipfile.ZipFile(
                __import__("io").BytesIO(data)
            ) as src:
                for n in src.namelist():
                    body = src.read(n)
                    if n == "xl/workbook.xml":
                        body = body.replace(b"http://schemas.openxmlformats.org/spreadsheetml/2006/main",
                                            b"http://purl.oclc.org/ooxml/spreadsheetml/main")
                    zf.writestr(n, body)
            data = path.with_suffix(".tmp").read_bytes()
        elif case == "not_zip":
            data = b"PK\x03\x04broken"
        path.write_bytes(data)
        expected = UnsupportedFormatError if case == "xlsm" else XlsxFormatError
        with pytest.raises(expected) as info:
            wb.import_file(path)
        assert info.value.reason in {"encrypted_or_legacy", "strict_ooxml", "not_a_zip",
                                     "unsupported_spreadsheet_format"}


def test_tables_are_assigned_per_sheet(tmp_path, dataset):
    """表の定義はシートごとに見出しで照合する。どの定義にも合わないシートは出力に含め、残存検査する。"""
    from maskapp.config.model import ConfigError

    spec, _ = formula_book(dataset)
    path = tmp_path / "book.xlsx"
    path.write_bytes(build_xlsx(spec))
    project, wb = _project(tmp_path, BOOK_TEMPLATES)
    with project:
        run = wb.start_run()
        file_id = wb.import_file(path).id
        assert wb.add_input(run, file_id, "売上記録") == {1: "売上記録"}
        assert wb.add_input(run, file_id, sheet="顧客") == {0: "顧客一覧"}
        with pytest.raises(ConfigError):
            wb.add_input(run, file_id, "顧客一覧", sheet="集計")
        wb.plan(run)
        assert {(p.record, p.column) for p in wb.preview(run, file_id, sheet=1)} == {
            (r, c) for r in range(1, 31) for c in (1, 2)
        }
