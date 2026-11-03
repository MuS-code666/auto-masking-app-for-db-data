"""プロジェクト一覧（設計書 04 章 2）。

一覧に載せるのはプロジェクトフォルダの場所だけで、アプリ用フォルダの projects.json に置く。
名前・件数・作業状況は表示のたびに各プロジェクトの project.db を読み取り専用で読む
（ロックは取らない。開いている最中のプロジェクトも表示できる）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from maskapp.infra.paths import app_home
from maskapp.project.store import DB_NAME

_STATE_LABELS = {
    "draft": "設定中",
    "planned": "確認中",
    "staged": "検証中",
    "verified": "検証済み（確定前）",
    "committed": "出力待ち（対応表は確定済み）",
}


@dataclass
class ProjectSummary:
    path: Path
    name: str = ""
    color: str | None = None
    updated_at: str = ""
    files: int = 0
    mappings: dict[str, int] = field(default_factory=dict)
    status: str = ""
    error: str | None = None  # 読めない場合の理由（"missing" / "unreadable"）


def _summary(path: Path) -> ProjectSummary:
    summary = ProjectSummary(path)
    db = path / DB_NAME
    if not db.is_file():
        summary.error = "missing"
        return summary
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            summary.name, summary.color = conn.execute("SELECT name, color FROM project_info").fetchone()
            summary.files = conn.execute("SELECT COUNT(*) FROM source_file").fetchone()[0]
            summary.mappings = {
                name: count for name, count in conn.execute(
                    "SELECT t.display_name, COUNT(e.id) FROM entity_type t JOIN entity e "
                    "ON e.entity_type_id = t.id AND e.status = 'confirmed' GROUP BY t.id ORDER BY t.rowid"
                )
            }
            updated = conn.execute("SELECT MAX(updated_at) FROM run").fetchone()[0]
            summary.updated_at = updated or conn.execute("SELECT created_at FROM project_info").fetchone()[0]
            active = conn.execute(
                "SELECT id, state FROM run WHERE kind = 'convert' AND state IN "
                "('draft', 'planned', 'staged', 'verified', 'committed') ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if active:
                summary.status = _STATE_LABELS[active[1]]
                if active[1] == "planned":
                    n = conn.execute("SELECT COUNT(*) FROM review_item WHERE run_id = ? AND state = 'open'",
                                     (active[0],)).fetchone()[0]
                    if n:
                        summary.status = f"確認中（要確認 {n}件）"
            else:
                published = conn.execute(
                    "SELECT MAX(updated_at) FROM run WHERE state = 'published'").fetchone()[0]
                summary.status = f"出力済み（{published[:16].replace('T', ' ')} UTC）" if published else "未着手"
        finally:
            conn.close()
    except (sqlite3.DatabaseError, TypeError):
        summary.error = "unreadable"
    return summary


class Registry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_home() / "projects.json"

    def paths(self) -> list[Path]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [Path(p) for p in data.get("projects", [])]

    def _write(self, paths: list[Path]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"projects": [str(p) for p in paths]}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)

    def add(self, path: Path) -> None:
        path = Path(path).resolve()
        paths = [p for p in self.paths() if p.resolve() != path]
        self._write([path] + paths)

    def remove(self, path: Path) -> None:
        path = Path(path).resolve()
        self._write([p for p in self.paths() if p.resolve() != path])

    def summaries(self) -> list[ProjectSummary]:
        return [_summary(p) for p in self.paths()]
