"""受入シナリオ用の共通設定：合成データの CSV 一式と、それに合う変換設定。"""

from __future__ import annotations

from pathlib import Path

from maskapp.app import Workbench
from maskapp.config import Action, ColumnSpec, RelationSpec, Role, TemplateSpec
from maskapp.transform.reviews import Decision, ReviewKind
from maskapp.verify.checks import VerifyReport
from tests.synthetic.generator import ALIAS_SHORT, Dataset, Table, to_csv_bytes


def keep(h: str) -> ColumnSpec:
    return ColumnSpec(h)


def clear(h: str) -> ColumnSpec:
    return ColumnSpec(h, Action.CLEAR)


def code(h: str, etype: str, scheme: str, binding: str | None = None) -> ColumnSpec:
    return ColumnSpec(h, Action.REPLACE, entity_type=etype, role=Role.CODE, code_scheme=scheme, binding=binding)


def name(h: str, etype: str, binding: str | None = None) -> ColumnSpec:
    return ColumnSpec(h, Action.REPLACE, entity_type=etype, role=Role.NAME, binding=binding)


def value(h: str, info_type: str) -> ColumnSpec:
    return ColumnSpec(h, Action.REPLACE, info_type=info_type, role=Role.VALUE)


TEMPLATES = [
    TemplateSpec("顧客マスタ", [
        code("顧客番号", "CUSTOMER", "顧客コード"), name("顧客名", "CUSTOMER"), clear("顧客名カナ"),
        value("郵便番号", "POSTAL"), clear("住所"), value("電話番号", "PHONE"), value("メール", "EMAIL"),
        keep("業種"), keep("取引開始日"), keep("与信限度額"),
    ]),
    TemplateSpec("製品マスタ", [
        code("製品コード", "PRODUCT", "製品コード"), name("製品名", "PRODUCT"), keep("製品区分"), keep("単位"),
        keep("標準単価"), keep("標準原価"), code("主原料コード", "MATERIAL", "原料コード"), keep("発売日"),
        keep("販売状況"), keep("重量g"),
    ]),
    TemplateSpec("原料マスタ", [
        code("原料コード", "MATERIAL", "原料コード"), name("原料名", "MATERIAL"), keep("原料区分"), keep("単位"),
        keep("標準仕入単価"), code("主仕入先コード", "SUPPLIER", "仕入先コード"), keep("産地"), keep("保管区分"),
        keep("安全在庫"), keep("登録日"),
    ]),
    TemplateSpec("担当者マスタ", [
        code("担当者コード", "PERSON", "担当者コード"), name("氏名", "PERSON"), clear("氏名カナ"), keep("部署"),
        keep("役職"), clear("内線"), value("メール", "EMAIL"), keep("入社日"), keep("拠点"), keep("有効フラグ"),
    ]),
    TemplateSpec("仕入先マスタ", [
        code("仕入先コード", "SUPPLIER", "仕入先コード"), name("仕入先名", "SUPPLIER"), value("郵便番号", "POSTAL"),
        clear("住所"), value("電話番号", "PHONE"), clear("先方担当者"), value("メール", "EMAIL"),
        keep("支払条件"), keep("取引開始日"), keep("評価"),
    ]),
    TemplateSpec("売上明細", [
        keep("伝票番号"), keep("行番号"), keep("売上日"), code("得意先CD", "CUSTOMER", "顧客コード"),
        name("得意先名", "CUSTOMER"), code("製品CD", "PRODUCT", "製品コード"), name("製品名", "PRODUCT"),
        keep("数量"), keep("単価"), keep("金額"), keep("消費税"), keep("税込金額"), keep("原価"), keep("粗利"),
        code("担当者CD", "PERSON", "担当者コード"), name("担当者名", "PERSON"), keep("支払区分"), keep("区分"),
        keep("取引年月"), clear("備考"),
    ]),
    TemplateSpec("仕入明細", [
        keep("伝票番号"), keep("行番号"), keep("仕入日"), code("仕入先番号", "SUPPLIER", "仕入先コード"),
        name("仕入先名", "SUPPLIER"), code("原料CD", "MATERIAL", "原料コード"), name("原料名", "MATERIAL"),
        keep("数量"), keep("単価"), keep("金額"), keep("消費税"), keep("税込金額"), keep("検収日"),
        code("担当者CD", "PERSON", "担当者コード"), name("担当者名", "PERSON"), keep("倉庫"), keep("支払予定日"),
        keep("取引年月"), keep("区分"), clear("備考"),
    ]),
]

RELATIONS = [
    RelationSpec("売上明細", "得意先CD", "顧客マスタ", "顧客番号"),
    RelationSpec("売上明細", "製品CD", "製品マスタ", "製品コード"),
    RelationSpec("売上明細", "担当者CD", "担当者マスタ", "担当者コード"),
    RelationSpec("仕入明細", "仕入先番号", "仕入先マスタ", "仕入先コード"),
    RelationSpec("仕入明細", "原料CD", "原料マスタ", "原料コード"),
    RelationSpec("製品マスタ", "主原料コード", "原料マスタ", "原料コード"),
]

# マスタの CSV 形式（文字コード・改行・BOM の違いを混ぜる）
MASTER_FORMATS = {
    "顧客マスタ": ("cp932", "\r\n", False),
    "製品マスタ": ("utf-8", "\n", True),
    "原料マスタ": ("utf-8", "\r\n", False),
    "担当者マスタ": ("cp932", "\n", False),
    "仕入先マスタ": ("utf-8", "\r\n", True),
}
MASTER_ORDER = ["顧客マスタ", "製品マスタ", "原料マスタ", "担当者マスタ", "仕入先マスタ"]


def write_table(table: Table, path: Path, encoding: str, newline: str, bom: bool) -> Path:
    path.write_bytes(to_csv_bytes(table, encoding, newline, bom))
    return path


def write_csv_dataset(ds: Dataset, out: Path) -> dict[str, list[Path]]:
    """マスタ5種と月次（売上・仕入）を CSV で書き出す。"""
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, list[Path]] = {}
    for name_, table in ds.masters.items():
        files[name_] = [write_table(table, out / f"{name_}.csv", *MASTER_FORMATS[name_])]
    files["売上明細"] = [write_table(t, out / f"{t.name}.csv", "cp932", "\r\n", False) for t in ds.monthly["売上明細"]]
    files["仕入明細"] = [write_table(t, out / f"{t.name}.csv", "utf-8", "\n", True) for t in ds.monthly["仕入明細"]]
    return files


def resolve_reviews(wb: Workbench, run_id: int) -> dict[str, int]:
    """要確認を合成データの意図どおりに判断する。判断した件数を種類別に返す。"""
    counts: dict[str, int] = {}
    for _ in range(5):  # 判断で新しい要確認が出ることがあるため繰り返す
        items = wb.reviews(run_id)
        if not items:
            break
        for item in items:
            counts[item.kind.value] = counts.get(item.kind.value, 0) + 1
            if item.kind is ReviewKind.NEW_ENTITY:
                continue
            if item.kind is ReviewKind.CODE_MULTI_NAME:
                decision = Decision.ADD_ALIAS if item.subject["name"] == ALIAS_SHORT else Decision.KEEP_AS_SOURCE_ERROR
                wb.decide(item.id, decision)
            elif item.kind in (ReviewKind.NAME_AMBIGUOUS, ReviewKind.SIMILAR_NAME):
                wb.decide(item.id, Decision.LINK_EXISTING, item.subject["candidates"][0]["entity_id"])
            elif item.kind is ReviewKind.RESIDUAL_SHORT:
                wb.decide(item.id, Decision.FALSE_POSITIVE)
        wb.decide_all(run_id, ReviewKind.NEW_ENTITY, Decision.REGISTER_NEW)
        wb.plan(run_id)
    return counts


def run_through(wb: Workbench, run_id: int) -> tuple[VerifyReport, list[Path]]:
    """計画 → 判断 → 書出し → 検証 を合格するまで（最大3回）行う。"""
    wb.plan(run_id)
    for _ in range(3):
        resolve_reviews(wb, run_id)
        staged = wb.stage(run_id)
        report = wb.verify(run_id)
        if report.ok or any(f.kind not in ("open_reviews", "residual_short") for f in report.blocking):
            break
        wb.plan(run_id)
    return report, staged


def add_files(wb: Workbench, run_id: int, paths: list[Path]) -> list[int]:
    ids = []
    for path in paths:
        file_id = wb.import_csv(path).id
        wb.add_input(run_id, file_id)
        ids.append(file_id)
    return ids
