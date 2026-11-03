"""要確認項目（設計書 04 章 5、03 章 4 review_item）。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ReviewKind(StrEnum):
    NEW_ENTITY = "new_entity"  # 新しい対象
    CODE_MULTI_NAME = "code_multi_name"  # 同じコードに複数の名称
    NAME_AMBIGUOUS = "name_ambiguous"  # 同じ名称の対象が複数（コードなしの列）
    SIMILAR_NAME = "similar_name"  # 正規化で一致する別表記
    RESIDUAL_SHORT = "residual_short"  # 短い語の残存（人が判断）


class Decision(StrEnum):
    REGISTER_NEW = "register_new"  # 新しい対象として登録
    LINK_EXISTING = "link_existing"  # 既存の対象に紐付け（別名登録）
    ADD_ALIAS = "add_alias"  # 別名として登録
    KEEP_AS_SOURCE_ERROR = "keep_as_source_error"  # 元データの誤りとして扱う（コードの対象名で置換）
    FALSE_POSITIVE = "false_positive"  # 問題なし（誤検出）


ALLOWED: dict[ReviewKind, set[Decision]] = {
    ReviewKind.NEW_ENTITY: {Decision.REGISTER_NEW, Decision.LINK_EXISTING},
    ReviewKind.CODE_MULTI_NAME: {Decision.ADD_ALIAS, Decision.KEEP_AS_SOURCE_ERROR},
    ReviewKind.NAME_AMBIGUOUS: {Decision.LINK_EXISTING, Decision.REGISTER_NEW},
    ReviewKind.SIMILAR_NAME: {Decision.LINK_EXISTING, Decision.REGISTER_NEW},
    ReviewKind.RESIDUAL_SHORT: {Decision.FALSE_POSITIVE},
}


@dataclass(frozen=True)
class ReviewItem:
    id: int
    kind: ReviewKind
    subject: dict[str, Any]
    location: dict[str, Any]
    state: str
    decision: dict[str, Any] | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def upsert(
    conn: sqlite3.Connection, run_id: int, kind: ReviewKind, key: str, subject: dict, location: dict
) -> ReviewItem:
    """同じ作業・同じキーの項目があればそれを返し、なければ作る。"""
    item_key = f"{kind.value}:{key}"
    conn.execute(
        "INSERT OR IGNORE INTO review_item (run_id, kind, location, subject, state, created_at, item_key) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (run_id, kind.value, json.dumps(location, ensure_ascii=False), json.dumps(subject, ensure_ascii=False),
         _now(), item_key),
    )
    row = conn.execute(
        "SELECT id, kind, subject, location, state, decision FROM review_item WHERE run_id = ? AND item_key = ?",
        (run_id, item_key),
    ).fetchone()
    return _row(row)


def get(conn: sqlite3.Connection, item_id: int) -> ReviewItem:
    row = conn.execute(
        "SELECT id, kind, subject, location, state, decision FROM review_item WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"review item {item_id} not found")
    return _row(row)


def list_items(conn: sqlite3.Connection, run_id: int, state: str | None = "open") -> list[ReviewItem]:
    sql = "SELECT id, kind, subject, location, state, decision FROM review_item WHERE run_id = ?"
    params: list[Any] = [run_id]
    if state:
        sql += " AND state = ?"
        params.append(state)
    return [_row(r) for r in conn.execute(sql + " ORDER BY id", params)]


def set_decision(conn: sqlite3.Connection, item_id: int, decision: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE review_item SET decision = ?, state = 'decided', decided_at = ? WHERE id = ?",
        (json.dumps(decision, ensure_ascii=False), _now(), item_id),
    )


def _row(row) -> ReviewItem:
    item_id, kind, subject, location, state, decision = row
    return ReviewItem(
        item_id, ReviewKind(kind), json.loads(subject), json.loads(location), state,
        json.loads(decision) if decision else None,
    )
