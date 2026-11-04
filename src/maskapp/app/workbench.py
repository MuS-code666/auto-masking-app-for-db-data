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
from maskapp.entity.normalize import normalize_for_search
from maskapp.formats.csvfile import CsvDocument
from maskapp.infra.safelog import get_logger
from maskapp.ingest import ImportedFile, Source, import_csv, import_file, load_source
from maskapp.project import Project
from maskapp.transform import reviews
from maskapp.transform.planner import Plan, RunFile, RunUnit, build_plan, grid_of, render_file
from maskapp.transform.reviews import ALLOWED, Decision, ReviewItem, ReviewKind
from maskapp.transform.xlsx_plan import BookOptions
from maskapp.verify.checks import VerifyReport, verify
from maskapp.verify.residual import is_short

_log = get_logger("workbench")
_PARTIAL_SUFFIX = ".partial"
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
VALUE_TYPES = ("PHONE", "EMAIL", "POSTAL", "URL", "ADDRESS")  # 文章内の候補を値として登録するときの種類


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
    blocking_notes: list[dict]  # 取込ファイル由来の停止条件（未対応の機能、コメント、数式の部分一致等）


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

    def import_file(self, path: Path, encoding: str | None = None, delimiter: str | None = None) -> ImportedFile:
        """拡張子で CSV・xlsx を判別して取り込む。"""
        return import_file(self.project, path, encoding=encoding, delimiter=delimiter)

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

    def active_run(self) -> int | None:
        """進行中の変換作業（同時に1つ）。"""
        row = self.conn.execute(
            f"SELECT id FROM run WHERE kind = 'convert' AND state IN ({','.join('?' * len(ACTIVE_STATES))}) "
            "ORDER BY id DESC LIMIT 1",
            [s.value for s in ACTIVE_STATES],
        ).fetchone()
        return row[0] if row else None

    def current_step(self) -> int:
        """画面の手順（2 取込・3 設定・4 確認・5 検証と出力）。作業の状態から決め、再開時に使う。"""
        run_id = self.active_run()
        if run_id is None:
            return 2
        state = self.state(run_id)
        if state is RunState.DRAFT:
            return 3 if self._inputs(run_id)[0] else 2
        if state is RunState.PLANNED:
            return 4
        return 5

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

    def add_input(self, run_id: int, file_id: int, template_name: str | None = None,
                  sheet: int | str | None = None) -> dict[int, str]:
        """作業にファイルを加え、表の定義を適用するシートを決める。{シート番号: 表の定義名} を返す。

        表の定義は見出しで照合する（ファイル名は使わない）。xlsx でシートを指定しない場合は、
        見出しが一致するすべてのシートに適用する。どのシートにも適用しないシートも出力に含まれ、
        残存検査の対象になる。同じファイルに対して繰り返し呼べば、シートを追加できる。
        """
        self._require(run_id, RunState.DRAFT, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        config = self.config()
        source = load_source(self.project, file_id)
        if isinstance(source, CsvDocument):
            if sheet not in (None, 0):
                raise ConfigError("a CSV file has only one table")
            candidates = [0]
        elif sheet is None:
            candidates = [s.idx for s in source.sheets]
        else:
            candidates = [self._sheet_index(source, sheet)]
        scope = config if template_name is None else Config(None, {template_name: config.template(template_name)})
        assigned: dict[int, str] = {}
        for idx in candidates:
            matched = match_template(scope, grid_of(source, idx).rows)
            if matched is not None:
                assigned[idx] = matched
        if not assigned:
            if template_name is not None:
                raise ConfigError("file headers do not match the table definition")
            raise ConfigError("no table definition matches this file's headers")
        sha = self.conn.execute("SELECT sha256 FROM source_file WHERE id = ?", (file_id,)).fetchone()[0]
        with self.project.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM run_input WHERE run_id = ? AND source_file_id = ?", (run_id, file_id)
            ).fetchone()
            if not exists:
                position = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM run_input WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO run_input (run_id, source_file_id, sha256, position) VALUES (?, ?, ?, ?)",
                    (run_id, file_id, sha, position),
                )
            conn.executemany(
                "INSERT OR REPLACE INTO run_sheet (run_id, source_file_id, sheet_idx, template_name) "
                "VALUES (?, ?, ?, ?)",
                [(run_id, file_id, idx, name) for idx, name in assigned.items()],
            )
            self._back_to_draft(conn, run_id)
        return assigned

    def add_file(self, run_id: int, file_id: int) -> None:
        """表の定義を適用せずにファイルを作業に加える（定義は画面で後から決める）。"""
        self._require(run_id, RunState.DRAFT, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        sha = self.conn.execute("SELECT sha256 FROM source_file WHERE id = ?", (file_id,)).fetchone()[0]
        with self.project.transaction() as conn:
            position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM run_input WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO run_input (run_id, source_file_id, sha256, position) VALUES (?, ?, ?, ?)",
                (run_id, file_id, sha, position),
            )
            self._back_to_draft(conn, run_id)

    def assign_sheet(self, run_id: int, file_id: int, sheet: int, template_name: str | None) -> None:
        """シートに適用する表の定義を決める（None で外す）。見出しとの一致は計画の作成時に確かめる。"""
        self._require(run_id, RunState.DRAFT, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        with self.project.transaction() as conn:
            conn.execute("DELETE FROM run_sheet WHERE run_id = ? AND source_file_id = ? AND sheet_idx = ?",
                         (run_id, file_id, sheet))
            if template_name is not None:
                conn.execute(
                    "INSERT INTO run_sheet (run_id, source_file_id, sheet_idx, template_name) VALUES (?, ?, ?, ?)",
                    (run_id, file_id, sheet, template_name),
                )
            self._back_to_draft(conn, run_id)

    def back_to_draft(self, run_id: int) -> None:
        """設定を見直すために作業を設定中に戻す（判断・確保した番号はそのまま）。"""
        with self.project.transaction() as conn:
            self._back_to_draft(conn, run_id)

    def remove_input(self, run_id: int, file_id: int, sheet: int | None = None) -> None:
        """作業からファイル（または xlsx の1シートの表の定義の適用）を外す。"""
        self._require(run_id, RunState.DRAFT, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        with self.project.transaction() as conn:
            if sheet is None:
                conn.execute("DELETE FROM run_sheet WHERE run_id = ? AND source_file_id = ?", (run_id, file_id))
                conn.execute("DELETE FROM run_input WHERE run_id = ? AND source_file_id = ?", (run_id, file_id))
            else:
                conn.execute("DELETE FROM run_sheet WHERE run_id = ? AND source_file_id = ? AND sheet_idx = ?",
                             (run_id, file_id, sheet))
            self._back_to_draft(conn, run_id)

    def run_files(self, run_id: int) -> list[RunFile]:
        return self._inputs(run_id)[0]

    def run_units(self, run_id: int) -> list[RunUnit]:
        return self._inputs(run_id)[1]

    def source(self, file_id: int) -> Source:
        return load_source(self.project, file_id)

    def file_info(self, file_id: int) -> dict:
        """取込済みファイルの情報（元のファイル名を含む。画面表示用で、ログには出さない）。"""
        name, fmt, info = self.conn.execute(
            "SELECT original_filename, format, format_info FROM source_file WHERE id = ?", (file_id,)
        ).fetchone()
        sheets = [tuple(r) for r in self.conn.execute(
            "SELECT idx, name, visibility FROM sheet WHERE source_file_id = ? ORDER BY idx", (file_id,))]
        return {"id": file_id, "name": name, "format": fmt, "info": json.loads(info), "sheets": sheets}

    def reset_inputs(self, run_id: int) -> None:
        """設定を変えた後に、作業に含めたファイルの表の定義を照合し直す（見出しが合わなくなったシートは外す）。"""
        files, units = self._inputs(run_id)
        config = self.config()
        with self.project.transaction() as conn:
            for unit in units:
                template = config.templates.get(unit.template_name)
                rows = grid_of(load_source(self.project, unit.file_id), unit.sheet).rows
                if template is None or match_template(Config(None, {template.name: template}), rows) is None:
                    conn.execute("DELETE FROM run_sheet WHERE run_id = ? AND source_file_id = ? AND sheet_idx = ?",
                                 (run_id, unit.file_id, unit.sheet))
            self._back_to_draft(conn, run_id)

    @staticmethod
    def _sheet_index(source, sheet: int | str) -> int:
        if isinstance(sheet, int):
            if not 0 <= sheet < len(source.sheets):
                raise ConfigError("sheet index out of range")
            return sheet
        for s in source.sheets:
            if s.name == sheet:
                return s.idx
        raise ConfigError("sheet not found")

    def set_options(self, run_id: int, file_id: int, options: BookOptions) -> None:
        """xlsx の扱い（コメントの除去、画像の確認、数式の値への変換等）を選ぶ。"""
        self._require(run_id, RunState.DRAFT, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        with self.project.transaction() as conn:
            updated = conn.execute(
                "UPDATE run_input SET options = ? WHERE run_id = ? AND source_file_id = ?",
                (json.dumps(options.to_json()), run_id, file_id),
            ).rowcount
            if not updated:
                raise KeyError("file is not part of this run")
            self._back_to_planned(conn, run_id)

    def _inputs(self, run_id: int) -> tuple[list[RunFile], list[RunUnit]]:
        files = [
            RunFile(fid, pos, json.loads(options))
            for fid, pos, options in self.conn.execute(
                "SELECT source_file_id, position, options FROM run_input WHERE run_id = ? ORDER BY position",
                (run_id,),
            )
        ]
        units = [
            RunUnit(fid, idx, name)
            for fid, idx, name in self.conn.execute(
                "SELECT source_file_id, sheet_idx, template_name FROM run_sheet WHERE run_id = ? "
                "ORDER BY source_file_id, sheet_idx",
                (run_id,),
            )
        ]
        return files, units

    def _sources(self, run_id: int) -> dict[int, Source]:
        files, _ = self._inputs(run_id)
        return {f.file_id: load_source(self.project, f.file_id) for f in files}

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
        files, units = self._inputs(run_id)
        sources = self._sources(run_id)
        for unit in units:
            template = config.templates.get(unit.template_name)
            if template is None or match_template(
                Config(None, {template.name: template}), grid_of(sources[unit.file_id], unit.sheet).rows
            ) is None:
                raise ConfigError("a sheet in this run does not match its table definition")
        return build_plan(conn, run_id, config, files, units, sources)

    # ---------------------------------------------------------------- T1 計画

    def plan(self, run_id: int) -> PlanSummary:
        self._require(run_id, RunState.DRAFT, RunState.PLANNED)
        if not self._inputs(run_id)[0]:
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
            blocking_notes=[{"kind": n.kind, **n.detail} for n in plan.notes if n.severity == "block"],
        )
        _log.event("run_planned", run_id=run_id, changed=summary.changed_cells,
                   unresolved=summary.unresolved_cells, reviews=summary.open_reviews)
        return summary

    def preview(self, run_id: int, file_id: int, only_changed: bool = True, sheet: int = 0) -> list[PreviewRow]:
        """変換前後の比較。計画（確保済みの番号）をそのまま使う。"""
        self._require(run_id, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED, RunState.COMMITTED)
        with self.project.transaction() as conn:
            plan = self._build(conn, run_id)
        fp = next(f for f in plan.files if f.file_id == file_id and f.sheet == sheet)
        rows = []
        for r, values in enumerate(fp.grid.rows):
            for c, value in enumerate(values):
                a = fp.changes.get((r, c))
                if a is None and only_changed:
                    continue
                header = fp.template.columns[c].header if c < len(fp.template.columns) else ""
                rows.append(PreviewRow(r, c, header, value, value if a is None else a.value))
        return rows

    # ---------------------------------------------------------------- 要確認

    def reviews(self, run_id: int, state: str | None = "open") -> list[ReviewItem]:
        return reviews.list_items(self.conn, run_id, state)

    def decide(self, item_id: int, decision: Decision, entity_id: str | None = None, *,
               target: str | None = None, entity_type: str | None = None, info_type: str | None = None,
               text: str | None = None) -> None:
        """要確認を判断する。

        文章内の語（短い語・置換先が複数）：REPLACE_TERM で target（候補の key）を選ぶ、または FALSE_POSITIVE。
        文章内の候補：REGISTER_NEW（entity_type）・LINK_EXISTING（entity_id）・REGISTER_VALUE（info_type）で
        登録して置換する、または FALSE_POSITIVE。text で登録する語句を候補の一部に絞れる。
        """
        item = reviews.get(self.conn, item_id)
        run_id = self.conn.execute("SELECT run_id FROM review_item WHERE id = ?", (item_id,)).fetchone()[0]
        self._require(run_id, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        if decision not in ALLOWED[item.kind]:
            raise ValueError(f"decision {decision.value} is not allowed for {item.kind.value}")
        with self.project.transaction() as conn:
            self._apply_decision(conn, run_id, item, decision, entity_id, target=target, entity_type=entity_type,
                                 info_type=info_type, text=text)
            self._back_to_planned(conn, run_id)

    def decide_all(self, run_id: int, kind: ReviewKind, decision: Decision) -> int:
        """同じ種類の未確認項目を一括で判断する（例：新しい対象の一括確定）。

        文章内の候補の REGISTER_VALUE は、電話番号・メール等として検出した候補だけを、検出した種類で登録する。
        """
        items = [i for i in self.reviews(run_id) if i.kind is kind]
        if kind is ReviewKind.FREETEXT_CANDIDATE and decision is Decision.REGISTER_VALUE:
            items = [i for i in items if i.subject.get("info_type")]
        elif kind is ReviewKind.FREETEXT_CANDIDATE and decision is Decision.REGISTER_NEW:
            raise ValueError("candidates must be registered one by one (choose the entity type)")
        if decision not in ALLOWED[kind]:
            raise ValueError(f"decision {decision.value} is not allowed for {kind.value}")
        if decision in (Decision.LINK_EXISTING, Decision.REPLACE_TERM):
            raise ValueError("this decision must be made one by one")
        self._require(run_id, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        with self.project.transaction() as conn:
            for item in items:
                self._apply_decision(conn, run_id, item, decision, None, info_type=item.subject.get("info_type"))
            self._back_to_planned(conn, run_id)
        return len(items)

    def _apply_decision(self, conn, run_id: int, item: ReviewItem, decision: Decision, entity_id: str | None,
                        **options):
        master = Master(conn)
        subject = item.subject
        record: dict = {"decision": decision.value}
        if item.kind in (ReviewKind.FREETEXT_SHORT, ReviewKind.FREETEXT_AMBIGUOUS):
            if decision is Decision.REPLACE_TERM:
                keys = {c["key"] for c in subject.get("candidates", [])}
                if options.get("target") not in keys:
                    raise ValueError("choose one of the replacement candidates")
                record["target"] = options["target"]
            reviews.set_decision(conn, item.id, record)
            return
        if item.kind is ReviewKind.FREETEXT_CANDIDATE:
            self._apply_candidate(conn, run_id, item, decision, entity_id, record, **options)
            return
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

    def _apply_candidate(self, conn, run_id: int, item: ReviewItem, decision: Decision, entity_id: str | None,
                         record: dict, target=None, entity_type=None, info_type=None, text=None) -> None:
        """規則で検出した候補を、対象の名称・別名または値として登録する（以後は辞書の語句として置換する）。"""
        master = Master(conn)
        found = item.subject["text"]
        text = found if text is None else text.strip()
        if decision is not Decision.FALSE_POSITIVE:
            if not text or text not in found:
                raise ValueError("the registered text must be part of the detected text")
            if text != found and is_short(normalize_for_search(text)):
                raise ValueError("a short word cannot be registered from part of a candidate")
            record["text"] = text
        if decision is Decision.REGISTER_NEW:
            exists = conn.execute("SELECT 1 FROM entity_type WHERE id = ?", (entity_type,)).fetchone()
            if not exists:
                raise ValueError("choose the entity type to register")
            new_id = master.create_entity(entity_type, run_id)
            master.add_name(new_id, text, "primary", run_id)
            record.update(entity_id=new_id, target=f"name:{new_id}")
        elif decision is Decision.LINK_EXISTING:
            status = conn.execute("SELECT status FROM entity WHERE id = ?", (entity_id,)).fetchone()
            if not entity_id or status is None or status[0] not in ("draft", "confirmed"):
                raise ValueError("target entity does not exist")
            master.add_name(entity_id, text, "alias", run_id)
            record.update(entity_id=entity_id, target=f"name:{entity_id}")
        elif decision is Decision.REGISTER_VALUE:
            if info_type not in VALUE_TYPES:
                raise ValueError("choose the value type to register")
            master.value_token(info_type, text, run_id)
            record.update(info_type=info_type, target=f"value:{info_type}:{text}")
        reviews.set_decision(conn, item.id, record)

    def forget_decision(self, item_id: int) -> None:
        """前回の作業から引き継いだ判断を取り消して、判断し直せるようにする。"""
        item = reviews.get(self.conn, item_id)
        run_id = self.conn.execute("SELECT run_id FROM review_item WHERE id = ?", (item_id,)).fetchone()[0]
        self._require(run_id, RunState.PLANNED, RunState.STAGED, RunState.VERIFIED)
        if not (item.decision and item.decision.get("remembered")):
            raise ValueError("only decisions carried over from a previous run can be reopened")
        with self.project.transaction() as conn:
            reviews.forget(conn, item_id)
            self._back_to_planned(conn, run_id)

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
        """既定の出力ファイル名「{表の定義名}_{連番2桁}.{拡張子}」（元のファイル名は使わない）。"""
        counts: dict[str, int] = {}
        names = {}
        for rf in plan.run_files:
            units = plan.units_of(rf.file_id)
            base = units[0].template.name if units else "ファイル"
            ext = "csv" if isinstance(plan.sources[rf.file_id], CsvDocument) else "xlsx"
            counts[base] = counts.get(base, 0) + 1
            names[rf.file_id] = f"{base}_{counts[base]:02d}.{ext}"
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
        for rf in plan.run_files:
            data = render_file(plan, rf.file_id)
            path = staging / names[rf.file_id]
            path.write_bytes(data)
            rows.append((run_id, rf.file_id, names[rf.file_id], _sha256(data), len(data),
                         str(path.relative_to(self.project.root)), rf.position))
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
            reviews.remember_run(conn, run_id)
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
        for rf in plan.run_files:
            name, digest, staged_path = expected[rf.file_id]
            data = render_file(plan, rf.file_id)
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
