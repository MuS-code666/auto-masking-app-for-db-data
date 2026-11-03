"""取込：原本をプロジェクトに読み取り専用で複製し、ハッシュと形式を記録する（設計書 02 章 4）。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from maskapp.formats.csvfile import CsvDocument, Dialect, read_csv
from maskapp.formats.xlsx.book import XlsxBook
from maskapp.formats.xlsx.package import XlsxFormatError
from maskapp.infra.safelog import get_logger
from maskapp.project import Project

_log = get_logger("ingest")

CSV_SUFFIXES = (".csv", ".txt", ".tsv")
XLSX_SUFFIXES = (".xlsx",)
# 拡張子で分かる未対応形式（設計書 06 章 1）
UNSUPPORTED_SUFFIXES = (".xlsm", ".xltm", ".xls", ".xlsb", ".ods", ".xlam")

Source = CsvDocument | XlsxBook


class UnsupportedFormatError(ValueError):
    """対応していない形式。reason は画面の案内に使う識別子。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ImportedFile:
    id: int
    sha256: str
    format: str  # "csv" / "xlsx"
    dialect: Dialect | None
    record_count: int
    already_imported: bool


def _store_original(project: Project, path: Path, data: bytes, digest: str) -> Path:
    stored = Path("originals") / f"{digest[:16]}{path.suffix.lower()}"
    target = project.root / stored
    if not target.exists():
        target.write_bytes(data)
        os.chmod(target, stat.S_IREAD)
    return stored


def _existing(project: Project, digest: str) -> int | None:
    row = project.conn.execute("SELECT id FROM source_file WHERE sha256 = ?", (digest,)).fetchone()
    return row[0] if row else None


def import_file(project: Project, path: Path, encoding: str | None = None,
                delimiter: str | None = None) -> ImportedFile:
    suffix = Path(path).suffix.lower()
    if suffix in XLSX_SUFFIXES:
        return import_xlsx(project, path)
    if suffix in CSV_SUFFIXES:
        return import_csv(project, path, encoding=encoding, delimiter=delimiter)
    if suffix in UNSUPPORTED_SUFFIXES:
        raise UnsupportedFormatError("unsupported_spreadsheet_format")
    raise UnsupportedFormatError("unknown_file_type")


def import_csv(
    project: Project, path: Path, encoding: str | None = None, delimiter: str | None = None
) -> ImportedFile:
    path = Path(path)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    doc = read_csv(data, encoding=encoding, delimiter=delimiter)  # 読めないファイルはここで停止

    existing = _existing(project, digest)
    if existing:
        _log.event("import_skipped_duplicate", file_id=existing)
        return ImportedFile(existing, digest, "csv", doc.dialect, len(doc.records), True)

    stored = _store_original(project, path, data, digest)
    with project.transaction() as conn:
        file_id = conn.execute(
            "INSERT INTO source_file (original_filename, stored_path, sha256, size, format, imported_at, "
            "format_info, original_dir) VALUES (?, ?, ?, ?, 'csv', ?, ?, ?)",
            (path.name, stored.as_posix(), digest, len(data), datetime.now(UTC).isoformat(),
             json.dumps(doc.dialect.to_json()), str(path.resolve().parent)),
        ).lastrowid
        sheet_id = conn.execute(
            "INSERT INTO sheet (source_file_id, idx, name, visibility) VALUES (?, 0, '', 'visible')", (file_id,)
        ).lastrowid
        width = max((len(r.fields) for r in doc.records), default=0)
        region_id = conn.execute(
            "INSERT INTO table_region (sheet_id, header_rows, data_first_row, data_last_row, first_col, "
            "last_col, region_kind) VALUES (?, '[0]', 1, ?, 0, ?, 'data')",
            (sheet_id, max(len(doc.records) - 1, 0), max(width - 1, 0)),
        ).lastrowid
        if doc.records:
            conn.executemany(
                "INSERT INTO source_column (region_id, col_index, header_text) VALUES (?, ?, ?)",
                [(region_id, i, h) for i, h in enumerate(doc.records[0].values)],
            )
    _log.event("csv_imported", file_id=file_id, records=len(doc.records), columns=width)
    return ImportedFile(file_id, digest, "csv", doc.dialect, len(doc.records), False)


def feature_summary(book: XlsxBook) -> dict[str, int]:
    """取込時に検出した部品の種類ごとの数（画面表示用）。"""
    counts = Counter(p.feature for p in book.pkg.parts())
    counts["hidden_sheets"] = sum(1 for s in book.sheets if s.state != "visible")
    counts["formulas"] = sum(1 for s in book.sheets for c in s.cells.values() if c.formula is not None)
    return dict(sorted(counts.items()))


def import_xlsx(project: Project, path: Path) -> ImportedFile:
    path = Path(path)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    book = XlsxBook.parse(data)  # 暗号化・Strict 形式・壊れたファイルはここで停止
    rows = sum(len(s.grid()) for s in book.sheets)

    existing = _existing(project, digest)
    if existing:
        _log.event("import_skipped_duplicate", file_id=existing)
        return ImportedFile(existing, digest, "xlsx", None, rows, True)

    stored = _store_original(project, path, data, digest)
    with project.transaction() as conn:
        file_id = conn.execute(
            "INSERT INTO source_file (original_filename, stored_path, sha256, size, format, imported_at, "
            "format_info, original_dir) VALUES (?, ?, ?, ?, 'xlsx', ?, ?, ?)",
            (path.name, stored.as_posix(), digest, len(data), datetime.now(UTC).isoformat(),
             json.dumps({"features": feature_summary(book)}), str(path.resolve().parent)),
        ).lastrowid
        conn.executemany(
            "INSERT INTO sheet (source_file_id, idx, name, visibility) VALUES (?, ?, ?, ?)",
            [(file_id, s.idx, s.name, s.state if s.state in ("hidden", "veryHidden") else "visible")
             for s in book.sheets],
        )
    _log.event("xlsx_imported", file_id=file_id, sheets=len(book.sheets), rows=rows)
    return ImportedFile(file_id, digest, "xlsx", None, rows, False)


def load_source(project: Project, file_id: int) -> Source:
    """プロジェクト内の原本の複製を読み込む。ハッシュが記録と違えば停止する。"""
    row = project.conn.execute(
        "SELECT stored_path, sha256, format_info, format FROM source_file WHERE id = ?", (file_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"source file {file_id} not found")
    stored_path, digest, format_info, fmt = row
    data = (project.root / stored_path).read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("stored original file has been modified")
    if fmt == "xlsx":
        return XlsxBook.parse(data)
    dialect = Dialect.from_json(json.loads(format_info))
    return read_csv(data, encoding=dialect.encoding, delimiter=dialect.delimiter)


__all__ = [
    "ImportedFile", "Source", "UnsupportedFormatError", "XlsxFormatError", "import_csv", "import_file",
    "import_xlsx", "load_source",
]
