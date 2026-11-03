"""合成データ生成器の試験。後続段階の試験の前提になるため、構成と仕掛けを確認する。"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from openpyxl import load_workbook

from tests.synthetic import generate
from tests.synthetic.generator import (
    ALIAS_FULL,
    ALIAS_SHORT,
    DUP_CUSTOMER_NAME,
    MASTER_ROWS,
    MONTHLY_ROWS,
    SHARED_NAME,
    UNREGISTERED_CUSTOMER_CODES,
    to_csv_bytes,
)


def _read_csv(path: Path, encoding: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(path.read_bytes().decode(encoding), newline="")))


def test_base_case_file_set(dataset_dir: Path):
    names = sorted(p.name for p in dataset_dir.iterdir())
    assert len([n for n in names if n.endswith("マスタ.xlsx")]) == 5
    assert len([n for n in names if n.startswith("売上明細_") and n.endswith(".csv")]) == 5
    assert len([n for n in names if n.startswith("仕入明細_") and n.endswith(".csv")]) == 5
    assert len([n for n in names if n.startswith("製造原価_") and n.endswith(".xlsx")]) == 5
    assert "truth.json" in names


def test_master_shapes(dataset_dir: Path):
    for path in dataset_dir.glob("*マスタ.xlsx"):
        ws = load_workbook(path).worksheets[0]
        assert (ws.max_row, ws.max_column) == (MASTER_ROWS + 1, 10), path.name


def test_monthly_csv_formats(dataset_dir: Path):
    for path in dataset_dir.glob("売上明細_*.csv"):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" in raw
        rows = _read_csv(path, "cp932")
        assert len(rows) == MONTHLY_ROWS + 1
        assert {len(r) for r in rows} == {20}
    for path in dataset_dir.glob("仕入明細_*.csv"):
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" not in raw
        rows = _read_csv(path, "utf-8-sig")
        assert len(rows) == MONTHLY_ROWS + 1
        assert {len(r) for r in rows} == {20}


def test_cost_workbook_has_formulas(dataset_dir: Path):
    ws = load_workbook(dataset_dir / "製造原価_202604.xlsx")["製造原価"]
    assert (ws.max_row, ws.max_column) == (MONTHLY_ROWS + 1, 20)
    assert ws["D2"].value.startswith("=VLOOKUP(")
    assert ws["I2"].value == "=G2*H2"


def test_person_code_is_numeric_with_leading_zero_format(dataset_dir: Path):
    ws = load_workbook(dataset_dir / "担当者マスタ.xlsx").worksheets[0]
    assert ws["A2"].value == 1
    assert ws["A2"].number_format == "0000"
    sales = _read_csv(dataset_dir / "売上明細_202604.csv", "cp932")
    assert all(len(r[14]) == 4 for r in sales[1:])  # 月次 CSV では "0012" の文字列


def test_acceptance_case_hooks(dataset):
    customers = dataset.masters["顧客マスタ"].rows
    products = dataset.masters["製品マスタ"].rows
    # S3: 顧客名と製品名に同じ文字列
    assert SHARED_NAME in [r[1] for r in customers]
    assert SHARED_NAME in [r[1] for r in products]
    # S4: 同名で別コードの顧客
    dup = [r[0] for r in customers if r[1] == DUP_CUSTOMER_NAME]
    assert len(dup) == 2 and dup[0] != dup[1]
    # 顧客コードは 6 桁で先頭ゼロを含む
    assert all(len(r[0]) == 6 for r in customers)
    assert any(r[0].startswith("0") for r in customers)

    sales_rows = [row for t in dataset.monthly["売上明細"] for row in t.rows]
    # S5: 略称と正式名の両方が売上明細に現れる
    names = {r[4] for r in sales_rows}
    assert ALIAS_SHORT in names and ALIAS_FULL in names
    # S7: 顧客マスタにない得意先CD
    master_codes = {r[0] for r in customers}
    assert set(UNREGISTERED_CUSTOMER_CODES) <= {r[3] for r in sales_rows} - master_codes
    # S8: 空欄・負数・小数・改行・引用符
    assert "" in {r[3] for r in sales_rows}
    assert any(r[7].startswith("-") for r in sales_rows)
    assert any("." in r[7] for r in sales_rows)
    remarks = [r[19] for r in sales_rows]
    assert any("\n" in x for x in remarks)
    assert any('"' in x for x in remarks)
    assert any("," in x for x in remarks)


def test_generation_is_deterministic(dataset):
    again = generate()
    assert json.dumps(again.truth, default=str) == json.dumps(dataset.truth, default=str)
    for kind in ("売上明細", "仕入明細"):
        for a, b in zip(dataset.monthly[kind], again.monthly[kind], strict=True):
            assert to_csv_bytes(a, "utf-8", "\n") == to_csv_bytes(b, "utf-8", "\n")


def test_different_seed_changes_data(dataset):
    other = generate(seed=1)
    assert other.truth["sensitive_values"] != dataset.truth["sensitive_values"]


def test_synthetic_contact_data_uses_reserved_ranges(dataset):
    for value in dataset.truth["sensitive_values"]:
        if "@" in value:
            assert value.endswith("@example.com")
        if value.startswith("099-0"):
            assert len(value) == 13
