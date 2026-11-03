"""変換作業の手順と状態遷移（設計書 07 章 3〜5）。

状態: draft → planned → staged → verified → committed → published（取消は cancelled）

トランザクション境界:
  T1 計画   plan()    新しい対象の仮登録と番号の確保、要確認項目の作成
  T2 確定   commit()  仮登録の確定、変更履歴、出力ファイルのハッシュ記録
  T3 公開   publish() 外部フォルダへ一時名で書出し → 改名 → ハッシュ確認
  T4 取消   cancel()  仮登録の取消（番号は欠番）

外部フォルダへの公開は必ず対応表の確定の後に行う。公開に失敗しても確定済みの対応は正しく、
作業領域のファイルから再公開できる。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from maskapp.config import Config, RelationSpec, TemplateSpec, load_config, match_template, save_config
from maskapp.config.model import ConfigError
from maskapp.entity.master import Master
from maskapp.formats.csvfile import CsvDocument
from maskapp.infra.safelog import get_logger
from maskapp.ingest import ImportedFile, import_csv, load_document
from maskapp.project import Project
from maskapp.transform import reviews
from maskapp.transform.planner import Plan, build_plan
from maskapp.transform.reviews import ALLOWED, Decision, ReviewItem, ReviewKind
from maskapp.verify.checks import VerifyReport, verify

_log = get_logger("workbench")
_PARTIAL_SUFFIX = ".partial"
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class RunState(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    STAGED = "staged"
    VERIFIED = "verified"
    COMMITTED = "committed"
    PUBLISHED = "published"
    CANCELLED = "cancelled"
    FAILED = "failed"


ACTIVE_STATES = (RunState.DRAFT, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED, RunState.COMMITTED)


class RunStateError(RuntimeError):
    """その状態ではできない操作。"""


class OutputLocationError(ValueError):
    """出力先が不適切（プロジェクト内、原本と同じ場所、既存ファイルの上書き等）。"""


@dataclass(frozen=True)
class PlanSummary:
    run_id: int
    changed_cells: int
    unresolved_cells: int
    open_reviews: int
    new_entities: int


@dataclass(frozen=True)
class PreviewRow:
    record: int
    column: int
    header: str
    before: str
    after: str | None  # None は未解決


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Workbench:
    """1つのプロジェクトに対する変換作業の操作。開いた時点で途中の作業を復旧する。"""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.recover()

    @property
    def conn(self):
        return self.project.conn

    # ---------------------------------------------------------------- 取込・設定

    def import_csv(self, path: Path, encoding: str | None = None, delimiter: str | None = None) -> ImportedFile:
        return import_csv(self.project, path, encoding=encoding, delimiter=delimiter)

    def save_config(self, templates: list[TemplateSpec], relations: list[RelationSpec] = ()) -> int:
        with self.project.transaction() as conn:
            return save_config(conn, templates, relations)

    def config(self, version_id: int | None = None) -> Config:
        return load_config(self.conn, version_id)

    # ---------------------------------------------------------------- 作業

    def start_run(self) -> int:
        active = self.conn.execute(
            f"SELECT id FROM run WHERE kind = 'convert' AND state IN ({','.join('?' * len(ACTIVE_STATES))})",
            [s.value for s in ACTIVE_STATES],
        ).fetchone()
        if active:
            raise RunStateError("another conversion is in progress; finish or cancel it first")
        with self.project.transaction() as conn:
            now = _now()
            run_id = conn.execute(
                "INSERT INTO run (kind, state, master_rev_base, created_at, updated_at) "
                "VALUES ('convert', 'draft', ?, ?, ?)",
                (self.project.master_revision(), now, now),
            ).lastrowid
        _log.event("run_started", run_id=run_id)
        return run_id

    def state(self, run_id: int) -> RunState:
        row = self.conn.execute("SELECT state FROM run WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run {run_id} not found")
        return RunState(row[0])

    def _set_state(self, conn, run_id: int, state: RunState) -> None:
        conn.execute("UPDATE run SET state = ?, updated_at = ? WHERE id = ?", (state.value, _now(), run_id))

    def _require(self, run_id: int, *states: RunState) -> RunState:
        current = self.state(run_id)
        if current not in states:
            raise RunStateError(f"run is {current.value}; expected one of {[s.value for s in states]}")
        return current

    def add_input(self, run_id: int, file_id: int, template_name: str | None = None) -> str:
        """作業にファイルを加える。表の定義は見出しで照合する（ファイル名は使わない）。"""
        self._require(run_id, RunState.DRAFT, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        config = self.config()
        doc = load_document(self.project, file_id)
        values = doc.values()
        if template_name is None:
            template_name = match_template(config, values)
            if template_name is None:
                raise ConfigError("no table definition matches this file's headers")
        else:
            template = config.template(template_name)
            if template.header_row >= len(values) or match_template(
                Config(None, {template_name: template}), values
            ) is None:
                raise ConfigError("file headers do not match the table definition")
        sha = self.conn.execute("SELECT sha256 FROM source_file WHERE id = ?", (file_id,)).fetchone()[0]
        with self.project.transaction() as conn:
            position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM run_input WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO run_input (run_id, source_file_id, sha256, template_name, position) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, file_id, sha, template_name, position),
            )
            self._back_to_draft(conn, run_id)
        return template_name

    def _inputs(self, run_id: int) -> list[tuple[int, int, str]]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT source_file_id, position, template_name FROM run_input WHERE run_id = ? ORDER BY position",
            (run_id,),
        )]

    def _docs(self, run_id: int) -> dict[int, CsvDocument]:
        return {fid: load_document(self.project, fid) for fid, _, _ in self._inputs(run_id)}

    def _back_to_draft(self, conn, run_id: int) -> None:
        state = RunState(conn.execute("SELECT state FROM run WHERE id = ?", (run_id,)).fetchone()[0])
        if state in (RunState.PLANNED, RunState.STAGED, RunState.VERIFIED):
            self._discard_staging(conn, run_id)
            self._set_state(conn, run_id, RunState.DRAFT)

    def _build(self, conn, run_id: int) -> Plan:
        config = load_config(conn)
        if config.version_id is None:
            raise ConfigError("no configuration has been saved")
        conn.execute("UPDATE run SET config_version_id = ? WHERE id = ?", (config.version_id, run_id))
        return build_plan(conn, run_id, config, self._inputs(run_id), self._docs(run_id))

    # ---------------------------------------------------------------- T1 計画

    def plan(self, run_id: int) -> PlanSummary:
        self._require(run_id, RunState.DRAFT, RunState.PLANNED)
        if not self._inputs(run_id):
            raise RunStateError("no input files in this run")
        with self.project.transaction() as conn:
            plan = self._build(conn, run_id)
            self._set_state(conn, run_id, RunState.PLANNED)
        summary = PlanSummary(
            run_id,
            changed_cells=sum(len(f.changes) for f in plan.files),
            unresolved_cells=plan.unresolved,
            open_reviews=len(self.reviews(run_id)),
            new_entities=self.conn.execute(
                "SELECT COUNT(*) FROM entity WHERE created_run_id = ? AND status = 'draft'", (run_id,)
            ).fetchone()[0],
        )
        _log.event("run_planned", run_id=run_id, changed=summary.changed_cells,
                   unresolved=summary.unresolved_cells, reviews=summary.open_reviews)
        return summary

    def preview(self, run_id: int, file_id: int, only_changed: bool = True) -> list[PreviewRow]:
        """変換前後の比較。計画（確保済みの番号）をそのまま使う。"""
        self._require(run_id, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED, RunState.COMMITTED)
        with self.project.transaction() as conn:
            plan = self._build(conn, run_id)
        fp = next(f for f in plan.files if f.file_id == file_id)
        rows = []
        for r in range(len(fp.doc.records)):
            for c, f in enumerate(fp.doc.records[r].fields):
                a = fp.changes.get((r, c))
                if a is None and only_changed:
                    continue
                header = fp.template.columns[c].header if c < len(fp.template.columns) else ""
                rows.append(PreviewRow(r, c, header, f.value, f.value if a is None else a.value))
        return rows

    # ---------------------------------------------------------------- 要確認

    def reviews(self, run_id: int, state: str | None = "open") -> list[ReviewItem]:
        return reviews.list_items(self.conn, run_id, state)

    def decide(self, item_id: int, decision: Decision, entity_id: str | None = None) -> None:
        item = reviews.get(self.conn, item_id)
        run_id = self.conn.execute("SELECT run_id FROM review_item WHERE id = ?", (item_id,)).fetchone()[0]
        self._require(run_id, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        if decision not in ALLOWED[item.kind]:
            raise ValueError(f"decision {decision.value} is not allowed for {item.kind.value}")
        with self.project.transaction() as conn:
            self._apply_decision(conn, run_id, item, decision, entity_id)
            self._back_to_planned(conn, run_id)

    def decide_all(self, run_id: int, kind: ReviewKind, decision: Decision) -> int:
        """同じ種類の未確認項目を一括で判断する（例：新しい対象の一括確定）。"""
        items = [i for i in self.reviews(run_id) if i.kind is kind]
        if decision not in ALLOWED[kind]:
            raise ValueError(f"decision {decision.value} is not allowed for {kind.value}")
        if decision is Decision.LINK_EXISTING:
            raise ValueError("linking to an existing entity must be decided one by one")
        self._require(run_id, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        with self.project.transaction() as conn:
            for item in items:
                self._apply_decision(conn, run_id, item, decision, None)
            self._back_to_planned(conn, run_id)
        return len(items)

    def _apply_decision(self, conn, run_id: int, item: ReviewItem, decision: Decision, entity_id: str | None):
        master = Master(conn)
        subject = item.subject
        record: dict = {"decision": decision.value}
        if decision is Decision.LINK_EXISTING:
            if not entity_id:
                raise ValueError("entity_id is required to link to an existing entity")
            target_type = conn.execute("SELECT entity_type_id FROM entity WHERE id = ?", (entity_id,)).fetchone()
            if target_type is None or target_type[0] != subject["entity_type"]:
                raise ValueError("target entity does not exist or has a different type")
            record["entity_id"] = entity_id
        if item.kind is ReviewKind.NEW_ENTITY and decision is Decision.LINK_EXISTING:
            master.move_to(subject["entity_id"], entity_id)
        elif item.kind is ReviewKind.CODE_MULTI_NAME and decision is Decision.ADD_ALIAS:
            master.add_name(subject["entity_id"], subject["name"], "alias", run_id)
        elif item.kind is ReviewKind.SIMILAR_NAME:
            if decision is Decision.LINK_EXISTING:
                master.add_name(entity_id, subject["name"], "alias", run_id)
            else:
                new_id = master.create_entity(subject["entity_type"], run_id)
                master.add_name(new_id, subject["name"], "primary", run_id)
                record["entity_id"] = new_id
        elif item.kind is ReviewKind.NAME_AMBIGUOUS and decision is Decision.REGISTER_NEW:
            new_id = master.create_entity(subject["entity_type"], run_id)
            master.add_name(new_id, subject["name"], "primary", run_id)
            record["entity_id"] = new_id
        reviews.set_decision(conn, item.id, record)

    def _back_to_planned(self, conn, run_id: int) -> None:
        state = RunState(conn.execute("SELECT state FROM run WHERE id = ?", (run_id,)).fetchone()[0])
        if state in (RunState.STAGED, RunState.VERIFIED):
            self._discard_staging(conn, run_id)
            self._set_state(conn, run_id, RunState.PLANNED)

    # ---------------------------------------------------------------- 書出し・検証

    def _staging_dir(self, run_id: int) -> Path:
        return self.project.work_dir / "runs" / str(run_id)

    def _discard_staging(self, conn, run_id: int) -> None:
        conn.execute("DELETE FROM run_output WHERE run_id = ?", (run_id,))
        shutil.rmtree(self._staging_dir(run_id), ignore_errors=True)

    def _default_names(self, plan: Plan) -> dict[int, str]:
        counts: dict[str, int] = {}
        names = {}
        for fp in plan.files:
            counts[fp.template.name] = counts.get(fp.template.name, 0) + 1
            names[fp.file_id] = f"{fp.template.name}_{counts[fp.template.name]:02d}.csv"
        return names

    def stage(self, run_id: int, output_names: dict[int, str] | None = None) -> list[Path]:
        """作業領域に出力ファイルを書き出す。"""
        self._require(run_id, RunState.PLANNED)
        with self.project.transaction() as conn:
            plan = self._build(conn, run_id)
        if plan.unresolved:
            raise RunStateError("some cells are unresolved; decide the open review items first")
        names = {**self._default_names(plan), **(output_names or {})}
        for name in names.values():
            if _INVALID_FILENAME.search(name) or name.strip() != name or not name:
                raise OutputLocationError("invalid output file name")
        if len(set(n.casefold() for n in names.values())) != len(names):
            raise OutputLocationError("output file names must be unique")
        staging = self._staging_dir(run_id)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        rows = []
        for fp in plan.files:
            data = fp.doc.serialize(fp.text_changes())
            path = staging / names[fp.file_id]
            path.write_bytes(data)
            rows.append((run_id, fp.file_id, names[fp.file_id], _sha256(data), len(data),
                         str(path.relative_to(self.project.root)), fp.position))
        with self.project.transaction() as conn:
            conn.execute("DELETE FROM run_output WHERE run_id = ?", (run_id,))
            conn.executemany(
                "INSERT INTO run_output (run_id, source_file_id, output_filename, sha256, size, staged_path, "
                "position) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._set_state(conn, run_id, RunState.STAGED)
        _log.event("run_staged", run_id=run_id, files=len(rows))
        return [self.project.root / r[5] for r in rows]

    def _staged(self, run_id: int) -> dict[int, tuple[str, bytes]]:
        out = {}
        for file_id, name, staged_path in self.conn.execute(
            "SELECT source_file_id, output_filename, staged_path FROM run_output WHERE run_id = ?", (run_id,)
        ):
            out[file_id] = (name, (self.project.root / staged_path).read_bytes())
        return out

    def verify(self, run_id: int) -> VerifyReport:
        """書き出したファイルを読み直して検証する。問題がなければ verified、あれば planned に戻す。"""
        self._require(run_id, RunState.STAGED)
        staged = self._staged(run_id)
        with self.project.transaction() as conn:
            plan = self._build(conn, run_id)
            report = verify(conn, plan, staged)
            if report.ok:
                self._set_state(conn, run_id, RunState.VERIFIED)
            else:
                self._set_state(conn, run_id, RunState.PLANNED)
        if not report.ok:
            shutil.rmtree(self._staging_dir(run_id), ignore_errors=True)
            with self.project.transaction() as conn:
                conn.execute("DELETE FROM run_output WHERE run_id = ?", (run_id,))
        _log.event("run_verified", run_id=run_id, ok=report.ok, blocking=len(report.blocking))
        return report

    # ---------------------------------------------------------------- T2 確定

    def commit(self, run_id: int) -> int:
        """対応表を確定する。確定後の対応表版を返す。"""
        self._require(run_id, RunState.VERIFIED)
        for file_id, (_, data) in self._staged(run_id).items():
            expected = self.conn.execute(
                "SELECT sha256 FROM run_output WHERE run_id = ? AND source_file_id = ?", (run_id, file_id)
            ).fetchone()[0]
            if _sha256(data) != expected:
                raise RunStateError("staged output changed after verification; stage and verify again")
        self.project.backup(reason="commit")
        with self.project.transaction() as conn:
            Master(conn).confirm_run(run_id)
            revision = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM master_event").fetchone()[0]
            conn.execute("UPDATE run SET master_rev_committed = ? WHERE id = ?", (revision, run_id))
            self._set_state(conn, run_id, RunState.COMMITTED)
        _log.event("run_committed", run_id=run_id, master_revision=revision)
        return revision

    # ---------------------------------------------------------------- T3 公開

    def _check_export_dir(self, export_dir: Path) -> Path:
        target = Path(export_dir).resolve()
        root = self.project.root.resolve()
        if target == root or root in target.parents:
            raise OutputLocationError("export folder must be outside the project folder")
        for (original_dir,) in self.conn.execute("SELECT DISTINCT original_dir FROM source_file"):
            if original_dir and Path(original_dir).resolve() == target:
                raise OutputLocationError("export folder must differ from the folder of the original files")
        return target

    def publish(self, run_id: int, export_dir: Path) -> list[Path]:
        """確定済みの出力を外部フォルダへ公開する。既存ファイルは上書きしない。"""
        self._require(run_id, RunState.COMMITTED)
        target_dir = self._check_export_dir(export_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        outputs = self.conn.execute(
            "SELECT id, output_filename, staged_path, sha256, published_at FROM run_output WHERE run_id = ? "
            "ORDER BY position",
            (run_id,),
        ).fetchall()
        pending = [o for o in outputs if o[4] is None]
        for _, name, _, _, _ in pending:
            if (target_dir / name).exists():
                raise OutputLocationError("a file with the same name already exists in the export folder")
        with self.project.transaction() as conn:
            for out_id, name, _, _, _ in pending:
                conn.execute("UPDATE run_output SET output_path = ? WHERE id = ?", (str(target_dir / name), out_id))
        published = []
        for out_id, name, staged_path, digest, _ in pending:
            data = (self.project.root / staged_path).read_bytes()
            if _sha256(data) != digest:
                raise RunStateError("staged output does not match the committed record; restage first")
            final = target_dir / name
            partial = final.with_name(final.name + _PARTIAL_SUFFIX)
            with open(partial, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if final.exists():
                partial.unlink()
                raise OutputLocationError("a file with the same name appeared in the export folder")
            os.replace(partial, final)
            if _sha256(final.read_bytes()) != digest:
                raise RunStateError("published file does not match the staged output")
            with self.project.transaction() as conn:
                conn.execute("UPDATE run_output SET published_at = ? WHERE id = ?", (_now(), out_id))
            published.append(final)
        with self.project.transaction() as conn:
            self._set_state(conn, run_id, RunState.PUBLISHED)
        shutil.rmtree(self._staging_dir(run_id), ignore_errors=True)
        _log.event("run_published", run_id=run_id, files=len(published))
        return [target_dir / o[1] for o in outputs]

    def restage_committed(self, run_id: int) -> None:
        """確定済みで未公開の作業の出力を、確定した対応から作り直す（作業領域を失った場合の復旧）。

        作り直したファイルが確定時に記録したハッシュと一致しなければ停止する。
        """
        self._require(run_id, RunState.COMMITTED)
        with self.project.transaction() as conn:
            plan = self._build(conn, run_id)
        expected = {
            fid: (name, digest, staged_path)
            for fid, name, digest, staged_path in self.conn.execute(
                "SELECT source_file_id, output_filename, sha256, staged_path FROM run_output WHERE run_id = ?",
                (run_id,),
            )
        }
        staging = self._staging_dir(run_id)
        staging.mkdir(parents=True, exist_ok=True)
        for fp in plan.files:
            name, digest, staged_path = expected[fp.file_id]
            data = fp.doc.serialize(fp.text_changes())
            if _sha256(data) != digest:
                raise RunStateError("regenerated output differs from the committed output")
            (self.project.root / staged_path).write_bytes(data)

    # ---------------------------------------------------------------- T4 取消

    def cancel(self, run_id: int) -> int:
        """作業を取り消す。仮登録は取消され、確保した番号は欠番になる。取り消した対象数を返す。"""
        self._require(run_id, RunState.DRAFT, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        with self.project.transaction() as conn:
            count = Master(conn).void_run(run_id)
            self._discard_staging(conn, run_id)
            self._set_state(conn, run_id, RunState.CANCELLED)
        _log.event("run_cancelled", run_id=run_id, voided=count)
        return count

    # ---------------------------------------------------------------- 復旧

    def recover(self) -> None:
        """途中で終了した作業を整える（設計書 07 章 4）。"""
        rows = self.conn.execute(
            "SELECT id, state FROM run WHERE state IN ('staged', 'verified', 'committed')"
        ).fetchall()
        for run_id, state in rows:
            if state in ("staged", "verified"):
                with self.project.transaction() as conn:
                    self._discard_staging(conn, run_id)
                    self._set_state(conn, run_id, RunState.PLANNED)
                _log.event("run_recovered_to_planned", run_id=run_id)
                continue
            # committed: 公開の途中で終わった可能性
            for out_id, output_path, digest in self.conn.execute(
                "SELECT id, output_path, sha256 FROM run_output WHERE run_id = ? AND published_at IS NULL "
                "AND output_path IS NOT NULL",
                (run_id,),
            ).fetchall():
                final = Path(output_path)
                partial = final.with_name(final.name + _PARTIAL_SUFFIX)
                if partial.exists():
                    partial.unlink()
                    _log.event("partial_output_removed", run_id=run_id)
                if final.exists() and _sha256(final.read_bytes()) == digest:
                    with self.project.transaction() as conn:
                        conn.execute("UPDATE run_output SET published_at = ? WHERE id = ?", (_now(), out_id))
            remaining = self.conn.execute(
                "SELECT COUNT(*) FROM run_output WHERE run_id = ? AND published_at IS NULL", (run_id,)
            ).fetchone()[0]
            if remaining == 0:
                with self.project.transaction() as conn:
                    self._set_state(conn, run_id, RunState.PUBLISHED)
            else:
                staged_ok = all(
                    (self.project.root / p).exists() and _sha256((self.project.root / p).read_bytes()) == d
                    for p, d in self.conn.execute(
                        "SELECT staged_path, sha256 FROM run_output WHERE run_id = ? AND published_at IS NULL",
                        (run_id,),
                    )
                )
                if not staged_ok:
                    self.restage_committed(run_id)

    # ---------------------------------------------------------------- 参照

    def findings(self, run_id: int) -> list[dict]:
        return [
            {"origin": o, "severity": s, "kind": k, "location": json.loads(loc), "detail": json.loads(d)}
            for o, s, k, loc, d in self.conn.execute(
                "SELECT origin, severity, kind, location, detail FROM finding WHERE run_id = ? ORDER BY id",
                (run_id,),
            )
        ]
