from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from maskapp.project import (
    InvalidProjectError,
    Project,
    ProjectExistsError,
    ProjectLockedError,
    SchemaTooNewError,
    schema,
)
from maskapp.project.store import SUBDIRS

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path: Path):
    with Project.create(tmp_path, "試験プロジェクト", color="#3366cc") as p:
        yield p


def test_create_builds_folder_layout_and_schema(tmp_path: Path):
    with Project.create(tmp_path, "案件A") as p:
        assert p.root == tmp_path / "案件A"
        for sub in SUBDIRS:
            assert (p.root / sub).is_dir()
        assert p.temp_dir.is_dir()
        assert p.schema_version == schema.CURRENT_VERSION
        assert p.name == "案件A"
        types = [r[0] for r in p.conn.execute("SELECT id FROM entity_type ORDER BY id")]
        assert types == sorted(t[0] for t in schema.DEFAULT_ENTITY_TYPES)
        assert p.master_revision() == 0


def test_reopen_keeps_identity(tmp_path: Path):
    with Project.create(tmp_path, "案件A") as p:
        project_uuid = p.uuid
    with Project.open(tmp_path / "案件A") as p:
        assert p.uuid == project_uuid


def test_create_refuses_existing_folder(tmp_path: Path):
    Project.create(tmp_path, "案件A").close()
    with pytest.raises(ProjectExistsError):
        Project.create(tmp_path, "案件A")


@pytest.mark.parametrize("name", ["", "   ", "a/b", "a\\b", "a:b", "CON", "nul.txt", "末尾.", "x" * 81])
def test_invalid_project_names(tmp_path: Path, name: str):
    with pytest.raises(ValueError):
        Project.create(tmp_path, name)


def test_second_open_in_same_process_is_locked(project: Project):
    with pytest.raises(ProjectLockedError):
        Project.open(project.root, lock_timeout=0)


def test_lock_released_on_close(tmp_path: Path):
    p = Project.create(tmp_path, "案件A")
    p.close()
    Project.open(tmp_path / "案件A").close()


def test_lock_held_by_other_process_and_released_when_it_dies(tmp_path: Path):
    Project.create(tmp_path, "案件A").close()
    root = tmp_path / "案件A"
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(REPO_ROOT / "src")!r})
        from maskapp.project import Project
        p = Project.open({str(root)!r})
        print("locked", flush=True)
        time.sleep(60)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(ProjectLockedError):
            Project.open(root, lock_timeout=0)
    finally:
        proc.kill()  # 異常終了を模擬
        proc.wait(timeout=10)
    # Windows ではロック解放が即時とは限らないため待ち時間を長めに取る
    Project.open(root, lock_timeout=10).close()


def test_transaction_rolls_back_on_error(project: Project):
    with pytest.raises(RuntimeError, match="boom"):
        with project.transaction() as conn:
            conn.execute(
                "INSERT INTO entity_type (id, display_name, name_prefix) VALUES ('X', 'x', 'x')"
            )
            raise RuntimeError("boom")
    assert project.conn.execute("SELECT COUNT(*) FROM entity_type WHERE id='X'").fetchone()[0] == 0


def test_transaction_commits(project: Project):
    with project.transaction() as conn:
        conn.execute("INSERT INTO entity_type (id, display_name, name_prefix) VALUES ('X', 'x', 'x')")
    assert project.conn.execute("SELECT COUNT(*) FROM entity_type WHERE id='X'").fetchone()[0] == 1


def test_nested_transaction_is_rejected(project: Project):
    with project.transaction():
        with pytest.raises(RuntimeError):
            with project.transaction():
                pass


def test_failed_migration_leaves_database_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def broken(conn):
        conn.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(schema, "MIGRATIONS", [schema.MIGRATIONS[0], broken])
    monkeypatch.setattr(schema, "CURRENT_VERSION", 2)  # v1 の後に失敗する移行
    conn = sqlite3.connect(tmp_path / "m.db", isolation_level=None)
    with pytest.raises(RuntimeError):
        schema.migrate(conn)
    assert schema.get_version(conn) == 0
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert tables == []
    conn.close()


def test_newer_schema_is_refused_and_lock_released(tmp_path: Path):
    Project.create(tmp_path, "案件A").close()
    root = tmp_path / "案件A"
    conn = sqlite3.connect(root / "project.db")
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(SchemaTooNewError):
        Project.open(root)
    with pytest.raises(SchemaTooNewError):  # ロックが解放されていれば同じ理由で失敗する
        Project.open(root)


def test_old_schema_is_migrated_with_backup(tmp_path: Path):
    """v1 のプロジェクトを開くと、バックアップを取ってから最新版へ移行する。"""
    root = tmp_path / "旧版"
    root.mkdir()
    conn = sqlite3.connect(root / "project.db", isolation_level=None)
    conn.execute("BEGIN")
    schema.MIGRATIONS[0](conn)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO project_info VALUES (1, 'u-1', '旧版', NULL, '2026-01-01', '0.0.0')"
    )
    conn.execute("COMMIT")
    conn.close()
    with Project.open(root) as p:
        assert p.schema_version == schema.CURRENT_VERSION
        assert p.uuid == "u-1"
        assert len(list(p.backup_dir.glob("project-*-migrate.db"))) == 1
        cols = [r[1] for r in p.conn.execute("PRAGMA table_info(column_rule)")]
        assert "binding" in cols


def test_corrupted_database_is_reported(tmp_path: Path):
    Project.create(tmp_path, "案件A").close()
    root = tmp_path / "案件A"
    for leftover in root.glob("project.db-*"):
        leftover.unlink()
    (root / "project.db").write_bytes(b"this is not a database" * 100)
    with pytest.raises(InvalidProjectError):
        Project.open(root)
    with pytest.raises(InvalidProjectError):
        Project.open(root)


def test_temp_leftovers_removed_on_open(tmp_path: Path):
    with Project.create(tmp_path, "案件A") as p:
        (p.temp_dir / "leftover.tmp").write_text("x")
    with Project.open(tmp_path / "案件A") as p:
        assert list(p.temp_dir.iterdir()) == []


def test_backup_generations(project: Project):
    for _ in range(4):
        project.backup(keep=3)
    backups = sorted(project.backup_dir.glob("project-*.db"))
    assert [b.name.split("-")[1] for b in backups] == ["000002", "000003", "000004"]
    conn = sqlite3.connect(backups[-1])
    assert conn.execute("SELECT project_uuid FROM project_info").fetchone()[0] == project.uuid
    conn.close()


def _add_entity(conn, entity_id: str, seq: int) -> None:
    conn.execute(
        "INSERT INTO entity (id, entity_type_id, name_seq, masked_name, status) "
        "VALUES (?, 'CUSTOMER', ?, ?, 'confirmed')",
        (entity_id, seq, f"顧客{seq:04d}"),
    )


def test_master_constraints(project: Project):
    conn = project.conn
    conn.execute(
        "INSERT INTO code_scheme (id, entity_type_id, display_name) VALUES (1, 'CUSTOMER', '得意先CD')"
    )
    _add_entity(conn, "e1", 1)
    _add_entity(conn, "e2", 2)

    # 同じ番号は同じ種類で使えない
    with pytest.raises(sqlite3.IntegrityError):
        _add_entity(conn, "e3", 1)

    # 同じ名称を別の対象に持たせてよい（同名の別顧客）
    for eid in ("e1", "e2"):
        conn.execute(
            "INSERT INTO entity_name (entity_id, original_name, normalized_name, kind, status) "
            "VALUES (?, '同名商事', '同名商事', 'primary', 'confirmed')",
            (eid,),
        )

    insert_code = (
        "INSERT INTO entity_code (entity_id, code_scheme_id, original_code, masked_code, code_seq, status) "
        "VALUES (?, 1, ?, ?, ?, ?)"
    )
    conn.execute(insert_code, ("e1", "000101", "900001", 1, "confirmed"))
    # 有効な同じ元コードは1対象のみ
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert_code, ("e2", "000101", "900002", 2, "draft"))
    # 取消済みの行は一意性の対象外。ただし変換後コードと番号は再利用できない
    conn.execute(insert_code, ("e2", "000202", "900002", 2, "void"))
    conn.execute(insert_code, ("e2", "000202", "900003", 3, "draft"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert_code, ("e2", "000303", "900002", 4, "draft"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert_code, ("e2", "000303", "900004", 2, "draft"))


def test_foreign_keys_enforced(project: Project):
    with pytest.raises(sqlite3.IntegrityError):
        project.conn.execute(
            "INSERT INTO entity (id, entity_type_id, name_seq, masked_name, status) "
            "VALUES ('e9', 'NO_SUCH_TYPE', 1, 'x', 'draft')"
        )
