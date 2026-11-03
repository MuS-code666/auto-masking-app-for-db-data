"""対応表の操作（設計書 03 章 2、05 章 2〜3）。

番号（名称番号・コード番号・値番号）は確保した時点で進め、戻さない。
仮登録（draft）は作成した作業（run）を記録し、作業の確定で confirmed、取消で void にする。
すべて呼び出し側のトランザクション内で使う。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from maskapp.entity.codes import OutputRule, derive_rule
from maskapp.entity.normalize import normalize_name

ACTIVE = ("draft", "confirmed")
_ACTIVE_SQL = "('draft', 'confirmed')"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Master:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---- 参照 ----

    def entity_by_code(self, scheme_id: int, code: str) -> str | None:
        row = self.conn.execute(
            f"SELECT c.entity_id FROM entity_code c JOIN entity e ON e.id = c.entity_id "
            f"WHERE c.code_scheme_id = ? AND c.original_code = ? AND c.status IN {_ACTIVE_SQL} "
            f"AND e.status IN {_ACTIVE_SQL}",
            (scheme_id, code),
        ).fetchone()
        return row[0] if row else None

    def entities_by_name(self, entity_type: str, name: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            f"SELECT DISTINCT n.entity_id FROM entity_name n JOIN entity e ON e.id = n.entity_id "
            f"WHERE e.entity_type_id = ? AND n.original_name = ? AND n.status IN {_ACTIVE_SQL} "
            f"AND e.status IN {_ACTIVE_SQL} ORDER BY e.name_seq",
            (entity_type, name),
        )]

    def entities_by_normalized(self, entity_type: str, name: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            f"SELECT DISTINCT n.entity_id FROM entity_name n JOIN entity e ON e.id = n.entity_id "
            f"WHERE e.entity_type_id = ? AND n.normalized_name = ? AND n.status IN {_ACTIVE_SQL} "
            f"AND e.status IN {_ACTIVE_SQL} ORDER BY e.name_seq",
            (entity_type, normalize_name(name)),
        )]

    def names_of(self, entity_id: str) -> set[str]:
        return {r[0] for r in self.conn.execute(
            f"SELECT original_name FROM entity_name WHERE entity_id = ? AND status IN {_ACTIVE_SQL}",
            (entity_id,),
        )}

    def masked_name(self, entity_id: str) -> str:
        return self.conn.execute("SELECT masked_name FROM entity WHERE id = ?", (entity_id,)).fetchone()[0]

    def masked_code(self, entity_id: str, scheme_id: int) -> str | None:
        row = self.conn.execute(
            f"SELECT masked_code FROM entity_code WHERE entity_id = ? AND code_scheme_id = ? "
            f"AND status IN {_ACTIVE_SQL} ORDER BY code_seq LIMIT 1",
            (entity_id, scheme_id),
        ).fetchone()
        return row[0] if row else None

    def entity_status(self, entity_id: str) -> tuple[str, int | None]:
        return self.conn.execute(
            "SELECT status, created_run_id FROM entity WHERE id = ?", (entity_id,)
        ).fetchone()

    # ---- 仮登録（番号の確保） ----

    def create_entity(self, entity_type: str, run_id: int) -> str:
        prefix, digits, seq = self.conn.execute(
            "SELECT name_prefix, name_digits, next_name_seq FROM entity_type WHERE id = ?", (entity_type,)
        ).fetchone()
        self.conn.execute("UPDATE entity_type SET next_name_seq = ? WHERE id = ?", (seq + 1, entity_type))
        entity_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO entity (id, entity_type_id, name_seq, masked_name, status, created_run_id) "
            "VALUES (?, ?, ?, ?, 'draft', ?)",
            (entity_id, entity_type, seq, f"{prefix}{seq:0{digits}d}", run_id),
        )
        return entity_id

    def add_name(self, entity_id: str, name: str, kind: str, run_id: int) -> None:
        if name in self.names_of(entity_id):
            return
        self.conn.execute(
            "INSERT INTO entity_name (entity_id, original_name, normalized_name, kind, status, created_run_id) "
            "VALUES (?, ?, ?, ?, 'draft', ?)",
            (entity_id, name, normalize_name(name), kind, run_id),
        )

    def output_rule(self, scheme_id: int, observed: list[str]) -> OutputRule:
        """コード体系の生成規則。未設定なら観測コードから作って保存する（以後は変えない）。"""
        text = self.conn.execute("SELECT output_rule FROM code_scheme WHERE id = ?", (scheme_id,)).fetchone()[0]
        rule = OutputRule.from_json(text)
        if rule is None:
            existing = [r[0] for r in self.conn.execute(
                "SELECT original_code FROM entity_code WHERE code_scheme_id = ?", (scheme_id,)
            )]
            rule = derive_rule(existing + observed)
            self.conn.execute(
                "UPDATE code_scheme SET output_rule = ?, capacity = ? WHERE id = ?",
                (rule.to_json(), rule.capacity, scheme_id),
            )
        return rule

    def add_code(self, entity_id: str, scheme_id: int, code: str, run_id: int, rule: OutputRule) -> str:
        """元コードを対象に登録し、変換後コードを確保する。元コードと同じ値は使わない。"""
        seq = self.conn.execute("SELECT next_code_seq FROM code_scheme WHERE id = ?", (scheme_id,)).fetchone()[0]
        originals = {r[0] for r in self.conn.execute(
            "SELECT original_code FROM entity_code WHERE code_scheme_id = ?", (scheme_id,)
        )}
        originals.add(code)
        masked = rule.make(seq)
        while masked in originals:
            seq += 1
            masked = rule.make(seq)
        self.conn.execute("UPDATE code_scheme SET next_code_seq = ? WHERE id = ?", (seq + 1, scheme_id))
        self.conn.execute(
            "INSERT INTO entity_code (entity_id, code_scheme_id, original_code, masked_code, code_seq, status, "
            "created_run_id) VALUES (?, ?, ?, ?, ?, 'draft', ?)",
            (entity_id, scheme_id, code, masked, seq, run_id),
        )
        return masked

    def code_usage(self, scheme_id: int) -> tuple[int, int | None]:
        """(使用済み件数, 生成可能件数)。"""
        used, capacity = self.conn.execute(
            "SELECT next_code_seq - 1, capacity FROM code_scheme WHERE id = ?", (scheme_id,)
        ).fetchone()
        return used, capacity

    def value_token(self, info_type: str, value: str, run_id: int) -> str:
        row = self.conn.execute(
            f"SELECT masked_value FROM value_token WHERE info_type = ? AND original_value = ? "
            f"AND status IN {_ACTIVE_SQL}",
            (info_type, value),
        ).fetchone()
        if row:
            return row[0]
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM value_token WHERE info_type = ?", (info_type,)
        ).fetchone()[0]
        masked = make_value_token(info_type, seq)
        self.conn.execute(
            "INSERT INTO value_token (info_type, original_value, masked_value, seq, status, created_run_id) "
            "VALUES (?, ?, ?, ?, 'draft', ?)",
            (info_type, value, masked, seq, run_id),
        )
        return masked

    # ---- 判断の反映 ----

    def move_to(self, draft_id: str, target_id: str) -> None:
        """仮登録の対象を既存の対象に紐付ける。仮登録の番号は欠番になる。"""
        self.conn.execute("UPDATE entity_code SET entity_id = ? WHERE entity_id = ?", (target_id, draft_id))
        for (name,) in self.conn.execute(
            f"SELECT original_name FROM entity_name WHERE entity_id = ? AND status IN {_ACTIVE_SQL}", (draft_id,)
        ).fetchall():
            if name in self.names_of(target_id):
                self.conn.execute(
                    "UPDATE entity_name SET status = 'void' WHERE entity_id = ? AND original_name = ?",
                    (draft_id, name),
                )
            else:
                self.conn.execute(
                    "UPDATE entity_name SET entity_id = ?, kind = 'alias' WHERE entity_id = ? AND original_name = ?",
                    (target_id, draft_id, name),
                )
        self.conn.execute("UPDATE entity SET status = 'void' WHERE id = ?", (draft_id,))

    # ---- 作業の確定・取消 ----

    def confirm_run(self, run_id: int) -> None:
        """作業で作った仮登録をすべて確定し、変更履歴を記録する（対応表版が進む）。"""
        now = _now()
        events: list[tuple[str, str | None, dict]] = []
        for (eid,) in self.conn.execute(
            "SELECT id FROM entity WHERE created_run_id = ? AND status = 'draft' ORDER BY entity_type_id, name_seq",
            (run_id,),
        ).fetchall():
            events.append(("create", eid, {}))
        new_entities = {e[1] for e in events}
        for eid, name, kind in self.conn.execute(
            "SELECT entity_id, original_name, kind FROM entity_name WHERE created_run_id = ? AND status = 'draft' "
            "ORDER BY id",
            (run_id,),
        ).fetchall():
            if eid not in new_entities or kind == "alias":
                events.append(("alias_add", eid, {"name": name}))
        for eid, scheme, code in self.conn.execute(
            "SELECT entity_id, code_scheme_id, original_code FROM entity_code WHERE created_run_id = ? "
            "AND status = 'draft' ORDER BY id",
            (run_id,),
        ).fetchall():
            if eid not in new_entities:
                events.append(("code_add", eid, {"scheme": scheme, "code": code}))
        for table in ("entity", "entity_code", "entity_name", "value_token"):
            self.conn.execute(
                f"UPDATE {table} SET status = 'confirmed' WHERE created_run_id = ? AND status = 'draft'", (run_id,)
            )
        self.conn.execute(
            "UPDATE entity SET confirmed_run_id = ? WHERE created_run_id = ? AND status = 'confirmed' "
            "AND confirmed_run_id IS NULL",
            (run_id, run_id),
        )
        self.conn.executemany(
            "INSERT INTO master_event (run_id, kind, entity_id, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            [(run_id, kind, eid, json.dumps(payload, ensure_ascii=False), now) for kind, eid, payload in events],
        )

    def void_run(self, run_id: int) -> int:
        """作業で作った仮登録を取り消す。番号は欠番となり再利用しない。取り消した対象数を返す。"""
        count = self.conn.execute(
            "SELECT COUNT(*) FROM entity WHERE created_run_id = ? AND status = 'draft'", (run_id,)
        ).fetchone()[0]
        for table in ("entity", "entity_code", "entity_name", "value_token"):
            self.conn.execute(
                f"UPDATE {table} SET status = 'void' WHERE created_run_id = ? AND status = 'draft'", (run_id,)
            )
        return count


def make_value_token(info_type: str, seq: int) -> str:
    """住所・電話・メール等の置換値。形式を保ったダミー値にする（設計書 01 章 2.3）。"""
    kind = info_type.upper()
    if kind == "PHONE":
        return f"000-0000-{seq:04d}"
    if kind == "EMAIL":
        return f"user{seq:04d}@example.invalid"
    if kind == "ADDRESS":
        return f"住所{seq:04d}"
    if kind == "POSTAL":
        return f"000-{seq:04d}"
    return f"{info_type}{seq:04d}"
