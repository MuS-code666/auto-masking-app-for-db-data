"""対応表の管理（設計書 04 章 7）：検索・詳細・別名の追加と削除・変換後名称の変更・統合・取消。

変更は確定済みの対応表に直接行い、変更履歴（master_event）に記録する。どの変更も過去の出力の
表記と食い違い得るため、画面では実行前に影響する出力の一覧を示す。
変換作業の途中（仮登録がある間）は変更しない（呼び出し側で確認する）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from maskapp.entity.normalize import normalize_name

_ACTIVE = "('draft', 'confirmed')"


class MasterEditError(ValueError):
    """対応表を変更できない（重複・種類違い・最後の名称の削除 等）。"""


@dataclass(frozen=True)
class EntityRow:
    id: str
    entity_type: str
    masked_name: str
    status: str
    names: list[tuple[str, str]]  # (元名称, 種別 primary/alias)
    codes: list[tuple[str, str, str]]  # (コード体系名, 元コード, 変換後コード)


@dataclass(frozen=True)
class OutputRef:
    run_id: int
    filename: str
    published_at: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _event(conn, kind: str, entity_id: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO master_event (run_id, kind, entity_id, payload, created_at) VALUES (NULL, ?, ?, ?, ?)",
        (kind, entity_id, json.dumps(payload, ensure_ascii=False), _now()),
    )


def entity(conn: sqlite3.Connection, entity_id: str) -> EntityRow:
    row = conn.execute("SELECT id, entity_type_id, masked_name, status FROM entity WHERE id = ?",
                       (entity_id,)).fetchone()
    if row is None:
        raise MasterEditError("entity not found")
    names = [(n, k) for n, k in conn.execute(
        f"SELECT original_name, kind FROM entity_name WHERE entity_id = ? AND status IN {_ACTIVE} "
        "ORDER BY kind DESC, id", (entity_id,))]
    codes = [tuple(r) for r in conn.execute(
        f"SELECT s.display_name, c.original_code, c.masked_code FROM entity_code c "
        f"JOIN code_scheme s ON s.id = c.code_scheme_id WHERE c.entity_id = ? AND c.status IN {_ACTIVE} "
        "ORDER BY s.display_name, c.code_seq", (entity_id,))]
    return EntityRow(row[0], row[1], row[2], row[3], names, codes)


def search(conn: sqlite3.Connection, text: str = "", entity_type: str | None = None,
           limit: int = 500) -> list[EntityRow]:
    """元名称・別名・元コード・変換後名称・変換後コードの部分一致で検索する（確定・仮登録の対象）。"""
    like = f"%{text}%"
    params: list = [like, like, like, like]
    sql = (
        f"SELECT DISTINCT e.id FROM entity e "
        f"LEFT JOIN entity_name n ON n.entity_id = e.id AND n.status IN {_ACTIVE} "
        f"LEFT JOIN entity_code c ON c.entity_id = e.id AND c.status IN {_ACTIVE} "
        f"WHERE e.status IN {_ACTIVE} AND (n.original_name LIKE ? OR e.masked_name LIKE ? "
        f"OR c.original_code LIKE ? OR c.masked_code LIKE ?)"
    )
    if entity_type:
        sql += " AND e.entity_type_id = ?"
        params.append(entity_type)
    sql += " ORDER BY e.entity_type_id, e.name_seq LIMIT ?"
    params.append(limit)
    return [entity(conn, r[0]) for r in conn.execute(sql, params).fetchall()]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """種類ごとの確定件数（例：{"顧客": 120}）。"""
    return dict(conn.execute(
        "SELECT t.display_name, COUNT(e.id) FROM entity_type t LEFT JOIN entity e ON e.entity_type_id = t.id "
        "AND e.status = 'confirmed' GROUP BY t.id ORDER BY t.rowid"
    ).fetchall())


def affected_outputs(conn: sqlite3.Connection, entity_id: str) -> list[OutputRef]:
    """対象を確定した作業以降に出力したファイル（変更すると表記が食い違い得るもの）。"""
    first = conn.execute("SELECT COALESCE(confirmed_run_id, created_run_id, 0) FROM entity WHERE id = ?",
                         (entity_id,)).fetchone()
    if first is None:
        return []
    return [OutputRef(*r) for r in conn.execute(
        "SELECT o.run_id, o.output_filename, o.published_at FROM run_output o JOIN run r ON r.id = o.run_id "
        "WHERE o.run_id >= ? AND r.state IN ('committed', 'published') ORDER BY o.run_id, o.position",
        (first[0],),
    )]


def _require_confirmed(conn, entity_id: str) -> tuple[str, str]:
    row = conn.execute("SELECT entity_type_id, status FROM entity WHERE id = ?", (entity_id,)).fetchone()
    if row is None or row[1] != "confirmed":
        raise MasterEditError("only confirmed entities can be edited")
    return row


def add_alias(conn: sqlite3.Connection, entity_id: str, name: str) -> None:
    _require_confirmed(conn, entity_id)
    name = name.strip()
    if not name:
        raise MasterEditError("name is empty")
    exists = conn.execute(
        f"SELECT 1 FROM entity_name WHERE entity_id = ? AND original_name = ? AND status IN {_ACTIVE}",
        (entity_id, name),
    ).fetchone()
    if exists:
        raise MasterEditError("the entity already has this name")
    conn.execute(
        "INSERT INTO entity_name (entity_id, original_name, normalized_name, kind, status) "
        "VALUES (?, ?, ?, 'alias', 'confirmed')",
        (entity_id, name, normalize_name(name)),
    )
    _event(conn, "alias_add", entity_id, {"name": name})


def remove_alias(conn: sqlite3.Connection, entity_id: str, name: str) -> None:
    _require_confirmed(conn, entity_id)
    active = conn.execute(
        f"SELECT COUNT(*) FROM entity_name WHERE entity_id = ? AND status IN {_ACTIVE}", (entity_id,)
    ).fetchone()[0]
    if active <= 1:
        raise MasterEditError("cannot remove the last name of an entity")
    updated = conn.execute(
        "UPDATE entity_name SET status = 'void' WHERE entity_id = ? AND original_name = ? AND status = 'confirmed'",
        (entity_id, name),
    ).rowcount
    if not updated:
        raise MasterEditError("name not found")
    _event(conn, "alias_remove", entity_id, {"name": name})


def rename_masked(conn: sqlite3.Connection, entity_id: str, new_name: str) -> None:
    """変換後名称を変える（例：顧客0003 → 顧客A）。同じ種類で重複する名称・元の名称と同じ名称は不可。"""
    etype, _ = _require_confirmed(conn, entity_id)
    new_name = new_name.strip()
    if not new_name:
        raise MasterEditError("name is empty")
    if conn.execute("SELECT 1 FROM entity WHERE entity_type_id = ? AND masked_name = ? AND id != ?",
                    (etype, new_name, entity_id)).fetchone():
        raise MasterEditError("another entity already uses this masked name")
    if conn.execute(f"SELECT 1 FROM entity_name WHERE original_name = ? AND status IN {_ACTIVE}",
                    (new_name,)).fetchone():
        raise MasterEditError("the masked name must not be an original name")
    old = conn.execute("SELECT masked_name FROM entity WHERE id = ?", (entity_id,)).fetchone()[0]
    conn.execute("UPDATE entity SET masked_name = ? WHERE id = ?", (new_name, entity_id))
    _event(conn, "rename_masked", entity_id, {"before": old, "after": new_name})


def merge(conn: sqlite3.Connection, source_id: str, target_id: str) -> None:
    """source を target に統合する。名称は target の別名、コードは target のコードになる。"""
    if source_id == target_id:
        raise MasterEditError("cannot merge an entity into itself")
    s_type, _ = _require_confirmed(conn, source_id)
    t_type, _ = _require_confirmed(conn, target_id)
    if s_type != t_type:
        raise MasterEditError("entities of different types cannot be merged")
    target_names = {r[0] for r in conn.execute(
        f"SELECT original_name FROM entity_name WHERE entity_id = ? AND status IN {_ACTIVE}", (target_id,))}
    for (name,) in conn.execute(
        f"SELECT original_name FROM entity_name WHERE entity_id = ? AND status IN {_ACTIVE}", (source_id,)
    ).fetchall():
        if name in target_names:
            conn.execute("UPDATE entity_name SET status = 'void' WHERE entity_id = ? AND original_name = ?",
                         (source_id, name))
        else:
            conn.execute("UPDATE entity_name SET entity_id = ?, kind = 'alias' WHERE entity_id = ? "
                         "AND original_name = ? AND status = 'confirmed'", (target_id, source_id, name))
    conn.execute("UPDATE entity_code SET entity_id = ? WHERE entity_id = ? AND status = 'confirmed'",
                 (target_id, source_id))
    conn.execute("UPDATE entity SET status = 'merged', merged_into = ? WHERE id = ?", (target_id, source_id))
    _event(conn, "merge", source_id, {"into": target_id})


def void(conn: sqlite3.Connection, entity_id: str) -> None:
    """対象を取り消す（番号は欠番）。名称・コードも無効にする。次の作業で同じ値が現れれば新しい対象になる。"""
    _require_confirmed(conn, entity_id)
    conn.execute("UPDATE entity_name SET status = 'void' WHERE entity_id = ? AND status = 'confirmed'", (entity_id,))
    conn.execute("UPDATE entity_code SET status = 'void' WHERE entity_id = ? AND status = 'confirmed'", (entity_id,))
    conn.execute("UPDATE entity SET status = 'void' WHERE id = ?", (entity_id,))
    _event(conn, "void", entity_id, {})
