"""プロジェクトの作成・開閉・トランザクション・バックアップ（設計書 07 章 6）。

プロジェクトフォルダの構成::

    <プロジェクト名>/
      project.db     対応表・設定・履歴（元の値を含む）
      project.lock   同時使用防止
      originals/     取込時に複製した原本
      work/          作業領域（work/tmp は開くたびに空にする）
      reports/       元の値を含む検証レポート
      backups/       project.db の世代バックアップ
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from maskapp import __version__
from maskapp.infra.safelog import get_logger
from maskapp.project import schema
from maskapp.project.lock import ProjectLock

DB_NAME = "project.db"
LOCK_NAME = "project.lock"
SUBDIRS = ("originals", "work", "reports", "backups")
DEFAULT_BACKUP_GENERATIONS = 10
DEFAULT_LOCK_TIMEOUT = 2.0  # 秒。異常終了直後のロック解放待ち

_log = get_logger("project")

# Windows のファイル名に使えない文字・名前
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ProjectExistsError(FileExistsError):
    """同名のプロジェクトフォルダが既にある。"""


class InvalidProjectError(ValueError):
    """プロジェクトフォルダとして不正、またはデータベースが壊れている。"""


class SchemaTooNewError(RuntimeError):
    """新しい版のアプリで作られたプロジェクト。"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _backup_seq(path: Path) -> int:
    parts = path.stem.split("-")
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


def validate_project_name(name: str) -> str:
    stripped = name.strip()
    if not stripped:
        raise ValueError("project name is empty")
    if len(stripped) > 80:
        raise ValueError("project name is too long")
    if _INVALID_CHARS.search(stripped):
        raise ValueError("project name contains characters not allowed in folder names")
    if stripped.endswith("."):
        raise ValueError("project name must not end with a dot")
    if stripped.split(".")[0].upper() in _RESERVED_NAMES:
        raise ValueError("project name is reserved by Windows")
    return stripped


def _connect(db_path: Path) -> sqlite3.Connection:
    # isolation_level=None: トランザクションは Project.transaction() で明示的に管理する
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class Project:
    """開いているプロジェクト。with 文で使うか、最後に close() を呼ぶ。"""

    def __init__(self, root: Path, conn: sqlite3.Connection, lock: ProjectLock) -> None:
        self.root = root
        self._conn = conn
        self._lock = lock
        self._in_transaction = False

    # ---- 作成・開閉 ----

    @classmethod
    def create(cls, parent: Path, name: str, color: str | None = None) -> Project:
        name = validate_project_name(name)
        root = Path(parent) / name
        if root.exists():
            raise ProjectExistsError("a folder with this project name already exists")
        root.mkdir(parents=True)
        for sub in SUBDIRS:
            (root / sub).mkdir()
        lock = ProjectLock(root / LOCK_NAME)
        lock.acquire()
        try:
            conn = _connect(root / DB_NAME)
            schema.migrate(conn)
            conn.execute(
                "INSERT INTO project_info (id, project_uuid, name, color, created_at, "
                "created_by_app_version) VALUES (1, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), name, color, _now(), __version__),
            )
        except BaseException:
            lock.release()
            raise
        _log.event("project_created", schema_version=schema.CURRENT_VERSION)
        project = cls(root, conn, lock)
        project._prepare_work_dir()
        return project

    @classmethod
    def open(cls, root: Path, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> Project:
        root = Path(root)
        db_path = root / DB_NAME
        if not db_path.is_file():
            raise InvalidProjectError("project database not found")
        lock = ProjectLock(root / LOCK_NAME)
        lock.acquire(timeout=lock_timeout)
        conn = None
        try:
            conn = _connect(db_path)
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise InvalidProjectError("project database failed integrity check")
            version = schema.get_version(conn)
            if version > schema.CURRENT_VERSION:
                raise SchemaTooNewError("project was created by a newer version of the app")
            project = cls(root, conn, lock)
            if version < schema.CURRENT_VERSION:
                project.backup(reason="migrate")
                schema.migrate(conn)
                _log.event("schema_migrated", before=version, after=schema.CURRENT_VERSION)
            if conn.execute("SELECT COUNT(*) FROM project_info").fetchone()[0] != 1:
                raise InvalidProjectError("project information is missing")
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                conn.close()
            lock.release()
            raise InvalidProjectError("project database could not be read") from exc
        except BaseException:
            if conn is not None:
                conn.close()
            lock.release()
            raise
        for sub in SUBDIRS:
            (root / sub).mkdir(exist_ok=True)
        project._prepare_work_dir()
        _log.event("project_opened", schema_version=schema.CURRENT_VERSION)
        return project

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
        finally:
            self._conn = None
            self._lock.release()
        _log.event("project_closed")

    def __enter__(self) -> Project:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- 情報 ----

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("project is closed")
        return self._conn

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    @property
    def uuid(self) -> str:
        return self.conn.execute("SELECT project_uuid FROM project_info").fetchone()[0]

    @property
    def name(self) -> str:
        return self.conn.execute("SELECT name FROM project_info").fetchone()[0]

    @property
    def schema_version(self) -> int:
        return schema.get_version(self.conn)

    def master_revision(self) -> int:
        """対応表版＝対応表の変更履歴の最終通し番号（未変更なら 0）。"""
        row = self.conn.execute("SELECT MAX(seq) FROM master_event").fetchone()
        return row[0] or 0

    # ---- トランザクション ----

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """書込トランザクション。例外時はすべて取り消す。入れ子は不可。"""
        if self._in_transaction:
            raise RuntimeError("nested transactions are not supported")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        finally:
            self._in_transaction = False

    # ---- 作業領域 ----

    @property
    def work_dir(self) -> Path:
        return self.root / "work"

    @property
    def temp_dir(self) -> Path:
        """一時ファイル用。%TEMP% は使わない（設計書 07 章 6.1）。"""
        return self.work_dir / "tmp"

    def _prepare_work_dir(self) -> None:
        tmp = self.temp_dir
        if tmp.exists():
            removed = sum(1 for _ in tmp.iterdir())
            shutil.rmtree(tmp)
            if removed:
                _log.event("temp_leftovers_removed", count=removed)
        tmp.mkdir(parents=True)

    # ---- バックアップ ----

    @property
    def backup_dir(self) -> Path:
        return self.root / "backups"

    def backup(self, reason: str = "manual", keep: int = DEFAULT_BACKUP_GENERATIONS) -> Path:
        """SQLite のオンラインバックアップで世代バックアップを作り、古いものを削除する。"""
        if not re.fullmatch(r"[a-z_]{1,20}", reason):
            raise ValueError("reason must be a short lowercase identifier")
        if self._in_transaction:
            raise RuntimeError("cannot back up during a transaction")
        self.backup_dir.mkdir(exist_ok=True)
        # 時刻だけでは同名になり得る（Windows の時計分解能）ため通し番号を付ける
        existing = sorted(self.backup_dir.glob("project-*.db"))
        last_seq = max((_backup_seq(p) for p in existing), default=0)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.backup_dir / f"project-{last_seq + 1:06d}-{stamp}-{reason}.db"
        dest = sqlite3.connect(target)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        backups = sorted(self.backup_dir.glob("project-*.db"), key=_backup_seq)
        for old in backups[: max(0, len(backups) - keep)]:
            old.unlink()
        _log.event("backup_created", generations=min(len(backups), keep))
        return target
