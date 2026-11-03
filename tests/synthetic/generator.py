"""合成データ生成器（設計書 08 章 0.1 の基準ケース）。

実データは使わない。名称・住所・電話番号等はすべて乱数で組み立てた架空の値で、
メールは予約済みドメイン example.com、電話番号は実在しない番号帯（099-0xxx）を使う。

構成:
- マスタ 5 種（各 100 行・10 列、備考なし）: 顧客・製品・原料・担当者・仕入先 → .xlsx
- 月次 3 種 × 5 か月（各 1,000 行・20 列、備考約 20 文字）
    売上明細 → CSV（CP932・CRLF）
    仕入明細 → CSV（UTF-8 BOM 付き・LF）
    製造原価 → .xlsx（数式あり）

受入シナリオ用の仕掛け（truth.json の "cases" に記録）:
- 顧客名と製品名に同じ文字列（S3）
- 同名で別コードの顧客（S4）
- 売上明細で略称を使う顧客（S5）
- 顧客マスタにない得意先CD（S7）
- 同じ得意先CDに表記の違う名称
- 先頭ゼロのコード、空欄、負数（返品）、小数、改行・引用符・カンマを含む備考（S8）
- 担当者コードはマスタ（xlsx）では表示形式 "0000" の数値、月次 CSV では "0012" の文字列
"""

from __future__ import annotations

import csv
import io
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

DEFAULT_SEED = 20261103
MASTER_ROWS = 100
MONTHLY_ROWS = 1000
MONTHS = [(2026, m) for m in range(4, 9)]  # 2026年4月〜8月

# ---- 名称の部品（架空） ----
_STEMS = [
    "あおば", "ひかり", "みなと", "はるか", "つばさ", "こだま", "いずみ", "かえで", "しずく", "すずらん",
    "あさひ", "ゆうなぎ", "ほしぞら", "やまびこ", "わかば", "なぎさ", "みずほ", "こはく", "あかね", "ひなた",
    "東雲", "青葉台", "白峰", "朝霧", "若草", "緑川", "銀河", "北斗", "清流", "陽光",
    "翠嶺", "瑞穂野", "紫苑", "琥珀堂", "彩雲", "星野原", "月見台", "花咲", "風見", "雪村",
]
_COMPANY_SUFFIX = ["商事", "工業", "物産", "食品", "製作所", "産業", "商会", "フーズ", "電機", "化成"]
_LEGAL_FORMS = ["株式会社", "有限会社", "合同会社"]
_PRODUCT_WORDS = ["プレミアム", "スタンダード", "ライト", "ゴールド", "クラシック", "ナチュラル", "スペシャル"]
_PRODUCT_KINDS = ["ブレンド", "ソース", "スナック", "ドリンク", "ペースト", "パウダー", "シロップ"]
_MATERIALS = [
    "小麦粉", "砂糖", "食塩", "植物油", "脱脂粉乳", "でん粉", "香料", "酸味料", "乳化剤", "大豆",
    "米粉", "ココア", "果汁", "寒天", "ゼラチン", "はちみつ", "酵母", "麦芽", "胡麻", "抹茶",
]
_SURNAMES = [
    "相川", "石森", "上野原", "江口", "大槻", "加賀谷", "北園", "久我", "小早川", "笹倉",
    "志賀", "杉浦", "瀬尾", "高森", "千早", "辻堂", "戸倉", "中津", "西園", "野々村",
]
_GIVEN = [
    "陽斗", "湊", "蒼", "結衣", "紬", "凛", "大和", "悠真", "葵", "咲良",
    "樹", "遥", "颯", "澪", "柚希", "朔", "灯", "楓", "律", "詩織",
]
_PREFS = ["東京都", "大阪府", "愛知県", "福岡県", "北海道", "宮城県", "広島県", "静岡県"]
_TOWNS = ["みなと台", "さくら丘", "ひばり野", "あけぼの町", "しらかば", "つつじヶ丘", "かもめ浜", "こもれび"]
_INDUSTRIES = ["小売", "卸売", "飲食", "製造", "サービス"]
_DEPTS = ["営業一課", "営業二課", "購買課", "製造課", "品質管理課"]
_FACTORIES = ["第一工場", "第二工場"]

# 受入シナリオ用の固定値
SHARED_NAME = "さくら"  # 顧客名と製品名の両方に使う（S3）
DUP_CUSTOMER_NAME = "株式会社ひかり商事"  # 同名で別コードの顧客 2 件（S4）
ALIAS_FULL = "株式会社あおば商事"  # 正式名（顧客マスタ）
ALIAS_SHORT = "あおば商事"  # 略称（売上明細の一部の行）（S5）
UNREGISTERED_CUSTOMER_CODES = ["009901", "009902"]  # 顧客マスタにない得意先CD（S7）
UNREGISTERED_CUSTOMER_NAME = "株式会社みらい未登録商店"


@dataclass
class Table:
    """1つの表。rows の値は str / int / Decimal / date / None / Formula / NumCode。"""

    name: str
    headers: list[str]
    rows: list[list[Any]]
    sheet: str = "Sheet1"


@dataclass(frozen=True)
class Formula:
    """xlsx の数式（"=" から始まる文字列）と、そのキャッシュ値（Excel が最後に計算した結果）。

    shared=True の数式は、同じ列の連続するセルで Excel の共有数式として書き出す。
    Excel は他のシートを参照する数式を共有数式にしない（そのようなファイルは Excel で開けない）。
    """

    text: str
    cached: Any = None
    shared: bool = False


@dataclass(frozen=True)
class NumCode:
    """数値として保存し、表示形式で先頭ゼロを付けるコード（例 12 → "0012"）。"""

    value: int
    digits: int

    def as_text(self) -> str:
        return str(self.value).zfill(self.digits)


@dataclass
class Dataset:
    masters: dict[str, Table] = field(default_factory=dict)
    monthly: dict[str, list[Table]] = field(default_factory=dict)
    truth: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 生成


class _Gen:
    def __init__(self, seed: int) -> None:
        self.r = random.Random(seed)
        self.used_names: set[str] = set()

    def pick(self, seq):
        return self.r.choice(seq)

    def unique(self, make) -> str:
        for _ in range(10_000):
            v = make()
            if v not in self.used_names:
                self.used_names.add(v)
                return v
        raise RuntimeError("could not generate a unique name")

    def company(self) -> str:
        def make():
            base = self.pick(_STEMS) + self.pick(_COMPANY_SUFFIX)
            form = self.pick(_LEGAL_FORMS)
            return f"{form}{base}" if self.r.random() < 0.7 else f"{base}{form}"

        return self.unique(make)

    def person(self) -> str:
        return self.unique(lambda: f"{self.pick(_SURNAMES)} {self.pick(_GIVEN)}")

    def address(self) -> str:
        r = self.r
        return f"{self.pick(_PREFS)}{self.pick(_TOWNS)}{r.randint(1, 9)}-{r.randint(1, 30)}-{r.randint(1, 20)}"

    def phone(self) -> str:
        return f"099-0{self.r.randint(100, 999)}-{self.r.randint(1000, 9999)}"

    def postal(self) -> str:
        return f"{self.r.randint(100, 999)}-{self.r.randint(1000, 9999)}"

    def day(self, start: date, span_days: int) -> date:
        return start + timedelta(days=self.r.randint(0, span_days - 1))


def _yen(x: Decimal) -> Decimal:
    return x.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _tax(x: Decimal) -> Decimal:
    return (x * Decimal("0.1")).quantize(Decimal("1"), rounding=ROUND_DOWN)


def _kana_of(name: str) -> str:
    # 架空データのため読みは簡略化（全角カタカナ風の記号列）
    return "カブシキガイシャ" if "株式会社" in name else "ユウゲンガイシャ"


def generate(seed: int = DEFAULT_SEED) -> Dataset:
    g = _Gen(seed)
    r = g.r
    ds = Dataset()
    truth: dict[str, Any] = {"seed": seed, "entities": {}, "sensitive_values": [], "cases": {}}

    # 固定値と乱数生成の名称が重ならないよう先に予約する
    g.used_names.update([SHARED_NAME, DUP_CUSTOMER_NAME, ALIAS_FULL, ALIAS_SHORT, UNREGISTERED_CUSTOMER_NAME])

    # ---- 顧客マスタ ----
    customers = []
    for i in range(MASTER_ROWS):
        code = f"{i * 7 + 101:06d}"  # 6 桁・先頭ゼロ
        customers.append({"code": code, "name": g.company()})
    customers[0]["name"] = ALIAS_FULL
    customers[1]["name"] = DUP_CUSTOMER_NAME
    customers[2]["name"] = DUP_CUSTOMER_NAME  # 同名・別コード
    customers[3]["name"] = SHARED_NAME  # 製品名と同じ文字列
    for c in customers:
        c.update(
            address=g.address(), phone=g.phone(), postal=g.postal(),
            email=f"info{c['code']}@example.com",
        )
    ds.masters["顧客マスタ"] = Table(
        "顧客マスタ",
        ["顧客番号", "顧客名", "顧客名カナ", "郵便番号", "住所", "電話番号", "メール", "業種",
         "取引開始日", "与信限度額"],
        [
            [c["code"], c["name"], _kana_of(c["name"]), c["postal"], c["address"], c["phone"],
             c["email"], g.pick(_INDUSTRIES), g.day(date(2015, 1, 1), 3000),
             r.choice([1_000_000, 3_000_000, 5_000_000, 10_000_000])]
            for c in customers
        ],
        sheet="顧客",
    )

    # ---- 製品マスタ ----
    products = []
    for i in range(MASTER_ROWS):
        name = g.unique(lambda: f"{g.pick(_PRODUCT_WORDS)}{g.pick(_PRODUCT_KINDS)}{r.randint(1, 99)}")
        price = Decimal(r.randint(80, 3000)) + Decimal(r.choice(["0", "0.5", "0.25"]))
        products.append({"code": f"P{i + 1:05d}", "name": name, "price": price,
                         "cost": _yen(price * Decimal("0.62"))})
    products[4]["name"] = SHARED_NAME  # 顧客名と同じ文字列
    # ---- 原料マスタ ----
    materials = [
        {"code": f"M{i + 1:04d}",
         "name": f"{_MATERIALS[i % len(_MATERIALS)]}{'' if i < len(_MATERIALS) else f'（{i // len(_MATERIALS)}種）'}",
         "price": Decimal(r.randint(50, 900))}
        for i in range(MASTER_ROWS)
    ]
    # ---- 仕入先マスタ ----
    suppliers = [{"code": str(20001 + i), "name": g.company()} for i in range(MASTER_ROWS)]
    # ---- 担当者マスタ ----
    persons = [{"code": NumCode(i + 1, 4), "name": g.person()} for i in range(MASTER_ROWS)]

    ds.masters["製品マスタ"] = Table(
        "製品マスタ",
        ["製品コード", "製品名", "製品区分", "単位", "標準単価", "標準原価", "主原料コード", "発売日",
         "販売状況", "重量g"],
        [
            [p["code"], p["name"], g.pick(_PRODUCT_KINDS), r.choice(["個", "袋", "箱", "kg"]), p["price"],
             p["cost"], g.pick(materials)["code"], g.day(date(2018, 1, 1), 2500),
             r.choice(["販売中", "販売中", "終売予定"]), r.randint(50, 2000)]
            for p in products
        ],
        sheet="製品",
    )
    ds.masters["原料マスタ"] = Table(
        "原料マスタ",
        ["原料コード", "原料名", "原料区分", "単位", "標準仕入単価", "主仕入先コード", "産地", "保管区分",
         "安全在庫", "登録日"],
        [
            [m["code"], m["name"], r.choice(["主原料", "副原料", "添加物"]), r.choice(["kg", "L", "袋"]),
             m["price"], g.pick(suppliers)["code"], r.choice(["国内", "輸入"]),
             r.choice(["常温", "冷蔵", "冷凍"]), r.randint(10, 500), g.day(date(2016, 1, 1), 3000)]
            for m in materials
        ],
        sheet="原料",
    )
    ds.masters["担当者マスタ"] = Table(
        "担当者マスタ",
        ["担当者コード", "氏名", "氏名カナ", "部署", "役職", "内線", "メール", "入社日", "拠点", "有効フラグ"],
        [
            [p["code"], p["name"], "シメイ", g.pick(_DEPTS), r.choice(["", "主任", "係長", "課長"]),
             str(r.randint(1000, 1999)), f"staff{p['code'].as_text()}@example.com",
             g.day(date(2005, 4, 1), 6000), r.choice(["本社", "東日本", "西日本"]), r.choice([1, 1, 1, 0])]
            for p in persons
        ],
        sheet="担当者",
    )
    ds.masters["仕入先マスタ"] = Table(
        "仕入先マスタ",
        ["仕入先コード", "仕入先名", "郵便番号", "住所", "電話番号", "先方担当者", "メール", "支払条件",
         "取引開始日", "評価"],
        [
            [s["code"], s["name"], g.postal(), g.address(), g.phone(), g.person(),
             f"order{s['code']}@example.com", r.choice(["月末締翌月末", "20日締翌月10日"]),
             g.day(date(2012, 1, 1), 4000), r.choice(["A", "B", "C"])]
            for s in suppliers
        ],
        sheet="仕入先",
    )

    # ---- 月次 ----
    sales_tables, purchase_tables, cost_tables = [], [], []
    for year, month in MONTHS:
        ym = f"{year}{month:02d}"
        start = date(year, month, 1)
        sales_tables.append(_sales(g, ym, start, customers, products, persons))
        purchase_tables.append(_purchases(g, ym, start, suppliers, materials, persons))
        cost_tables.append(_costs(g, ym, start, products, materials, persons))
    ds.monthly = {"売上明細": sales_tables, "仕入明細": purchase_tables, "製造原価": cost_tables}

    # ---- 正解情報 ----
    truth["entities"] = {
        "CUSTOMER": [{"code": c["code"], "name": c["name"]} for c in customers]
        + [{"code": code, "name": UNREGISTERED_CUSTOMER_NAME} for code in UNREGISTERED_CUSTOMER_CODES],
        "PRODUCT": [{"code": p["code"], "name": p["name"]} for p in products],
        "MATERIAL": [{"code": m["code"], "name": m["name"]} for m in materials],
        "SUPPLIER": [{"code": s["code"], "name": s["name"]} for s in suppliers],
        "PERSON": [{"code": p["code"].as_text(), "name": p["name"]} for p in persons],
    }
    truth["aliases"] = {"CUSTOMER": [{"code": customers[0]["code"], "alias": ALIAS_SHORT}]}
    truth["cases"] = {
        "shared_name_customer_product": SHARED_NAME,
        "duplicate_customer_name": {"name": DUP_CUSTOMER_NAME,
                                    "codes": [customers[1]["code"], customers[2]["code"]]},
        "alias": {"full": ALIAS_FULL, "short": ALIAS_SHORT, "code": customers[0]["code"]},
        "unregistered_customer_codes": UNREGISTERED_CUSTOMER_CODES,
        "person_code_numeric_in_master": True,
    }
    sensitive: set[str] = set()
    for rows in truth["entities"].values():
        for e in rows:
            if e["name"]:
                sensitive.add(e["name"])
    sensitive.add(ALIAS_SHORT)
    for c in customers:
        sensitive.update([c["address"], c["phone"], c["email"]])
    for row in ds.masters["仕入先マスタ"].rows:
        sensitive.update([row[3], row[4], row[5], row[6]])
    for row in ds.masters["担当者マスタ"].rows:
        sensitive.add(row[6])
    truth["sensitive_values"] = sorted(sensitive)
    ds.truth = truth
    return ds


def _remark(g: _Gen, customer_name: str | None, person_name: str) -> str:
    r = g.r
    surname = person_name.split(" ")[0]
    templates = [
        "",
        "",
        "特記事項なし",
        "前回同様",
        "返品処理済み",
        f"{customer_name}様より追加注文" if customer_name else "追加注文",
        f"納期調整（担当:{surname}）",
        f"{g.phone()}へ折り返し連絡",
        "梱包注意\n天地無用",
        '"至急"対応、送料込',
        "単価改定 2026/4/1適用",
        "サンプル出荷, 請求なし",
    ]
    return r.choice(templates)


def _sales(g, ym, start, customers, products, persons) -> Table:
    r = g.r
    rows = []
    for i in range(MONTHLY_ROWS):
        c = r.choice(customers)
        cust_code, cust_name = c["code"], c["name"]
        if c["name"] == ALIAS_FULL and r.random() < 0.5:
            cust_name = ALIAS_SHORT  # 略称（S5）
        if i in (10, 11):
            cust_code, cust_name = UNREGISTERED_CUSTOMER_CODES[i - 10], UNREGISTERED_CUSTOMER_NAME  # S7
        if i == 12:
            cust_code = ""  # コード欠損
        if i == 13:
            cust_name = cust_name + "　"  # 同じコードに表記の違う名称（全角空白付き）
        p = r.choice(products)
        person = r.choice(persons)
        qty = Decimal(r.randint(1, 200))
        if r.random() < 0.03:
            qty = -qty  # 返品（負数）
        if r.random() < 0.05:
            qty = qty + Decimal("0.5")  # 小数
        price = p["price"]
        amount = _yen(qty * price)
        tax = _tax(amount)
        cost = _yen(qty * p["cost"])
        d = g.day(start, 28)
        rows.append([
            f"S{ym}{i + 1:05d}", str(1), d.strftime("%Y/%m/%d"), cust_code, cust_name,
            p["code"], p["name"], str(qty), str(price), str(amount), str(tax), str(amount + tax),
            str(cost), str(amount - cost), person["code"].as_text(), person["name"],
            r.choice(["現金", "掛", "掛"]), r.choice(["1-2", "2-1", "A"]), ym,
            _remark(g, cust_name or None, person["name"]),
        ])
    return Table(
        f"売上明細_{ym}",
        ["伝票番号", "行番号", "売上日", "得意先CD", "得意先名", "製品CD", "製品名", "数量", "単価", "金額",
         "消費税", "税込金額", "原価", "粗利", "担当者CD", "担当者名", "支払区分", "区分", "取引年月", "備考"],
        rows,
    )


def _purchases(g, ym, start, suppliers, materials, persons) -> Table:
    r = g.r
    rows = []
    for i in range(MONTHLY_ROWS):
        s, m, person = r.choice(suppliers), r.choice(materials), r.choice(persons)
        qty = Decimal(r.randint(1, 500)) / Decimal(r.choice([1, 1, 10]))
        amount = _yen(qty * m["price"])
        tax = _tax(amount)
        d = g.day(start, 28)
        rows.append([
            f"P{ym}{i + 1:05d}", "1", d.isoformat(), s["code"], s["name"], m["code"], m["name"],
            str(qty), str(m["price"]), str(amount), str(tax), str(amount + tax),
            (d + timedelta(days=2)).isoformat(), person["code"].as_text(), person["name"],
            r.choice(["本社倉庫", "第二倉庫"]), (d + timedelta(days=40)).isoformat(), ym,
            r.choice(["通常", "緊急", "返品"]) if i != 5 else "",
            _remark(g, None, person["name"]),
        ])
    return Table(
        f"仕入明細_{ym}",
        ["伝票番号", "行番号", "仕入日", "仕入先番号", "仕入先名", "原料CD", "原料名", "数量", "単価", "金額",
         "消費税", "税込金額", "検収日", "担当者CD", "担当者名", "倉庫", "支払予定日", "取引年月", "区分",
         "備考"],
        rows,
    )


def _costs(g, ym, start, products, materials, persons) -> Table:
    r = g.r
    rows = []
    for i in range(MONTHLY_ROWS):
        p, m, person = r.choice(products), r.choice(materials), r.choice(persons)
        excel_row = i + 2  # 見出しが1行目
        used, price = Decimal(r.randint(10, 900)) / 10, m["price"]
        material_cost = used * price
        labor, overhead = Decimal(r.randint(5_000, 80_000)), Decimal(r.randint(1_000, 30_000))
        total = material_cost + labor + overhead
        units = r.randint(10, 1000)
        rows.append([
            f"M{ym}{i + 1:05d}", g.day(start, 28), p["code"],
            Formula(f"=VLOOKUP(C{excel_row},製品一覧!A:B,2,FALSE)", p["name"]),  # 他シート参照は共有数式にしない
            m["code"], m["name"], used, price,
            Formula(f"=G{excel_row}*H{excel_row}", material_cost, shared=True),
            labor, overhead,
            Formula(f"=SUM(I{excel_row}:K{excel_row})", total, shared=True),
            units,
            Formula(f"=ROUND(L{excel_row}/M{excel_row},2)", (total / units).quantize(Decimal("0.01"),
                                                                                  rounding=ROUND_HALF_UP),
                    shared=True),
            g.pick(_FACTORIES), f"L{r.randint(1, 4)}", person["code"].as_text(), person["name"], ym,
            _remark(g, None, person["name"]),
        ])
    return Table(
        f"製造原価_{ym}",
        ["製造番号", "製造日", "製品CD", "製品名", "原料CD", "原料名", "使用量", "原料単価", "原料費",
         "労務費", "経費", "製造原価", "生産数", "単位原価", "工場", "製造ライン", "担当者CD", "担当者名",
         "取引年月", "備考"],
        rows,
        sheet="製造原価",
    )


# ---------------------------------------------------------------- 書出し


def _cell_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, NumCode):
        return v.as_text()
    if isinstance(v, (date, datetime)):
        return v.strftime("%Y/%m/%d")
    if isinstance(v, Formula):
        raise TypeError("formulas cannot be written to CSV")
    return str(v)


def to_csv_bytes(table: Table, encoding: str, newline: str, bom: bool = False) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator=newline, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(table.headers)
    for row in table.rows:
        writer.writerow([_cell_text(v) for v in row])
    data = buf.getvalue().encode(encoding)
    if bom:
        data = "﻿".encode(encoding) + data
    return data


def write_xlsx(path: Path, sheets: list[Table]) -> None:
    from tests.synthetic.ooxml import BookSpec, SheetSpec, build_xlsx

    book = BookSpec([SheetSpec(t.sheet, [list(t.headers)] + [list(r) for r in t.rows]) for t in sheets])
    path.write_bytes(build_xlsx(book))


def write_dataset(ds: Dataset, out_dir: Path) -> list[Path]:
    """基準ケースのファイル一式と truth.json を書き出し、作ったファイルの一覧を返す。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, table in ds.masters.items():
        path = out_dir / f"{name}.xlsx"
        write_xlsx(path, [table])
        written.append(path)
    product_list = Table("製品一覧", ["製品コード", "製品名"],
                         [[row[0], row[1]] for row in ds.masters["製品マスタ"].rows], sheet="製品一覧")
    for table in ds.monthly["売上明細"]:
        path = out_dir / f"{table.name}.csv"
        path.write_bytes(to_csv_bytes(table, "cp932", "\r\n"))
        written.append(path)
    for table in ds.monthly["仕入明細"]:
        path = out_dir / f"{table.name}.csv"
        path.write_bytes(to_csv_bytes(table, "utf-8", "\n", bom=True))
        written.append(path)
    for table in ds.monthly["製造原価"]:
        path = out_dir / f"{table.name}.xlsx"
        write_xlsx(path, [table, product_list])
        written.append(path)
    truth_path = out_dir / "truth.json"
    truth_path.write_text(json.dumps(ds.truth, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    written.append(truth_path)
    return written
