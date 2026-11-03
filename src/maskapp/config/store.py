"""設定の保存と読込。設定は版単位で不変で、保存のたびに新しい版を作る。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from maskapp.config.model import (
    Action,
    ColumnSpec,
    Config,
    ConfigError,
    OutsideAction,
    RelationSpec,
    Role,
    TemplateSpec,
    normalize_header,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def ensure_code_scheme(conn: sqlite3.Connection, entity_type: str, display_name: str) -> int:
    row = conn.execute(
        "SELECT id FROM code_scheme WHERE entity_type_id = ? AND display_name = ?",
        (entity_type, display_name),
    ).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO code_scheme (entity_type_id, display_name) VALUES (?, ?)", (entity_type, display_name)
    ).lastrowid


def save_config(
    conn: sqlite3.Connection,
    templates: list[TemplateSpec],
    relations: list[RelationSpec] = (),
    note: str | None = None,
) -> int:
    """設定一式を新しい版として保存し、版番号を返す。呼び出し側のトランザクション内で使う。"""
    names = [t.name for t in templates]
    if len(set(names)) != len(names):
        raise ConfigError("duplicate template names")
    known_types = {r[0] for r in conn.execute("SELECT id FROM entity_type")}
    for t in templates:
        t.validate()
        for c in t.columns:
            if c.entity_type and c.entity_type not in known_types:
                raise ConfigError(f"unknown entity type '{c.entity_type}'")
    by_name = {t.name: t for t in templates}
    for rel in relations:
        for tname, col in ((rel.from_template, rel.from_column), (rel.to_template, rel.to_column)):
            if tname not in by_name:
                raise ConfigError(f"relation refers to unknown template '{tname}'")
            by_name[tname].column(col)

    parent = conn.execute("SELECT MAX(id) FROM config_version").fetchone()[0]
    version = conn.execute(
        "INSERT INTO config_version (parent_id, created_at, note) VALUES (?, ?, ?)", (parent, _now(), note)
    ).lastrowid
    template_ids: dict[str, int] = {}
    for t in templates:
        tid = conn.execute(
            "INSERT INTO table_template (config_version_id, display_name, header_signature, region_rule) "
            "VALUES (?, ?, ?, ?)",
            (version, t.name, json.dumps(t.headers, ensure_ascii=False),
             json.dumps({"header_row": t.header_row, "skip_total_rows": t.skip_total_rows,
                         "outside_action": t.outside_action.value})),
        ).lastrowid
        template_ids[t.name] = tid
        for pos, c in enumerate(t.columns):
            scheme_id = ensure_code_scheme(conn, c.entity_type, c.code_scheme) if c.code_scheme else None
            conn.execute(
                "INSERT INTO column_rule (template_id, header_text, position_hint, info_type, action, "
                "entity_type_id, code_scheme_id, role, binding, review_state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed')",
                (tid, c.header, pos, c.info_type, c.action.value, c.entity_type, scheme_id,
                 c.role.value if c.role else None, c.binding),
            )
    for rel in relations:
        conn.execute(
            "INSERT INTO relation (config_version_id, from_template_id, from_columns, to_template_id, "
            "to_columns, expected, state) VALUES (?, ?, ?, ?, ?, NULL, 'confirmed')",
            (version, template_ids[rel.from_template], json.dumps([rel.from_column], ensure_ascii=False),
             template_ids[rel.to_template], json.dumps([rel.to_column], ensure_ascii=False)),
        )
    return version


def load_config(conn: sqlite3.Connection, version_id: int | None = None) -> Config:
    if version_id is None:
        version_id = conn.execute("SELECT MAX(id) FROM config_version").fetchone()[0]
        if version_id is None:
            return Config(version_id=None)
    templates: dict[str, TemplateSpec] = {}
    names_by_id: dict[int, str] = {}
    for tid, name, region_rule in conn.execute(
        "SELECT id, display_name, region_rule FROM table_template WHERE config_version_id = ? ORDER BY id",
        (version_id,),
    ).fetchall():
        columns = [
            ColumnSpec(
                header=header,
                action=Action(action),
                info_type=info_type,
                entity_type=entity_type,
                role=Role(role) if role else None,
                code_scheme=scheme,
                binding=binding,
            )
            for header, action, info_type, entity_type, role, scheme, binding in conn.execute(
                "SELECT r.header_text, r.action, r.info_type, r.entity_type_id, r.role, s.display_name, r.binding "
                "FROM column_rule r LEFT JOIN code_scheme s ON s.id = r.code_scheme_id "
                "WHERE r.template_id = ? ORDER BY r.position_hint",
                (tid,),
            )
        ]
        rule = json.loads(region_rule)
        templates[name] = TemplateSpec(name, columns, rule.get("header_row", 0), rule.get("skip_total_rows", False),
                                       OutsideAction(rule.get("outside_action", "keep")))
        names_by_id[tid] = name
    relations = [
        RelationSpec(names_by_id[f], json.loads(fc)[0], names_by_id[t], json.loads(tc)[0])
        for f, fc, t, tc in conn.execute(
            "SELECT from_template_id, from_columns, to_template_id, to_columns FROM relation "
            "WHERE config_version_id = ? AND state = 'confirmed' ORDER BY id",
            (version_id,),
        )
    ]
    return Config(version_id, templates, relations)


def match_template(config: Config, records: list[list[str]]) -> str | None:
    """取込ファイルの見出しと完全に一致する表の定義名を返す（ファイル名は使わない）。"""
    hits = []
    for t in config.templates.values():
        if t.header_row >= len(records):
            continue
        headers = [normalize_header(h) for h in records[t.header_row]]
        if headers == [normalize_header(h) for h in t.headers]:
            hits.append(t.name)
    if len(hits) > 1:
        raise ConfigError("file matches more than one table definition")
    return hits[0] if hits else None
