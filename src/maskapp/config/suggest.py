"""設定の候補づくり（設計書 04 章 3〜4）：列設定の推定、既存の表の定義との差分、関連の候補。

ここで作るのは「候補」だけで、利用者が画面で確認して確定する。推定は列名と値の形による規則で行う
（AI による推定は段階5）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from maskapp.config.model import (
    Action,
    ColumnSpec,
    Config,
    RelationSpec,
    Role,
    TemplateSpec,
    normalize_header,
)

# 種類の判定に使う列名の語（先に書いたものを優先。「仕入先」は「先」より前に判定する）
ENTITY_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("SUPPLIER", ("仕入先", "購買先", "発注先", "外注先", "業者", "メーカー")),
    ("CUSTOMER", ("顧客", "得意先", "取引先", "納品先", "請求先", "出荷先", "客")),
    ("MATERIAL", ("原料", "材料", "資材", "原材料")),
    ("PRODUCT", ("製品", "商品", "品目", "品名", "品番")),
    ("PERSON", ("担当者", "担当", "社員", "従業員", "氏名", "作成者", "営業")),
]
CODE_SCHEMES = {
    "CUSTOMER": "顧客コード", "PRODUCT": "製品コード", "MATERIAL": "原料コード",
    "SUPPLIER": "仕入先コード", "PERSON": "担当者コード",
}
_CODE_WORDS = ("コード", "cd", "番号", "no", "id", "code", "№")
_NAME_WORDS = ("名称", "氏名", "名", "name")
_CLEAR_WORDS = ("備考", "メモ", "コメント", "摘要", "特記", "カナ", "フリガナ", "ふりがな", "住所", "所在地", "内線")
_VALUE_WORDS = [("EMAIL", ("メール", "mail", "e-mail")), ("PHONE", ("電話", "tel", "fax", "携帯")),
                ("POSTAL", ("郵便", "〒"))]
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^0\d{1,4}-\d{1,4}-\d{3,4}$")
_POSTAL = re.compile(r"^\d{3}-\d{4}$")


def _norm(text: str) -> str:
    return normalize_header(text).casefold()


def _entity_type(header: str) -> str | None:
    h = _norm(header)
    for etype, words in ENTITY_WORDS:
        if any(w.casefold() in h for w in words):
            return etype
    return None


def _value_type(header: str, samples: Sequence[str]) -> str | None:
    h = _norm(header)
    for info_type, words in _VALUE_WORDS:
        if any(w in h for w in words):
            return info_type
    values = [v for v in samples if v]
    if values:
        for info_type, pattern in (("EMAIL", _EMAIL), ("PHONE", _PHONE), ("POSTAL", _POSTAL)):
            if sum(1 for v in values if pattern.match(v)) >= max(1, len(values) * 0.8):
                return info_type
    return None


def suggest_column(header: str, samples: Sequence[str] = ()) -> ColumnSpec:
    """1列の設定の候補（結合の組は suggest_columns で決める）。"""
    h = _norm(header)
    if any(w.casefold() in h for w in _CLEAR_WORDS) and not h.endswith(("コード", "cd")):
        return ColumnSpec(header, Action.CLEAR)
    info_type = _value_type(header, samples)
    if info_type:
        return ColumnSpec(header, Action.REPLACE, info_type=info_type, role=Role.VALUE)
    etype = _entity_type(header)
    if etype:
        if any(h.endswith(w) or w in ("cd", "コード") and w in h for w in _CODE_WORDS):
            return ColumnSpec(header, Action.REPLACE, entity_type=etype, role=Role.CODE,
                              code_scheme=CODE_SCHEMES.get(etype, f"{etype}コード"))
        words = next(ws for t, ws in ENTITY_WORDS if t == etype)
        if any(w in h for w in _NAME_WORDS) or any(h.endswith(w.casefold()) for w in words):
            return ColumnSpec(header, Action.REPLACE, entity_type=etype, role=Role.NAME)
    return ColumnSpec(header)


def suggest_columns(headers: Sequence[str], samples: Sequence[Sequence[str]] = ()) -> list[ColumnSpec]:
    """列設定の候補。同じ種類のコード列が複数ある表（請求先・納品先 等）では、名称列を直前のコード列と組にする。"""
    specs = [suggest_column(h, samples[i] if i < len(samples) else ()) for i, h in enumerate(headers)]
    groups: dict[str, list[dict]] = {}
    out: list[ColumnSpec] = []
    for spec in specs:
        if spec.role in (Role.CODE, Role.NAME) and spec.entity_type:
            etype = spec.entity_type
            lst = groups.setdefault(etype, [])
            target = next((g for g in reversed(lst) if spec.role not in g["roles"]), None) if lst else None
            if target is None or (spec.role is Role.CODE and target is not lst[-1]):
                target = {"roles": set(), "key": None if not lst else f"{etype}#{len(lst) + 1}"}
                lst.append(target)
            target["roles"].add(spec.role)
            if target["key"]:
                spec = ColumnSpec(spec.header, spec.action, spec.info_type, spec.entity_type, spec.role,
                                  spec.code_scheme, target["key"])
        out.append(spec)
    return out


def suggest_template(name: str, rows: list[list[str]], header_row: int = 0) -> TemplateSpec:
    headers = rows[header_row] if header_row < len(rows) else []
    while headers and not headers[-1]:
        headers = headers[:-1]
    samples = [[r[i] if i < len(r) else "" for r in rows[header_row + 1:header_row + 51]] for i in range(len(headers))]
    columns = suggest_columns(headers, samples)
    # 見出しが空・重複する列は名前を補う（表の定義では列名が一意である必要がある）
    seen: dict[str, int] = {}
    fixed = []
    for i, c in enumerate(columns):
        header = c.header or f"列{i + 1}"
        if header in seen:
            seen[header] += 1
            header = f"{header}_{seen[header]}"
        else:
            seen[header] = 1
        fixed.append(c if header == c.header else ColumnSpec(header, c.action, c.info_type, c.entity_type, c.role,
                                                               c.code_scheme, c.binding))
    return TemplateSpec(name, fixed, header_row)


# ---------------------------------------------------------------- 既存の表の定義との差分


@dataclass(frozen=True)
class HeaderDiff:
    added: list[str] = field(default_factory=list)  # ファイルにあって定義にない列
    removed: list[str] = field(default_factory=list)  # 定義にあってファイルにない列
    renamed: list[tuple[str, str]] = field(default_factory=list)  # 名前が変わった可能性のある列（旧, 新）

    @property
    def same(self) -> bool:
        return not (self.added or self.removed or self.renamed)

    @property
    def status(self) -> str:
        if self.same:
            return "一致"
        parts = []
        if self.renamed:
            parts.append("列名が変わっています")
        if self.added:
            parts.append("列が追加されています")
        if self.removed:
            parts.append("列がなくなっています")
        return "・".join(parts)


def _trim(row: list[str]) -> list[str]:
    row = list(row)
    while row and not row[-1]:
        row.pop()
    return row


def compare_headers(template: TemplateSpec, headers: list[str]) -> HeaderDiff:
    old = [normalize_header(h) for h in template.headers]
    new = [normalize_header(h) for h in _trim(headers)]
    added_idx = [i for i, h in enumerate(new) if h not in old]
    removed_idx = [i for i, h in enumerate(old) if h not in new]
    renamed = []
    # 同じ位置で、片方にしかない列どうしは改名の可能性がある
    for i in list(added_idx):
        if i in removed_idx:
            renamed.append((template.headers[i], _trim(headers)[i]))
            added_idx.remove(i)
            removed_idx.remove(i)
    return HeaderDiff([_trim(headers)[i] for i in added_idx], [template.headers[i] for i in removed_idx], renamed)


@dataclass(frozen=True)
class TemplateMatch:
    name: str | None
    diff: HeaderDiff | None
    score: float  # 見出しの一致の度合い（0〜1）

    @property
    def status(self) -> str:
        if self.name is None:
            return "新しい表"
        return self.diff.status


def best_template(config: Config, rows: list[list[str]], threshold: float = 0.5) -> TemplateMatch:
    """見出しが最もよく一致する表の定義。完全一致がなければ、一致の度合いがしきい値以上のものを差分付きで返す。"""
    best = TemplateMatch(None, None, 0.0)
    for t in config.templates.values():
        if t.header_row >= len(rows):
            continue
        headers = _trim(rows[t.header_row])
        old = {normalize_header(h) for h in t.headers}
        new = {normalize_header(h) for h in headers}
        if not old or not new:
            continue
        score = len(old & new) / len(old | new)
        if score > best.score:
            best = TemplateMatch(t.name, compare_headers(t, headers), score)
    if best.score < threshold:
        return TemplateMatch(None, None, best.score)
    return best


def adapt_template(template: TemplateSpec, headers: list[str]) -> tuple[TemplateSpec, list[str]]:
    """既存の定義を新しい見出しに合わせた定義と、確認が必要な列（追加・改名）を返す。"""
    diff = compare_headers(template, headers)
    renamed = {new: old for old, new in diff.renamed}
    by_header = {normalize_header(c.header): c for c in template.columns}
    columns, unconfirmed = [], []
    for h in _trim(headers):
        spec = by_header.get(normalize_header(h))
        if spec is None and h in renamed:
            old = by_header[normalize_header(renamed[h])]
            spec = ColumnSpec(h, old.action, old.info_type, old.entity_type, old.role, old.code_scheme, old.binding)
            unconfirmed.append(h)
        elif spec is None:
            spec = suggest_column(h)
            unconfirmed.append(h)
        else:
            spec = ColumnSpec(h, spec.action, spec.info_type, spec.entity_type, spec.role, spec.code_scheme,
                              spec.binding)
        columns.append(spec)
    return TemplateSpec(template.name, columns, template.header_row, template.skip_total_rows,
                        template.outside_action), unconfirmed


# ---------------------------------------------------------------- 関連の候補


@dataclass(frozen=True)
class RelationCandidate:
    relation: RelationSpec
    from_values: int  # 参照元の値のある行数
    matched: int  # 参照先にある行数
    unmatched_keys: int  # 参照先にない値の種類数
    duplicate_target_keys: int  # 参照先で重複している値の種類数

    @property
    def overlap(self) -> float:
        return self.matched / self.from_values if self.from_values else 0.0


def relation_stats(from_values: list[str], to_values: list[str]) -> tuple[int, int, int, int]:
    to_counts: dict[str, int] = {}
    for v in to_values:
        if v:
            to_counts[v] = to_counts.get(v, 0) + 1
    nonblank = [v for v in from_values if v]
    matched = sum(1 for v in nonblank if v in to_counts)
    unmatched = len({v for v in nonblank if v not in to_counts})
    dup = sum(1 for c in to_counts.values() if c > 1)
    return len(nonblank), matched, unmatched, dup


def suggest_relations(config: Config, values: dict[tuple[str, str], list[str]],
                      min_overlap: float = 0.5) -> list[RelationCandidate]:
    """同じコード体系のコード列どうしで、値の重なりから関連の候補を作る（自動では確定しない）。

    values[(表の定義名, 列名)] = その列の値（作業内の全ファイル分）。参照先は値がほぼ一意の列とする。
    """
    code_cols = [
        (t.name, c.header, c.code_scheme) for t in config.templates.values() for c in t.columns
        if c.action is Action.REPLACE and c.role is Role.CODE
    ]
    existing = {(r.from_template, r.from_column, r.to_template, r.to_column) for r in config.relations}
    out = []
    for ft, fc, fs in code_cols:
        for tt, tc, ts in code_cols:
            if ft == tt or fs != ts or (ft, fc) not in values or (tt, tc) not in values:
                continue
            fv, tv = values[(ft, fc)], values[(tt, tc)]
            n, matched, unmatched, dup = relation_stats(fv, tv)
            to_distinct = len({v for v in tv if v})
            if not n or not to_distinct or dup > to_distinct * 0.05:
                continue  # 参照先が一意でない
            from_distinct = len({v for v in fv if v})
            if from_distinct == n and to_distinct < from_distinct:
                continue  # 両方一意なら、種類の多い方を参照先にする
            if from_distinct == n and to_distinct == from_distinct and (ft, fc) > (tt, tc):
                continue
            if matched / n < min_overlap or (ft, fc, tt, tc) in existing:
                continue
            out.append(RelationCandidate(RelationSpec(ft, fc, tt, tc), n, matched, unmatched, dup))
    return sorted(out, key=lambda c: (-c.overlap, c.relation.from_template, c.relation.from_column))
