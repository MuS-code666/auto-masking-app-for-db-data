"""取込：原本をプロジェクトに読み取り専用で複製し、ハッシュと形式を記録する（設計書 02 章 4）。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from maskapp.formats.csvfile import CsvDocument, Dialect, read_csv
from maskapp.infra.safelog import get_logger
from maskapp.project import Project

_log = get_logger("ingest")


@dataclass(frozen=True)
class ImportedFile:
    id: int
    sha256: str
    dialect: Dialect
    record_count: int
    already_imported: bool


def import_csv(
    project: Project, path: Path, encoding: str | None = None, delimiter: str | None = None
) -> ImportedFile:
    path = Path(path)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    doc = read_csv(data, encoding=encoding, delimiter=delimiter)  # 読めないファイルはここで停止

    existing = project.conn.execute(
        "SELECT id FROM source_file WHERE sha256 = ?", (digest,)
    ).fetchone()
    if existing:
        _log.event("import_skipped_duplicate", file_id=existing[0])
        return ImportedFile(existing[0], digest, doc.dialect, len(doc.records), True)

    stored = Path("originals") / f"{digest[:16]}{path.suffix.lower()}"
    target = project.root / stored
    if not target.exists():
        target.write_bytes(data)
        os.chmod(target, stat.S_IREAD)

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
    return ImportedFile(file_id, digest, doc.dialect, len(doc.records), False)


def load_document(project: Project, file_id: int) -> CsvDocument:
    """プロジェクト内の原本の複製を読み込む。ハッシュが記録と違えば停止する。"""
    row = project.conn.execute(
        "SELECT stored_path, sha256, format_info FROM source_file WHERE id = ?", (file_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"source file {file_id} not found")
    stored_path, digest, format_info = row
    data = (project.root / stored_path).read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("stored original file has been modified")
    dialect = Dialect.from_json(json.loads(format_info))
    return read_csv(data, encoding=dialect.encoding, delimiter=dialect.delimiter)
