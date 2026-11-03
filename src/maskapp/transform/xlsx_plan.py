"""xlsx のブック全体の変換計画（設計書 06 章 3〜4）。

表の単位の計画（planner）で決めたセルの値に加えて、次を決める。
- 機能の扱い：未対応の部品は停止、コメントは除去を選ぶまで停止、画像は確認するまで停止 等
- 数式：文字列定数が登録語句と完全に一致し置換先が1つに決まるものは置換する。部分一致・
  置換先が複数・外部ブック参照は停止（「値に変換」を選べば数式を値にして出力）
- キャッシュ値：文字列のキャッシュ値が登録語句と完全に一致すれば置換し、一部に含む場合は
  キャッシュ値を消す（Excel が開いたときに再計算する）
- 文字列を加工する数式（LEFT・& 等）は、再計算の結果が変わり得るため警告する
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields

from maskapp.formats.xlsx.book import BookEdits, XlsxBook, cell_ref
from maskapp.formats.xlsx.formula import FormulaInfo, FormulaSyntaxError, analyze, replace_constants
from maskapp.transform.planner import Plan, PlanNote, RunFile
from maskapp.verify.residual import ResidualScanner, dictionary_terms, replacement_map

_SAMPLE = 20  # 注記に載せるセル位置の数


@dataclass(frozen=True)
class BookOptions:
    """利用者が選ぶ xlsx の扱い。既定はすべて「選ばない」（＝該当があれば停止）。"""

    remove_comments: bool = False  # コメント（メモ・スレッド形式）を除去して出力
    remove_charts: bool = False  # グラフを除去して出力
    accept_images: bool = False  # 画像の中身は検査されないことを確認して出力
    formulas_to_values: bool = False  # 自動で置換できない数式を値に変換して出力

    @classmethod
    def from_json(cls, data: dict) -> BookOptions:
        names = {f.name for f in fields(cls)}
        unknown = set(data) - names
        if unknown:
            raise ValueError("unknown xlsx option")
        return cls(**{k: bool(v) for k, v in data.items()})

    def to_json(self) -> dict:
        return asdict(self)


Key = tuple[int, int, int]  # (シート, 行, 列)


@dataclass
class BookPlan:
    file_id: int
    edits: BookEdits
    notes: list[PlanNote]
    generated: set[Key] = field(default_factory=set)  # 計画で作った値のセル（残存検査の対象外）
    blocked_formulas: set[Key] = field(default_factory=set)  # 停止の注記を出した数式セル


def _cells(keys: list[Key]) -> list[dict]:
    return [{"sheet": s, "cell": cell_ref(r, c)} for s, r, c in keys[:_SAMPLE]]


def _feature_notes(book: XlsxBook, options: BookOptions, edits: BookEdits, loc: dict) -> list[PlanNote]:
    notes: list[PlanNote] = []
    groups: dict[tuple[str, str], int] = defaultdict(int)
    for info in book.pkg.parts():
        groups[(info.feature, info.handling)] += 1
    for (feature, handling), count in sorted(groups.items()):
        detail = {**loc, "feature": feature, "parts": count}
        if handling == "block":
            notes.append(PlanNote("source", "block", "unsupported_feature", detail))
        elif handling == "comment":
            if options.remove_comments:
                edits.remove_comments = True
                notes.append(PlanNote("source", "info", "comments_removed", detail))
            else:
                notes.append(PlanNote("source", "block", "comments_present", detail))
        elif handling == "chart":
            if options.remove_charts:
                edits.remove_charts = True
                notes.append(PlanNote("source", "info", "charts_removed", detail))
            else:
                notes.append(PlanNote("source", "info", "charts_present", detail))
        elif handling == "image":
            if options.accept_images:
                notes.append(PlanNote("source", "warn", "images_not_inspected", detail))
            else:
                notes.append(PlanNote("source", "block", "images_need_confirmation", detail))
        elif handling == "binary":
            notes.append(PlanNote("source", "info", "binary_not_inspected", detail))
        elif handling == "remove":
            notes.append(PlanNote("source", "info", "part_removed", detail))
        elif feature == "properties":
            notes.append(PlanNote("source", "info", "properties_reset", detail))
    for sheet in book.sheets:
        if sheet.state != "visible":
            notes.append(PlanNote("source", "info", "hidden_sheet", {**loc, "sheet": sheet.idx,
                                                                     "state": sheet.state}))
    return notes


def plan_book(conn, plan: Plan, rf: RunFile, book: XlsxBook) -> BookPlan:
    options = BookOptions.from_json(rf.options)
    loc = {"file_id": rf.file_id}
    edits = BookEdits()
    notes = _feature_notes(book, options, edits, loc)
    bp = BookPlan(rf.file_id, edits, notes)

    # 1. 表の単位で決めたセルの値
    rich: list[Key] = []
    for fp in plan.units_of(rf.file_id):
        sheet = book.sheet(fp.sheet)
        for (r, c), a in fp.changes.items():
            cell = sheet.cells.get((r, c))
            if a.value is None or cell is None:
                continue
            key = (fp.sheet, r, c)
            edit = edits.edit(*key)
            if a.kind == "clear":
                edit.clear = True
                if cell.formula is not None:
                    edits.drop_calc_chain = True
            else:
                edit.value = a.value
                bp.generated.add(key)
                if cell.rich:
                    rich.append(key)
    if rich:
        notes.append(PlanNote("source", "warn", "rich_text_flattened", {**loc, "count": len(rich),
                                                                       "cells": _cells(rich)}))

    # 2. 数式
    mapping = replacement_map(conn)
    scanner = ResidualScanner(dictionary_terms(conn))

    def classify(value: str) -> str | None:
        """語句としての扱い：置換先が1つ → その値、それ以外で登録語句を含む → ""、無関係 → None。"""
        targets = mapping.get(value)
        if targets:
            return next(iter(targets)) if len(targets) == 1 else ""
        if any(not h.short for h in scanner.scan(value)):
            return ""
        return None

    analyses: dict[str, FormulaInfo | None] = {}
    found: dict[str, list[Key]] = defaultdict(list)
    has_formula = False
    for sheet in book.sheets:
        masters = {
            cell.formula.si: cell.formula.text for cell in sheet.cells.values()
            if cell.formula is not None and cell.formula.kind == "shared" and cell.formula.text is not None
        }
        for (r, c), cell in sorted(sheet.cells.items()):
            f = cell.formula
            if f is None:
                continue
            has_formula = True
            key = (sheet.idx, r, c)
            text = f.text if f.text is not None else masters.get(f.si)
            if text is None or key in edits.cells and edits.cells[key].clear:
                continue
            if text not in analyses:
                try:
                    analyses[text] = analyze(text)
                except FormulaSyntaxError:
                    analyses[text] = None
            info = analyses[text]
            if info is None:
                found["formula_unparsable"].append(key)
                continue
            if info.manipulates_text:
                found["formula_text_function"].append(key)
            problem = "formula_external_ref" if info.external else None
            replacements: dict[int, str] = {}
            for token in info.constants:
                result = classify(token.value)
                if result:
                    replacements[token.start] = result
                elif result == "":
                    problem = problem or (
                        "formula_constant_ambiguous" if token.value in mapping else "formula_constant_partial"
                    )
            if problem:
                if options.formulas_to_values:
                    edits.edit(*key).to_value = True
                    edits.drop_calc_chain = True
                    found["formula_converted_to_value"].append(key)
                else:
                    found[problem].append(key)
                    bp.blocked_formulas.add(key)
            elif replacements and f.text is not None:
                edits.edit(*key).formula = replace_constants(text, info, replacements)
                found["formula_constant_replaced"].append(key)

            # キャッシュ値（表の単位で値を決めたセルは除く）
            if key in bp.generated or cell.kind != "string" or not cell.text:
                continue
            result = classify(cell.text)
            if result:
                edits.edit(*key).value = result
                bp.generated.add(key)
                found["formula_cache_replaced"].append(key)
            elif result == "":
                edit = edits.edit(*key)
                if edit.to_value:
                    edit.clear = True
                else:
                    edit.drop_cache = True
                found["formula_cache_cleared"].append(key)

    severities = {
        "formula_constant_replaced": "info", "formula_cache_replaced": "info",
        "formula_text_function": "warn", "formula_cache_cleared": "warn", "formula_converted_to_value": "warn",
        "formula_constant_partial": "block", "formula_constant_ambiguous": "block",
        "formula_external_ref": "block", "formula_unparsable": "block",
    }
    for kind, keys in sorted(found.items()):
        origin = "transform" if severities[kind] in ("info", "warn") else "source"
        notes.append(PlanNote(origin, severities[kind], kind, {**loc, "count": len(keys), "cells": _cells(keys)}))
    if has_formula and edits.cells:
        edits.full_calc_on_load = True
        notes.append(PlanNote("transform", "info", "recalculate_on_open", loc))
    return bp
