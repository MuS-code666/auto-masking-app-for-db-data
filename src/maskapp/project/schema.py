"""project.db のスキーマと移行（設計書 03 データモデル）。

スキーマの版は SQLite の PRAGMA user_version で管理する。
版を上げるときは MIGRATIONS に関数を追加し、既存の関数は変更しない。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

SCHEMA_V1 = """
CREATE TABLE project_info (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    project_uuid  TEXT NOT NULL,
    name          TEXT NOT NULL,
    color         TEXT,
    created_at    TEXT NOT NULL,
    created_by_app_version TEXT NOT NULL
);

-- ===== 対応表系（B） =====

CREATE TABLE entity_type (
    id            TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    name_prefix   TEXT NOT NULL,
    name_digits   INTEGER NOT NULL DEFAULT 4 CHECK (name_digits BETWEEN 1 AND 12),
    next_name_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_name_seq >= 1)
);

CREATE TABLE code_scheme (
    id             INTEGER PRIMARY KEY,
    entity_type_id TEXT NOT NULL REFERENCES entity_type(id),
    display_name   TEXT NOT NULL,
    components     INTEGER NOT NULL DEFAULT 1 CHECK (components >= 1),
    format_rule    TEXT NOT NULL DEFAULT '{}',
    output_rule    TEXT NOT NULL DEFAULT '{}',
    next_code_seq  INTEGER NOT NULL DEFAULT 1 CHECK (next_code_seq >= 1),
    capacity       INTEGER
);

-- ===== 変換設定系（A） =====

CREATE TABLE config_version (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER REFERENCES config_version(id),
    created_at TEXT NOT NULL,
    note       TEXT
);

-- ===== 作業 =====

CREATE TABLE run (
    id                   INTEGER PRIMARY KEY,
    kind                 TEXT NOT NULL CHECK (kind IN ('convert', 'admin')),
    state                TEXT NOT NULL CHECK (state IN
                           ('draft', 'planned', 'staged', 'verified', 'committed',
                            'published', 'cancelled', 'failed')),
    config_version_id    INTEGER REFERENCES config_version(id),
    master_rev_base      INTEGER,
    master_rev_committed INTEGER,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- ===== 対応表系（B）続き =====

CREATE TABLE entity (
    id               TEXT PRIMARY KEY,
    entity_type_id   TEXT NOT NULL REFERENCES entity_type(id),
    name_seq         INTEGER NOT NULL,
    masked_name      TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'void', 'merged')),
    merged_into      TEXT REFERENCES entity(id),
    created_run_id   INTEGER REFERENCES run(id),
    confirmed_run_id INTEGER REFERENCES run(id),
    UNIQUE (entity_type_id, name_seq),
    UNIQUE (entity_type_id, masked_name)
);

CREATE TABLE entity_code (
    id                 INTEGER PRIMARY KEY,
    entity_id          TEXT NOT NULL REFERENCES entity(id),
    code_scheme_id     INTEGER NOT NULL REFERENCES code_scheme(id),
    original_code      TEXT NOT NULL,
    original_cell_kind TEXT CHECK (original_cell_kind IN ('text', 'number')),
    masked_code        TEXT NOT NULL,
    code_seq           INTEGER NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'void')),
    UNIQUE (code_scheme_id, masked_code),
    UNIQUE (code_scheme_id, code_seq)
);
-- 有効な（取消されていない）元コードは体系内で1対象のみ
CREATE UNIQUE INDEX ux_entity_code_active
    ON entity_code (code_scheme_id, original_code) WHERE status IN ('draft', 'confirmed');

CREATE TABLE entity_name (
    id              INTEGER PRIMARY KEY,
    entity_id       TEXT NOT NULL REFERENCES entity(id),
    original_name   TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('primary', 'alias', 'observed')),
    status          TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'void'))
);
-- 名称は複数の対象で重複してよい（同名の別顧客）。同じ対象内では重複しない
CREATE UNIQUE INDEX ux_entity_name_active
    ON entity_name (entity_id, original_name) WHERE status IN ('draft', 'confirmed');
CREATE INDEX ix_entity_name_lookup ON entity_name (original_name);
CREATE INDEX ix_entity_name_normalized ON entity_name (normalized_name);

CREATE TABLE value_token (
    id             INTEGER PRIMARY KEY,
    info_type      TEXT NOT NULL,
    original_value TEXT NOT NULL,
    masked_value   TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'void')),
    UNIQUE (info_type, seq),
    UNIQUE (info_type, masked_value)
);
CREATE UNIQUE INDEX ux_value_token_active
    ON value_token (info_type, original_value) WHERE status IN ('draft', 'confirmed');

CREATE TABLE term (
    id            INTEGER PRIMARY KEY,
    original_term TEXT NOT NULL,
    entity_id     TEXT REFERENCES entity(id),
    info_type     TEXT,
    action        TEXT NOT NULL CHECK (action IN ('replace', 'keep')),
    scope         TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'void'))
);

CREATE TABLE master_event (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES run(id),
    kind       TEXT NOT NULL CHECK (kind IN
                 ('create', 'confirm', 'void', 'alias_add', 'alias_remove', 'merge',
                  'split', 'rename_masked', 'code_add')),
    entity_id  TEXT REFERENCES entity(id),
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

-- ===== 変換設定系（A）続き =====

CREATE TABLE source_file (
    id                INTEGER PRIMARY KEY,
    original_filename TEXT NOT NULL,
    stored_path       TEXT NOT NULL,
    sha256            TEXT NOT NULL UNIQUE,
    size              INTEGER NOT NULL,
    format            TEXT NOT NULL CHECK (format IN ('csv', 'xlsx')),
    import_run_id     INTEGER REFERENCES run(id),
    imported_at       TEXT NOT NULL
);

CREATE TABLE sheet (
    id             INTEGER PRIMARY KEY,
    source_file_id INTEGER NOT NULL REFERENCES source_file(id),
    idx            INTEGER NOT NULL,
    name           TEXT NOT NULL,
    visibility     TEXT NOT NULL CHECK (visibility IN ('visible', 'hidden', 'veryHidden')),
    UNIQUE (source_file_id, idx)
);

CREATE TABLE table_template (
    id                INTEGER PRIMARY KEY,
    config_version_id INTEGER NOT NULL REFERENCES config_version(id),
    display_name      TEXT NOT NULL,
    header_signature  TEXT NOT NULL DEFAULT '[]',
    region_rule       TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE column_group (
    id                INTEGER PRIMARY KEY,
    config_version_id INTEGER NOT NULL REFERENCES config_version(id),
    display_name      TEXT NOT NULL,
    entity_type_id    TEXT REFERENCES entity_type(id),
    code_scheme_id    INTEGER REFERENCES code_scheme(id),
    role              TEXT
);

CREATE TABLE column_rule (
    id              INTEGER PRIMARY KEY,
    template_id     INTEGER NOT NULL REFERENCES table_template(id),
    header_text     TEXT NOT NULL,
    position_hint   INTEGER,
    info_type       TEXT,
    action          TEXT NOT NULL CHECK (action IN ('keep', 'replace', 'clear', 'freetext')),
    entity_type_id  TEXT REFERENCES entity_type(id),
    code_scheme_id  INTEGER REFERENCES code_scheme(id),
    role            TEXT,
    column_group_id INTEGER REFERENCES column_group(id),
    header_action   TEXT NOT NULL DEFAULT 'keep',
    review_state    TEXT NOT NULL DEFAULT 'unreviewed'
                      CHECK (review_state IN ('unreviewed', 'confirmed', 'changed'))
);

CREATE TABLE table_region (
    id             INTEGER PRIMARY KEY,
    sheet_id       INTEGER NOT NULL REFERENCES sheet(id),
    template_id    INTEGER REFERENCES table_template(id),
    header_rows    TEXT NOT NULL DEFAULT '[]',
    data_first_row INTEGER,
    data_last_row  INTEGER,
    first_col      INTEGER,
    last_col       INTEGER,
    region_kind    TEXT NOT NULL CHECK (region_kind IN ('data', 'note', 'total'))
);

CREATE TABLE source_column (
    id           INTEGER PRIMARY KEY,
    region_id    INTEGER NOT NULL REFERENCES table_region(id),
    col_index    INTEGER NOT NULL,
    header_text  TEXT,
    type_profile TEXT NOT NULL DEFAULT '{}',
    fingerprint  TEXT,
    UNIQUE (region_id, col_index)
);

CREATE TABLE relation (
    id                INTEGER PRIMARY KEY,
    config_version_id INTEGER NOT NULL REFERENCES config_version(id),
    from_template_id  INTEGER NOT NULL REFERENCES table_template(id),
    from_columns      TEXT NOT NULL,
    to_template_id    INTEGER NOT NULL REFERENCES table_template(id),
    to_columns        TEXT NOT NULL,
    expected          TEXT,
    state             TEXT NOT NULL CHECK (state IN ('suggested', 'confirmed', 'rejected'))
);

-- ===== 作業 続き =====

CREATE TABLE run_input (
    run_id         INTEGER NOT NULL REFERENCES run(id),
    source_file_id INTEGER NOT NULL REFERENCES source_file(id),
    sha256         TEXT NOT NULL,
    PRIMARY KEY (run_id, source_file_id)
);

CREATE TABLE run_output (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES run(id),
    source_file_id  INTEGER NOT NULL REFERENCES source_file(id),
    output_filename TEXT NOT NULL,
    output_path     TEXT,
    sha256          TEXT NOT NULL,
    size            INTEGER NOT NULL,
    published_at    TEXT
);

CREATE TABLE review_item (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES run(id),
    kind       TEXT NOT NULL,
    location   TEXT NOT NULL DEFAULT '{}',
    subject    TEXT NOT NULL DEFAULT '{}',
    decision   TEXT,
    state      TEXT NOT NULL CHECK (state IN ('open', 'decided')),
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE finding (
    id       INTEGER PRIMARY KEY,
    run_id   INTEGER NOT NULL REFERENCES run(id),
    origin   TEXT NOT NULL CHECK (origin IN ('source', 'transform', 'environment')),
    severity TEXT NOT NULL CHECK (severity IN ('block', 'warn', 'info')),
    kind     TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '{}',
    detail   TEXT NOT NULL DEFAULT '{}'
);
"""

# 初期の種類（設計書 01 章 2.3 の初期設定例）
DEFAULT_ENTITY_TYPES = [
    ("CUSTOMER", "顧客", "顧客"),
    ("PRODUCT", "製品", "製品"),
    ("MATERIAL", "原料", "原料"),
    ("SUPPLIER", "仕入先", "仕入先"),
    ("PERSON", "人物", "人物"),
]


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """文ごとに実行する。

    executescript() は実行前に暗黙の COMMIT を行い、移行を1トランザクションにできないため使わない。
    """
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            conn.execute(buffer)
            buffer = ""
    if buffer.strip():
        raise ValueError("incomplete SQL statement in schema script")


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    _execute_script(conn, SCHEMA_V1)
    conn.executemany(
        "INSERT INTO entity_type (id, display_name, name_prefix) VALUES (?, ?, ?)",
        DEFAULT_ENTITY_TYPES,
    )


SCHEMA_V2 = """
-- 行内で組になる列（コード列と名称列）の識別
ALTER TABLE column_rule ADD COLUMN binding TEXT;
-- 作業に含めたファイルの順序と適用した表の定義名（定義名は設定版をまたいで同じ表を指す）
ALTER TABLE run_input ADD COLUMN template_name TEXT;
ALTER TABLE run_input ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
-- 取込時に検出した形式（CSV の文字コード・区切り等）と元の保存場所（上書き防止の判定用）
ALTER TABLE source_file ADD COLUMN format_info TEXT NOT NULL DEFAULT '{}';
ALTER TABLE source_file ADD COLUMN original_dir TEXT;
-- 仮登録をどの作業で作ったか（取消・確定の対象を特定する）
ALTER TABLE entity_code ADD COLUMN created_run_id INTEGER REFERENCES run(id);
ALTER TABLE entity_name ADD COLUMN created_run_id INTEGER REFERENCES run(id);
ALTER TABLE value_token ADD COLUMN created_run_id INTEGER REFERENCES run(id);
-- 要確認項目の重複防止
ALTER TABLE review_item ADD COLUMN item_key TEXT;
CREATE UNIQUE INDEX ux_review_item_key ON review_item (run_id, item_key);
-- 作業領域に書き出したファイル
ALTER TABLE run_output ADD COLUMN staged_path TEXT;
ALTER TABLE run_output ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
"""


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    _execute_script(conn, SCHEMA_V2)


SCHEMA_V3 = """
-- 作業に含めた xlsx の扱いの選択（コメントの除去、画像を検査しないことの確認など）
ALTER TABLE run_input ADD COLUMN options TEXT NOT NULL DEFAULT '{}';
-- 作業で表の定義を適用するシート（CSV はシート 0）。1つの xlsx の複数シートに別々の定義を適用できる
CREATE TABLE run_sheet (
    run_id         INTEGER NOT NULL REFERENCES run(id),
    source_file_id INTEGER NOT NULL REFERENCES source_file(id),
    sheet_idx      INTEGER NOT NULL,
    template_name  TEXT NOT NULL,
    PRIMARY KEY (run_id, source_file_id, sheet_idx)
);
INSERT INTO run_sheet (run_id, source_file_id, sheet_idx, template_name)
    SELECT run_id, source_file_id, 0, template_name FROM run_input WHERE template_name IS NOT NULL;
"""


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    _execute_script(conn, SCHEMA_V3)


SCHEMA_V4 = """
-- 画面の作業状態（設定の下書き等）。元の値を含み得るためプロジェクト内にだけ置く
CREATE TABLE ui_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 作業をまたいで引き継ぐ判断（「元データの誤りとして扱う」、同名の対象からの選択）。確定した作業の判断だけを記録する
CREATE TABLE decision_memory (
    item_key       TEXT PRIMARY KEY,
    decision       TEXT NOT NULL,
    created_run_id INTEGER REFERENCES run(id),
    created_at     TEXT NOT NULL
);
"""


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    _execute_script(conn, SCHEMA_V4)


SCHEMA_V5 = """
-- AI（端末内の LLM）で文章を検査した結果。文章はハッシュで識別し、同じ文章は次の作業でも再利用する
CREATE TABLE ai_text (
    text_hash  TEXT NOT NULL,
    model      TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    candidates TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (text_hash, model)
);
"""


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    _execute_script(conn, SCHEMA_V5)


# MIGRATIONS[i] は版 i から i+1 への移行
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migrate_to_v1, _migrate_to_v2, _migrate_to_v3, _migrate_to_v4, _migrate_to_v5,
]
CURRENT_VERSION = len(MIGRATIONS)


def get_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> tuple[int, int]:
    """必要な移行を1つのトランザクションで適用し、(移行前, 移行後) の版を返す。"""
    before = get_version(conn)
    if before >= CURRENT_VERSION:
        return before, before
    conn.execute("BEGIN IMMEDIATE")
    try:
        for version in range(before, CURRENT_VERSION):
            MIGRATIONS[version](conn)
        conn.execute(f"PRAGMA user_version = {CURRENT_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return before, CURRENT_VERSION
