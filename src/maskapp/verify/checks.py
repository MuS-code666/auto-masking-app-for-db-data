"""検証（設計書 07 章 1〜2）。作業領域に書き出したファイルを読み直して検査する。

検査項目:
  V1 構造（レコード数・各レコードのフィールド数・形式）
  V2 維持した値（計画にないセルは元と完全一致）
  V3 集計（参考。Decimal で計算）
  V4 置換の一貫性（同じ対象→同じ値、異なる対象→異なる値、元の値が残っていない）
  V5 内容削除
  V6 関連（結合件数の比較。元からの未一致・重複は元データ由来として記録）
  V7 元語句の残存
  V8 未確認の要確認項目
  V9 出力ファイル名
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
from maskapp.transform.planner import FilePlan, Plan
from maskapp.transform.reviews import Decision, ReviewKind
from maskapp.verify.residual import ResidualScanner, TermKind

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")
_ACTIVE = "('draft', 'confirmed')"


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


def _column_values(fp: FilePlan, doc: CsvDocument, col: int) -> list[str]:
    out = []
    for r in fp.data_rows:
        record = doc.records[r]
        if record.is_blank:
            continue
        out.append(record.fields[col].value if col < len(record.fields) else "")
    return out


def _dictionary(conn: sqlite3.Connection) -> list[tuple[str, TermKind]]:
    terms: list[tuple[str, TermKind]] = []
    terms += [(r[0], TermKind.NAME) for r in conn.execute(
        f"SELECT n.original_name FROM entity_name n JOIN entity e ON e.id = n.entity_id "
        f"WHERE n.status IN {_ACTIVE} AND e.status IN {_ACTIVE}"
    )]
    terms += [(r[0], TermKind.CODE) for r in conn.execute(
        f"SELECT original_code FROM entity_code WHERE status IN {_ACTIVE}"
    )]
    terms += [(r[0], TermKind.VALUE) for r in conn.execute(
        f"SELECT original_value FROM value_token WHERE status IN {_ACTIVE}"
    )]
    return terms


def verify(conn: sqlite3.Connection, plan: Plan, staged: dict[int, tuple[str, bytes]]) -> VerifyReport:
    """staged[file_id] = (出力ファイル名, 書き出したバイト列)。呼び出し側のトランザクション内で使う。"""
    findings: list[Finding] = []
    aggregates: list[dict] = []
    out_docs: dict[int, CsvDocument] = {}

    for note in plan.notes:
        findings.append(Finding(note.origin, note.severity, note.kind, {}, note.detail))

    for fp in plan.files:
        name, data = staged[fp.file_id]
        loc = {"file_id": fp.file_id}
        src = fp.doc
        try:
            out = read_csv(data, encoding=src.dialect.encoding, delimiter=src.dialect.delimiter)
        except CsvFormatError:
            findings.append(Finding("transform", "block", "output_unreadable", loc))
            continue
        out_docs[fp.file_id] = out
        # V1 構造
        if out.dialect != src.dialect:
            findings.append(Finding("transform", "block", "format_changed", loc))
        if len(out.records) != len(src.records):
            findings.append(Finding("transform", "block", "record_count_changed", loc,
                                    {"before": len(src.records), "after": len(out.records)}))
            continue
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
        # V3 集計（参考）
        for c, spec in enumerate(fp.template.columns):
            if spec.action is not Action.KEEP:
                continue
            values = _column_values(fp, src, c)
            nonblank = [v for v in values if v]
            if nonblank and all(_NUMERIC.match(v) for v in nonblank):
                out_values = [v for v in _column_values(fp, out, c) if v]
                aggregates.append({
                    "file_id": fp.file_id, "column": spec.header, "count": len(nonblank),
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
        out = out_docs.get(fp.file_id)
        if out is None or len(out.records) != len(fp.doc.records):
            continue
        for (r, c), a in fp.changes.items():
            if a.value is None or c >= len(out.records[r].fields):
                continue
            original, produced = fp.doc.records[r].fields[c].value, out.records[r].fields[c].value
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
        if any(fp.file_id not in out_docs for fp in from_files + to_files):
            continue
        fc = from_files[0].template.index_of(rel.from_column)
        tc = to_files[0].template.index_of(rel.to_column)
        before = relation_stats(
            [v for fp in from_files for v in _column_values(fp, fp.doc, fc)],
            [v for fp in to_files for v in _column_values(fp, fp.doc, tc)],
        )
        after = relation_stats(
            [v for fp in from_files for v in _column_values(fp, out_docs[fp.file_id], fc)],
            [v for fp in to_files for v in _column_values(fp, out_docs[fp.file_id], tc)],
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

    # V7 元語句の残存（計画で作った値のセルは除く。生成した値には元の語句が含まれないため）
    scanner = ResidualScanner(_dictionary(conn))
    for fp in plan.files:
        out = out_docs.get(fp.file_id)
        if out is None:
            continue
        numeric_keep_cols = set()
        for c, spec in enumerate(fp.template.columns):
            if spec.action is Action.KEEP:
                values = [v for v in _column_values(fp, fp.doc, c) if v]
                if values and all(_NUMERIC.match(v) for v in values):
                    numeric_keep_cols.add(c)
        for r, record in enumerate(out.records):
            in_data = r > fp.template.header_row
            for c, f in enumerate(record.fields):
                if (r, c) in fp.changes:
                    continue
                for hit in scanner.scan(f.value):
                    cell = {"file_id": fp.file_id, "record": r, "column": c}
                    if in_data and c in numeric_keep_cols and TermKind.CODE in hit.kinds:
                        findings.append(Finding("source", "info", "residual_numeric_match", cell))
                    elif hit.short:
                        item = reviews.upsert(
                            conn, plan.run_id, ReviewKind.RESIDUAL_SHORT,
                            f"{fp.file_id}:{r}:{c}:{hit.term}", {"term": hit.term}, cell,
                        )
                        if not (item.decision and item.decision.get("decision") == Decision.FALSE_POSITIVE):
                            findings.append(Finding("transform", "block", "residual_short", cell,
                                                    {"review_id": item.id}))
                    else:
                        findings.append(Finding("transform", "block", "residual", cell))
        # V9 出力ファイル名
        name, _ = staged[fp.file_id]
        if scanner.scan(name):
            findings.append(Finding("transform", "block", "residual_filename", {"file_id": fp.file_id}))

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
        [(run_id, f.origin, f.severity, f.kind, json.dumps(f.location), json.dumps(f.detail, ensure_ascii=False))
         for f in findings],
    )
