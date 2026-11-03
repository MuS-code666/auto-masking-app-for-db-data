"""受入シナリオ S9・S10・S15 用の合成ブック（xlsx）。

顧客の名称・コードは基準ケースの顧客マスタから取る（S3〜S5 の仕掛けを含む）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tests.synthetic.generator import ALIAS_FULL, DUP_CUSTOMER_NAME, SHARED_NAME, Dataset, Formula
from tests.synthetic.ooxml import BookSpec, Comment, ExtraPart, RichText, SheetSpec

CUSTOMER_SHEET_HEADERS = ["顧客番号", "顧客名"]
SALES_SHEET_HEADERS = ["売上日", "得意先CD", "得意先名", "金額"]
SUMMARY_HEADERS = ["項目", "値"]


def customers(ds: Dataset, n: int = 20) -> list[tuple[str, str]]:
    """先頭は 正式名（あおば）→ 同名2件（ひかり）→ さくら → その他。"""
    rows = ds.masters["顧客マスタ"].rows
    picked = [r for r in rows if r[1] == ALIAS_FULL]
    picked += [r for r in rows if r[1] == DUP_CUSTOMER_NAME]
    picked += [r for r in rows if r[1] == SHARED_NAME]
    picked += [r for r in rows if r not in picked][: n - len(picked)]
    return [(r[0], r[1]) for r in picked]


def customer_sheet(ds: Dataset, name: str = "顧客") -> SheetSpec:
    return SheetSpec(name, [CUSTOMER_SHEET_HEADERS] + [[c, n] for c, n in customers(ds)])


@dataclass(frozen=True)
class FormulaBookFacts:
    aoba_total: Decimal
    grand_total: Decimal


def formula_book(ds: Dataset) -> tuple[BookSpec, FormulaBookFacts]:
    """S15：参照系・条件集計・条件分岐・他シート参照・文字列操作・部分一致の数式。"""
    custs = customers(ds)
    sales = [SALES_SHEET_HEADERS]
    aoba_total = Decimal(0)
    grand_total = Decimal(0)
    for i in range(30):
        code, name = custs[i % len(custs)]
        amount = Decimal(1000 + i * 37)
        grand_total += amount
        if name == ALIAS_FULL:
            aoba_total += amount
        row = i + 2
        sales.append([date(2026, 4, 1 + i % 28), code,
                      Formula(f"=VLOOKUP(B{row},顧客!A:B,2,FALSE)", name), amount])
    summary = [
        SUMMARY_HEADERS,
        ["あおば合計", Formula(f'=SUMIF(売上!C:C,"{ALIAS_FULL}",売上!D:D)', aoba_total)],
        ["判定", Formula(f'=IF(売上!C2="{ALIAS_FULL}","重点","")', "重点")],
        ["参照", Formula("=顧客!B2", custs[0][1])],
        ["同名参照", Formula("=顧客!B3", custs[1][1])],
        ["先頭", Formula("=LEFT(顧客!B2,4)", custs[0][1][:4])],
        ["件数", Formula(f'=COUNTIF(顧客!B:B,"*{SHARED_NAME}*")', 1)],
        ["合計", Formula("=SUM(売上!D:D)", grand_total)],
    ]
    book = BookSpec([customer_sheet(ds), SheetSpec("売上", sales), SheetSpec("集計", summary)])
    return book, FormulaBookFacts(aoba_total, grand_total)


# ---------------------------------------------------------------- S9：残存リスク


def risk_book(ds: Dataset, risk: str) -> BookSpec:
    """顧客シート（置換対象）に、元の名称が残り得る場所を1つ加えたブック。"""
    custs = customers(ds)
    name = custs[0][1]
    sheets = [customer_sheet(ds)]
    book = BookSpec(sheets)
    if risk == "comment":
        sheets[0].comments = [Comment("B2", "合成 太郎", f"{name}は重点顧客")]
    elif risk == "hidden_sheet":
        sheets.append(SheetSpec("メモ", [["メモ"], [f"{name}と商談中"]], state="hidden"))
    elif risk == "sheet_name":
        sheets.append(SheetSpec(name, [["メモ"], ["特記事項なし"]]))
    elif risk == "defined_name":
        book.defined_names = [("重点顧客", f'"{name}"')]
    elif risk == "header_footer":
        sheets[0].header_footer = f"&L{name}様向け資料"
    elif risk == "validation_list":
        sheets[0].validation_list = ("C2:C10", f'"{custs[3][1]},{custs[4][1]}"')
    elif risk == "properties":
        book.creator = name
        book.company = custs[5][1]
        book.abs_path = f"C:\\Users\\共有\\{name}\\"
        book.thumbnail = True
    elif risk == "rich_text":
        sheets[0].rows[1][1] = RichText(((name[:4], True), (name[4:], False)), phonetic="カブシキガイシャ")
    else:
        raise ValueError(risk)
    return book


# ---------------------------------------------------------------- S10：未対応の機能

_PIVOT = ("xl/pivotCache/pivotCacheDefinition1.xml",
          "application/vnd.openxmlformats-officedocument.spreadsheetml.pivotCacheDefinition+xml",
          "http://schemas.openxmlformats.org/officeDocument/2006/relationships/pivotCacheDefinition")
_EXTERNAL = ("xl/externalLinks/externalLink1.xml",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.externalLink+xml",
             "http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink")
_CONNECTIONS = ("xl/connections.xml",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.connections+xml",
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/connections")
_MACRO = ("xl/vbaProject.bin", "application/vnd.ms-office.vbaProject",
          "http://schemas.microsoft.com/office/2006/relationships/vbaProject")
_UNKNOWN = ("xl/mystery/part1.xml", "application/vnd.example.unknown+xml", None)

UNSUPPORTED = {"pivot": _PIVOT, "external_link": _EXTERNAL, "connection": _CONNECTIONS, "macro": _MACRO,
               "unknown_part": _UNKNOWN}


def feature_book(ds: Dataset, feature: str) -> BookSpec:
    custs = customers(ds)
    sheets = [customer_sheet(ds)]
    book = BookSpec(sheets)
    if feature in UNSUPPORTED:
        part, ct, rel = UNSUPPORTED[feature]
        payload = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><x xmlns="urn:example">'
                   + custs[0][1].encode() + b"</x>")
        book.extra_parts = [ExtraPart(part, ct, payload, rel)]
    elif feature == "comments":
        sheets[0].comments = [Comment("B2", "合成 太郎", "確認済み")]
    elif feature == "chart":
        chart = SheetSpec("グラフ", [["顧客", "金額"]] + [[n, 100 + i] for i, (_, n) in enumerate(custs[:5])],
                          chart_title="顧客別金額")
        sheets.append(chart)
    elif feature == "image":
        sheets[0].image = True
    else:
        raise ValueError(feature)
    return book
