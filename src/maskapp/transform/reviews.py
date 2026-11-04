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
    FREETEXT_SHORT = "freetext_short"  # 文章内の短い語（置換するかを人が判断。列ごと）
    FREETEXT_AMBIGUOUS = "freetext_ambiguous"  # 文章内の語で置換先が複数（列ごとに置換先を選ぶ）
    FREETEXT_CANDIDATE = "freetext_candidate"  # 規則で検出した候補（電話番号・法人名らしい語 等）


class Decision(StrEnum):
    REGISTER_NEW = "register_new"  # 新しい対象として登録
    LINK_EXISTING = "link_existing"  # 既存の対象に紐付け（別名登録）
    ADD_ALIAS = "add_alias"  # 別名として登録
    KEEP_AS_SOURCE_ERROR = "keep_as_source_error"  # 元データの誤りとして扱う（コードの対象名で置換）
    FALSE_POSITIVE = "false_positive"  # 問題なし（誤検出）
    REPLACE_TERM = "replace_term"  # 選んだ置換先で置換する（文章内の語）
    REGISTER_VALUE = "register_value"  # 値（電話番号・メール等）として登録して置換する


ALLOWED: dict[ReviewKind, set[Decision]] = {
    ReviewKind.NEW_ENTITY: {Decision.REGISTER_NEW, Decision.LINK_EXISTING},
    ReviewKind.CODE_MULTI_NAME: {Decision.ADD_ALIAS, Decision.KEEP_AS_SOURCE_ERROR},
    ReviewKind.NAME_AMBIGUOUS: {Decision.LINK_EXISTING, Decision.REGISTER_NEW},
    ReviewKind.SIMILAR_NAME: {Decision.LINK_EXISTING, Decision.REGISTER_NEW},
    ReviewKind.RESIDUAL_SHORT: {Decision.FALSE_POSITIVE},
    ReviewKind.FREETEXT_SHORT: {Decision.REPLACE_TERM, Decision.FALSE_POSITIVE},
    ReviewKind.FREETEXT_AMBIGUOUS: {Decision.REPLACE_TERM},
    ReviewKind.FREETEXT_CANDIDATE: {Decision.REGISTER_NEW, Decision.LINK_EXISTING, Decision.REGISTER_VALUE,
                                    Decision.FALSE_POSITIVE},
}


# 作業をまたいで引き継ぐ判断の種類。確定した作業の判断を記録し、次の作業で同じ項目に自動で適用する
# （画面に「前回の判断」と表示し、利用者は取り消して判断し直せる）
# 文章内の判断は、置換先が取り消し・統合されていれば計画の作成時に判断し直しを求める
REMEMBERED = {ReviewKind.CODE_MULTI_NAME, ReviewKind.NAME_AMBIGUOUS, ReviewKind.FREETEXT_SHORT,
              ReviewKind.FREETEXT_AMBIGUOUS, ReviewKind.FREETEXT_CANDIDATE}
FREETEXT_KINDS = {ReviewKind.FREETEXT_SHORT, ReviewKind.FREETEXT_AMBIGUOUS, ReviewKind.FREETEXT_CANDIDATE}


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
    created = conn.execute(
        "INSERT OR IGNORE INTO review_item (run_id, kind, location, subject, state, created_at, item_key) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (run_id, kind.value, json.dumps(location, ensure_ascii=False), json.dumps(subject, ensure_ascii=False),
         _now(), item_key),
    ).rowcount
    if created and kind in REMEMBERED:
        _apply_memory(conn, run_id, item_key)
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


def _apply_memory(conn: sqlite3.Connection, run_id: int, item_key: str) -> None:
    row = conn.execute("SELECT decision FROM decision_memory WHERE item_key = ?", (item_key,)).fetchone()
    if row is None:
        return
    decision = json.loads(row[0])
    entity_id = decision.get("entity_id")
    if entity_id is not None:
        status = conn.execute("SELECT status FROM entity WHERE id = ?", (entity_id,)).fetchone()
        if status is None or status[0] not in ("draft", "confirmed"):
            return  # 前回選んだ対象が取り消し・統合された場合は引き継がない
    item_id = conn.execute(
        "SELECT id FROM review_item WHERE run_id = ? AND item_key = ?", (run_id, item_key)
    ).fetchone()[0]
    set_decision(conn, item_id, {**decision, "remembered": True})


def remember_run(conn: sqlite3.Connection, run_id: int) -> int:
    """確定する作業の判断のうち、引き継ぐ種類のものを記録する。記録した件数を返す。"""
    rows = []
    for kind, item_key, decision in conn.execute(
        "SELECT kind, item_key, decision FROM review_item WHERE run_id = ? AND state = 'decided'", (run_id,)
    ).fetchall():
        if ReviewKind(kind) not in REMEMBERED:
            continue
        d = json.loads(decision)
        d.pop("remembered", None)
        if ReviewKind(kind) in FREETEXT_KINDS:
            pass  # 判断（置換先・問題なし）をそのまま引き継ぐ
        elif ReviewKind(kind) is ReviewKind.NAME_AMBIGUOUS:
            d = {"decision": Decision.LINK_EXISTING.value, "entity_id": d["entity_id"]}
        elif d.get("decision") != Decision.KEEP_AS_SOURCE_ERROR.value:
            continue  # 別名の登録は対応表に残るため記録不要
        rows.append((item_key, json.dumps(d, ensure_ascii=False), run_id, _now()))
    conn.executemany(
        "INSERT OR REPLACE INTO decision_memory (item_key, decision, created_run_id, created_at) VALUES (?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def forget(conn: sqlite3.Connection, item_id: int) -> None:
    """引き継いだ判断を取り消し、項目を未確認に戻す（記録も消す）。"""
    item_key = conn.execute("SELECT item_key FROM review_item WHERE id = ?", (item_id,)).fetchone()[0]
    conn.execute("DELETE FROM decision_memory WHERE item_key = ?", (item_key,))
    conn.execute("UPDATE review_item SET decision = NULL, state = 'open', decided_at = NULL WHERE id = ?", (item_id,))
