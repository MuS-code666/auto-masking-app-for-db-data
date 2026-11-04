"""検証（設計書 07 章 1〜2）。作業領域に書き出したファイルを読み直して検査する。

検査項目:
  V1 構造（CSV：レコード数・フィールド数・形式、xlsx：シートの数・名前・順序・セルの位置）
  V2 維持した値（計画にないセルは元と完全一致。xlsx は型・書式・数式も）
  V3 集計（参考。Decimal で計算）
  V4 置換の一貫性（同じ対象→同じ値、異なる対象→異なる値、元の値が残っていない）
  V5 内容削除
  V6 関連（結合件数の比較。元からの未一致・重複は元データ由来として記録）
  V7 元語句の残存（xlsx は全部品：セル・数式・シート名・名前定義・グラフ・コメント・プロパティ等）
  V8 未確認の要確認項目
  V9 出力ファイル名
  V10 出力の健全性（xlsx：ZIP・XML・関係定義・内容種別、第二の読込器 openpyxl で同じ値が読めること）
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from maskapp.config.model import Action
from maskapp.formats.csvfile import CsvDocument, CsvFormatError, read_csv
from maskapp.transform import reviews
from maskapp.transform.planner import FilePlan, Grid, Plan
from maskapp.transform.reviews import Decision, ReviewKind
from maskapp.verify.residual import ResidualScanner, TermKind, dictionary_terms

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


@dataclass(frozen=True)
class Finding:
    origin: str  # "source" / "transform" / "environment"
    severity: str  # "block" / "warn" / "info"
    kind: str
    location: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)


@dataclass
class VerifyReport:
    findings: list[Finding]
    aggregates: list[dict]
    relations: list[dict]

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def by_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]


def relation_stats(from_keys: list[str], to_keys: list[str]) -> dict[str, int]:
    to_counts = Counter(k for k in to_keys if k)
    nonblank = [k for k in from_keys if k]
    unmatched = [k for k in nonblank if k not in to_counts]
    return {
        "from_rows": len(from_keys),
        "blank_keys": len(from_keys) - len(nonblank),
        "matched_rows": len(nonblank) - len(unmatched),
        "unmatched_rows": len(unmatched),
        "unmatched_keys": len(set(unmatched)),
        "duplicate_target_keys": sum(1 for c in to_counts.values() if c > 1),
        "join_rows": sum(to_counts.get(k, 0) for k in nonblank),
    }


def column_values(fp: FilePlan, grid: Grid, col: int) -> list[str]:
    return [grid.cell(r, col) for r in fp.data_rows if r < len(grid.blank) and not grid.blank[r]]


class ResidualCheck:
    """元語句の残存の判定（V7）。短い語は要確認にし、人が「問題なし」とした箇所は除く。"""

    def __init__(self, conn: sqlite3.Connection, run_id: int, findings: list[Finding]) -> None:
        self.conn = conn
        self.run_id = run_id
        self.findings = findings
        self.scanner = ResidualScanner(dictionary_terms(conn))

    def check(self, text: str, location: dict, numeric_context: bool = False, kind: str = "residual",
              ignore_short: bool = False) -> None:
        """ignore_short: 文章内を検査した部分。短い語は計画の作成時に判断済みのため除く。"""
        for hit in self.scanner.scan(text):
            if ignore_short and hit.short:
                continue
            if numeric_context and TermKind.CODE in hit.kinds:
                self.findings.append(Finding("source", "info", "residual_numeric_match", location))
            elif hit.short:
                key = json.dumps(location, sort_keys=True, ensure_ascii=False) + ":" + hit.term
                item = reviews.upsert(self.conn, self.run_id, ReviewKind.RESIDUAL_SHORT, key,
                                      {"term": hit.term}, location)
                if not (item.decision and item.decision.get("decision") == Decision.FALSE_POSITIVE):
                    self.findings.append(Finding("transform", "block", "residual_short", location,
                                                 {"review_id": item.id}))
            else:
                self.findings.append(Finding("transform", "block", kind, location))


def _verify_csv(fp: FilePlan, src: CsvDocument, data: bytes, findings: list[Finding]) -> Grid | None:
    loc = {"file_id": fp.file_id}
    try:
        out = read_csv(data, encoding=src.dialect.encoding, delimiter=src.dialect.delimiter)
    except CsvFormatError:
        findings.append(Finding("transform", "block", "output_unreadable", loc))
        return None
    # V1 構造
    if out.dialect != src.dialect:
        findings.append(Finding("transform", "block", "format_changed", loc))
    if len(out.records) != len(src.records):
        findings.append(Finding("transform", "block", "record_count_changed", loc,
                                {"before": len(src.records), "after": len(out.records)}))
        return None
    width = len(fp.template.columns)
    for r, (a, b) in enumerate(zip(src.records, out.records, strict=True)):
        if len(a.fields) != len(b.fields):
            findings.append(Finding("transform", "block", "field_count_changed", {**loc, "record": r}))
        elif r >= fp.template.header_row and not a.is_blank and len(a.fields) != width:
            findings.append(Finding("source", "info", "ragged_record", {**loc, "record": r},
                                    {"fields": len(a.fields), "expected": width}))
    # V2・V4・V5 セル単位
    for r, (a, b) in enumerate(zip(src.records, out.records, strict=True)):
        for c, (fa, fb) in enumerate(zip(a.fields, b.fields, strict=False)):
            assignment = fp.changes.get((r, c))
            cell = {**loc, "record": r, "column": c}
            if assignment is None:
                if fa.value != fb.value:
                    findings.append(Finding("transform", "block", "kept_value_changed", cell))
            elif assignment.value is None:
                findings.append(Finding("transform", "block", "unresolved_cell", cell))
            elif fb.value != assignment.value:
                findings.append(Finding("transform", "block", "planned_value_mismatch", cell))
            elif assignment.kind != "clear" and fa.value and fb.value == fa.value:
                findings.append(Finding("transform", "block", "replace_not_applied", cell))
    return Grid.of_csv(out)


def _numeric_keep_columns(fp: FilePlan) -> set[int]:
    cols = set()
    for c, spec in enumerate(fp.template.columns):
        if spec.action is Action.KEEP:
            values = [v for v in column_values(fp, fp.grid, c) if v]
            if values and all(_NUMERIC.match(v) for v in values):
                cols.add(c)
    return cols


def verify(conn: sqlite3.Connection, plan: Plan, staged: dict[int, tuple[str, bytes]]) -> VerifyReport:
    """staged[file_id] = (出力ファイル名, 書き出したバイト列)。呼び出し側のトランザクション内で使う。"""
    from maskapp.verify.xlsx_checks import verify_book

    findings: list[Finding] = []
    aggregates: list[dict] = []
    out_grids: dict[tuple[int, int], Grid] = {}
    residual = ResidualCheck(conn, plan.run_id, findings)

    for note in plan.notes:
        location = {k: note.detail[k] for k in ("file_id",) if k in note.detail}
        findings.append(Finding(note.origin, note.severity, note.kind, location, note.detail))

    for rf in plan.run_files:
        name, data = staged[rf.file_id]
        source = plan.sources[rf.file_id]
        units = plan.units_of(rf.file_id)
        if isinstance(source, CsvDocument):
            fp = units[0]
            grid = _verify_csv(fp, source, data, findings)
            if grid is not None:
                out_grids[(fp.file_id, 0)] = grid
                # V7（計画で作った値のセルは除く。生成した値には元の語句が含まれないため）
                numeric_cols = _numeric_keep_columns(fp)
                for r, row in enumerate(grid.rows):
                    for c, value in enumerate(row):
                        if (r, c) in fp.freetext:
                            for part in fp.freetext[(r, c)]:
                                residual.check(part, {"file_id": fp.file_id, "record": r, "column": c},
                                               ignore_short=True)
                        elif (r, c) not in fp.changes and value:
                            residual.check(value, {"file_id": fp.file_id, "record": r, "column": c},
                                           numeric_context=not fp.is_outside(r) and r > fp.template.header_row
                                           and c in numeric_cols)
        else:
            out_book = verify_book(plan, rf, source, data, findings, residual)
            if out_book is not None:
                for fp in units:
                    out_grids[(fp.file_id, fp.sheet)] = Grid.of_rows(out_book.sheet(fp.sheet).grid())
        # V9 出力ファイル名
        residual.check(name, {"file_id": rf.file_id, "part": "filename"}, kind="residual_filename")

    # V3 集計（参考）
    for fp in plan.files:
        out = out_grids.get((fp.file_id, fp.sheet))
        if out is None:
            continue
        for c in sorted(_numeric_keep_columns(fp)):
            nonblank = [v for v in column_values(fp, fp.grid, c) if v]
            out_values = [v for v in column_values(fp, out, c) if v]
            aggregates.append({
                "file_id": fp.file_id, "sheet": fp.sheet, "column": fp.template.columns[c].header,
                "count": len(nonblank),
                "sum_before": str(sum(Decimal(v) for v in nonblank)),
                "sum_after": str(sum(Decimal(v) for v in out_values if _NUMERIC.match(v))),
            })

    # V4 置換の一貫性（作業全体）
    code_out: dict[tuple[int, str], set[str]] = {}
    code_back: dict[tuple[int, str], set[str]] = {}
    name_out: dict[str, set[str]] = {}
    name_back: dict[tuple[str, str], set[str]] = {}
    entity_types: dict[str, str] = {}
    for fp in plan.files:
        out = out_grids.get((fp.file_id, fp.sheet))
        if out is None:
            continue
        for (r, c), a in fp.changes.items():
            if a.value is None:
                continue
            original, produced = fp.grid.cell(r, c), out.cell(r, c)
            if a.kind == "entity_code":
                code_out.setdefault((a.scheme_id, original), set()).add(produced)
                code_back.setdefault((a.scheme_id, produced), set()).add(original)
            elif a.kind == "entity_name":
                name_out.setdefault(a.ref, set()).add(produced)
                if a.ref not in entity_types:
                    entity_types[a.ref] = conn.execute(
                        "SELECT entity_type_id FROM entity WHERE id = ?", (a.ref,)
                    ).fetchone()[0]
                name_back.setdefault((entity_types[a.ref], produced), set()).add(a.ref)
    for mapping, kind in ((code_out, "code_not_consistent"), (code_back, "code_not_unique"),
                          (name_out, "name_not_consistent"), (name_back, "name_not_unique")):
        for values in mapping.values():
            if len(values) > 1:
                findings.append(Finding("transform", "block", kind))

    # V6 関連
    relations: list[dict] = []
    for rel in plan.config.relations:
        from_files = [fp for fp in plan.files if fp.template.name == rel.from_template]
        to_files = [fp for fp in plan.files if fp.template.name == rel.to_template]
        detail = {"from": f"{rel.from_template}.{rel.from_column}", "to": f"{rel.to_template}.{rel.to_column}"}
        if not from_files or not to_files:
            findings.append(Finding("environment", "info", "relation_not_checked", {}, detail))
            continue
        if any((fp.file_id, fp.sheet) not in out_grids for fp in from_files + to_files):
            continue
        fc = from_files[0].template.index_of(rel.from_column)
        tc = to_files[0].template.index_of(rel.to_column)
        before = relation_stats(
            [v for fp in from_files for v in column_values(fp, fp.grid, fc)],
            [v for fp in to_files for v in column_values(fp, fp.grid, tc)],
        )
        after = relation_stats(
            [v for fp in from_files for v in column_values(fp, out_grids[(fp.file_id, fp.sheet)], fc)],
            [v for fp in to_files for v in column_values(fp, out_grids[(fp.file_id, fp.sheet)], tc)],
        )
        relations.append({**detail, "before": before, "after": after})
        if before != after:
            findings.append(Finding("transform", "block", "relation_changed", {}, {**detail, "before": before,
                                                                                    "after": after}))
        if before["unmatched_rows"]:
            findings.append(Finding("source", "info", "relation_unmatched", {},
                                    {**detail, "rows": before["unmatched_rows"], "keys": before["unmatched_keys"]}))
        if before["duplicate_target_keys"]:
            findings.append(Finding("source", "info", "relation_duplicate_target", {},
                                    {**detail, "keys": before["duplicate_target_keys"]}))

    # V8 未確認
    open_items = len(reviews.list_items(conn, plan.run_id, state="open"))
    if open_items:
        findings.append(Finding("transform", "block", "open_reviews", {}, {"count": open_items}))

    _store(conn, plan.run_id, findings)
    return VerifyReport(findings, aggregates, relations)


def _store(conn: sqlite3.Connection, run_id: int, findings: list[Finding]) -> None:
    conn.execute("DELETE FROM finding WHERE run_id = ?", (run_id,))
    conn.executemany(
        "INSERT INTO finding (run_id, origin, severity, kind, location, detail) VALUES (?, ?, ?, ?, ?, ?)",
        [(run_id, f.origin, f.severity, f.kind, json.dumps(f.location, ensure_ascii=False),
          json.dumps(f.detail, ensure_ascii=False))
         for f in findings],
    )
