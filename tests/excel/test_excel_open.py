"""Excel での確認（段階2 受入条件。開発時に手元の Excel で実行する）。

  .venv/Scripts/python -m pytest -m excel

合成データの出力を Excel で開き、
- 修復メッセージなしで開けること（壊れたファイルは開けずにエラーになる）
- 全再計算した後の値が、本アプリが書いたキャッシュ値と一致すること
  （文字列を加工する数式として警告したセル・キャッシュ値を消したセルは除く）
を確かめる。Excel がない環境では飛ばす。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from maskapp.formats.xlsx.book import XlsxBook
from maskapp.transform.xlsx_plan import BookOptions
from tests.scenarios.setup import (
    MASTER_ORDER,
    RELATIONS,
    TEMPLATES,
    XLSX_RELATIONS,
    XLSX_TEMPLATES,
    add_files,
    run_through,
)
from tests.scenarios.test_acceptance_xlsx import _project, _run_book
from tests.synthetic.books import feature_book, formula_book, risk_book

pytestmark = pytest.mark.excel
SCRIPT = Path(__file__).with_name("excel_values.ps1")
EXCEL = Path("C:/Program Files/Microsoft Office/root/Office16/EXCEL.EXE")


def _excel_values(paths: list[Path], out: Path) -> list[dict]:
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-Out", str(out),
         *[str(p) for p in paths]],
        check=True, timeout=600, capture_output=True,
    )
    return json.loads(out.read_text(encoding="utf-8"))


def _same(ours: str, theirs: str) -> bool:
    if ours == theirs:
        return True
    try:
        return abs(float(ours) - float(theirs)) <= 1e-6 * max(1.0, abs(float(ours)))
    except ValueError:
        return False


@pytest.fixture(scope="module")
def outputs(tmp_path_factory, dataset, dataset_dir) -> list[tuple[Path, set]]:
    if not EXCEL.exists() or shutil.which("powershell") is None:
        pytest.skip("Excel is not installed")
    base = tmp_path_factory.mktemp("excel")
    collected: list[tuple[Path, set]] = []  # (出力, 再計算で値が変わってよいセル)

    project, wb = _project(base / "base", TEMPLATES + XLSX_TEMPLATES, RELATIONS + XLSX_RELATIONS)
    with project:
        files = [dataset_dir / f"{m}.xlsx" for m in MASTER_ORDER] + [dataset_dir / "製造原価_202604.xlsx"]
        run = wb.start_run()
        add_files(wb, run, files)
        report, staged = run_through(wb, run)
        assert report.ok
        for p in staged:
            target = base / "out" / f"base_{p.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(p, target)
            collected.append((target, set()))

    cases = [
        ("formula", formula_book(dataset)[0], BookOptions(formulas_to_values=True)),
        ("comment", risk_book(dataset, "comment"), BookOptions(remove_comments=True)),
        ("rich", risk_book(dataset, "rich_text"), None),
        ("properties", risk_book(dataset, "properties"), None),
        ("chart", feature_book(dataset, "chart"), BookOptions(remove_charts=True)),
        ("chart_replaced", feature_book(dataset, "chart"), None),  # グラフの文字を置換（段階4）
        ("header_footer", risk_book(dataset, "header_footer"), None),  # ヘッダーの文字を置換（段階4）
        ("image", feature_book(dataset, "image"), BookOptions(accept_images=True)),
    ]
    for label, spec, options in cases:
        project, wb, run, _, report, staged = _run_book(base / label, spec, options)
        with project:
            assert report.ok, (label, report.blocking[:3])
            changing = set()
            for f in report.findings:
                if f.kind in ("formula_text_function", "formula_cache_cleared"):
                    changing |= {(c["sheet"], c["cell"]) for c in f.detail["cells"]}
            target = base / "out" / f"{label}.xlsx"
            shutil.copy(staged[0], target)
            collected.append((target, changing))
    return collected


def test_outputs_open_in_excel_and_recalculate_to_same_values(outputs, tmp_path):
    results = _excel_values([p for p, _ in outputs], tmp_path / "excel.json")
    assert len(results) == len(outputs)
    compared = 0
    for (path, changing), result in zip(outputs, results, strict=True):
        assert result["opened"], (path.name, result.get("error"))
        assert "修復" not in result["caption"] and "Repaired" not in result["caption"], path.name
        book = XlsxBook.parse(path.read_bytes())
        for sheet, excel_sheet in zip(book.sheets, result["sheets"], strict=True):
            assert sheet.name == excel_sheet["name"]
            excel = {(r, c): v for r, c, v in excel_sheet["cells"]}
            for (r, c), cell in sheet.cells.items():
                ref = cell.ref
                if (sheet.idx, ref) in changing:
                    continue
                theirs = excel.get((r, c), "")
                if cell.kind == "bool":
                    theirs = {"True": "TRUE", "False": "FALSE"}.get(theirs, theirs)
                assert _same(cell.text, theirs), (path.name, sheet.name, ref, cell.text, theirs)
                compared += 1
        if path.name == "formula.xlsx":
            # キャッシュ値を消したセル・文字列を加工する数式も、再計算すると置換後の名称から計算される
            excel = {(r, c): v for r, c, v in result["sheets"][2]["cells"]}
            customers = book.sheets[0].grid()
            assert excel[(4, 1)] == customers[2][1]  # =顧客!B3（同名の別顧客）
            assert excel[(5, 1)] == customers[1][1][:4]  # =LEFT(顧客!B2,4)
    assert compared > 20_000
