"""画面と処理の橋渡し（設計書 04 章）。画面はこのクラスの操作だけを使う。

画面に依存しない形にして、画面の手順（取込 → 設定 → 確認 → 検証・出力）を自動試験できるようにする。
変更はそのつど project.db に保存する（作業は Workbench、設定の下書きは ConfigDraft）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from maskapp.app.draft import ConfigDraft
from maskapp.app.registry import Registry
from maskapp.app.workbench import PlanSummary, RunState, Workbench
from maskapp.config import Config, ConfigError, RelationSpec, TemplateSpec, match_template
from maskapp.config.suggest import (
    HeaderDiff,
    RelationCandidate,
    adapt_template,
    best_template,
    compare_headers,
    suggest_relations,
    suggest_template,
)
from maskapp.formats.csvfile import CsvDocument, CsvFormatError
from maskapp.formats.xlsx.package import XlsxFormatError
from maskapp.infra.safelog import get_logger
from maskapp.ingest import UnsupportedFormatError
from maskapp.project import Project
from maskapp.transform.planner import grid_of

_log = get_logger("session")

# 取込で停止したときの案内（設計書 04 章 3「ファイル判定と案内」）
IMPORT_ERRORS = {
    "encrypted_or_legacy": "パスワード付き、または古い形式（.xls）のファイルは扱えません。Excel でパスワードを解除し、"
                           ".xlsx として保存し直してください。",
    "unsupported_spreadsheet_format": "この形式は対象外です。Excel で .xlsx として保存し直してください"
                                      "（マクロは失われます）。",
    "strict_ooxml": "「Strict Open XML」形式は扱えません。Excel で通常の .xlsx として保存し直してください。",
    "unknown_file_type": "対応していないファイルの種類です（.xlsx・.csv・.txt・.tsv が対象です）。",
    "csv": "CSV ファイルを読み取れません（文字コード・引用符の閉じ忘れ等）。",
}
FEATURE_LABELS = {
    "pivot": "ピボットテーブル（未対応）", "external_link": "外部リンク（未対応）",
    "connection": "データ接続（未対応）",
    "data_model": "データモデル（未対応）", "macro": "マクロ（未対応）", "activex": "ActiveX（未対応）",
    "ole_object": "埋め込みオブジェクト（未対応）", "revisions": "変更履歴（未対応）", "slicer": "スライサー（未対応）",
    "control": "フォームコントロール（未対応）", "custom_xml": "カスタム XML（未対応）",
    "rich_data": "セル内の画像等（未対応）",
    "chartsheet": "グラフシート（未対応）", "dialogsheet": "ダイアログシート（未対応）",
    "web_extension": "アドイン（未対応）", "unknown_part": "不明な部品（未対応）",
    "comments": "コメント", "chart": "グラフ", "image": "画像", "hidden_sheets": "非表示シート", "formulas": "数式",
}


@dataclass(frozen=True)
class ImportResult:
    path: Path
    file_id: int | None
    error: str | None  # 取込で停止した場合の案内文


@dataclass(frozen=True)
class TableRow:
    """取込画面の1行（1つの表＝CSV ファイル、または xlsx の1シート）。"""

    file_id: int
    sheet: int
    file_name: str
    sheet_name: str
    visibility: str
    template: str | None
    header_row: int  # 0 起点
    data_first: int | None  # 0 起点
    data_last: int | None
    columns: int
    status: str  # 一致／列が追加されています／新しい表 等
    suggestion: str | None  # 未割当のとき、差分付きで合いそうな既存の定義
    notes: list[str]


def guess_header_row(rows: list[list[str]], limit: int = 10) -> int:
    """見出し行の推定：先頭から見て、空でないセルが最も多い行のうち最初のもの（タイトル行を飛ばす）。"""
    best, best_count = 0, -1
    for r, row in enumerate(rows[:limit]):
        count = sum(1 for v in row if v)
        if count > best_count:
            best, best_count = r, count
    return best


def column_letter(col: int) -> str:
    out = ""
    n = col + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


class Session:
    def __init__(self, project: Project, registry: Registry | None = None) -> None:
        self.project = project
        self.registry = registry or Registry()
        self.wb = Workbench(project)
        self.draft = ConfigDraft.load(project)
        self._sources: dict[int, object] = {}

    # ---------------------------------------------------------------- 開閉

    @classmethod
    def create(cls, parent: Path, name: str, color: str | None = None, registry: Registry | None = None) -> Session:
        project = Project.create(parent, name, color)
        session = cls(project, registry)
        session.registry.add(project.root)
        return session

    @classmethod
    def open(cls, root: Path, registry: Registry | None = None) -> Session:
        project = Project.open(root)
        session = cls(project, registry)
        session.registry.add(project.root)
        return session

    def close(self) -> None:
        if self.project.is_open:
            self.project.close()

    @property
    def run_id(self) -> int | None:
        return self.wb.active_run()

    def ensure_run(self) -> int:
        return self.run_id or self.wb.start_run()

    def state(self) -> RunState | None:
        run = self.run_id
        return self.wb.state(run) if run else None

    def step(self) -> int:
        return self.wb.current_step()

    def source(self, file_id: int):
        if file_id not in self._sources:
            self._sources[file_id] = self.wb.source(file_id)
        return self._sources[file_id]

    def rows(self, file_id: int, sheet: int) -> list[list[str]]:
        return grid_of(self.source(file_id), sheet).rows

    def save_draft(self) -> None:
        self.draft.save(self.project)

    # ---------------------------------------------------------------- ② 取込

    def import_paths(self, paths: list[Path]) -> list[ImportResult]:
        """ファイルを取り込み、作業に加える。既存の表の定義と見出しが一致するシートには定義を適用する。"""
        run = self.ensure_run()
        results = []
        for path in paths:
            path = Path(path)
            try:
                imported = self.wb.import_file(path)
            except (XlsxFormatError, UnsupportedFormatError) as exc:
                results.append(ImportResult(path, None, IMPORT_ERRORS.get(
                    exc.reason, "ファイルを読み取れません（壊れているか、対応していない形式です）。")))
                continue
            except CsvFormatError:
                results.append(ImportResult(path, None, IMPORT_ERRORS["csv"]))
                continue
            self.wb.add_file(run, imported.id)
            source = self.source(imported.id)
            sheets = [0] if isinstance(source, CsvDocument) else [s.idx for s in source.sheets]
            assigned = {u.sheet for u in self.wb.run_units(run) if u.file_id == imported.id}
            for sheet in sheets:
                if sheet in assigned:
                    continue
                name = match_template(self.draft.config(), self.rows(imported.id, sheet))
                if name is not None:
                    self.wb.assign_sheet(run, imported.id, sheet, name)
            results.append(ImportResult(path, imported.id, None))
        _log.event("files_imported", count=len(paths), failed=sum(1 for r in results if r.error))
        return results

    def tables(self) -> list[TableRow]:
        run = self.run_id
        if run is None:
            return []
        units = {(u.file_id, u.sheet): u.template_name for u in self.wb.run_units(run)}
        config = self.draft.config()
        out = []
        for rf in self.wb.run_files(run):
            info = self.wb.file_info(rf.file_id)
            source = self.source(rf.file_id)
            features = info["info"].get("features", {})
            file_notes = [f"{FEATURE_LABELS[k]} {v}" for k, v in features.items()
                          if k in FEATURE_LABELS and v and k not in ("hidden_sheets", "formulas")]
            if features.get("formulas"):
                file_notes.append(f"数式 {features['formulas']}")
            sheets = [(0, "", "visible")] if isinstance(source, CsvDocument) else info["sheets"]
            for idx, sheet_name, visibility in sheets:
                rows = self.rows(rf.file_id, idx)
                template_name = units.get((rf.file_id, idx))
                template = config.templates.get(template_name) if template_name else None
                notes = list(file_notes)
                if visibility != "visible":
                    notes.insert(0, "非表示シート")
                suggestion = None
                if template is not None:
                    header_row = template.header_row
                    headers = rows[header_row] if header_row < len(rows) else []
                    status = compare_headers(template, headers).status
                    data = template.data_rows(rows)
                    excluded = len(range(header_row + 1, len(rows))) - len(data)
                    if excluded:
                        notes.append(f"合計行として除いた行 {excluded}")
                else:
                    header_row = guess_header_row(rows)
                    match = best_template(config, rows)
                    status = "新しい表" if match.name is None else "未割当"
                    suggestion = match.name if match.name else None
                    data = list(range(header_row + 1, len(rows)))
                above = sum(1 for r in rows[:header_row] for v in r if v)
                if above:
                    notes.append(f"表外の文字 {above}")
                width = len(rows[header_row]) if header_row < len(rows) else 0
                out.append(TableRow(rf.file_id, idx, info["name"], sheet_name, visibility, template_name,
                                    header_row, data[0] if data else None, data[-1] if data else None, width,
                                    status, suggestion, notes))
        return out

    def new_template_name(self, base: str = "表") -> str:
        n = 1
        while f"{base}{n}" in self.draft.templates:
            n += 1
        return f"{base}{n}"

    def create_template(self, file_id: int, sheet: int, name: str, header_row: int | None = None) -> TemplateSpec:
        """新しい表の定義を列設定の候補から作り、シートに適用する（列はすべて未確認）。"""
        name = name.strip()
        if not name:
            raise ConfigError("template name is empty")
        if name in self.draft.templates:
            raise ConfigError("a table definition with this name already exists")
        rows = self.rows(file_id, sheet)
        template = suggest_template(name, rows, guess_header_row(rows) if header_row is None else header_row)
        if not template.columns:
            raise ConfigError("the header row is empty")
        self.draft.put_template(template, confirmed=set())
        self.save_draft()
        self.wb.assign_sheet(self.ensure_run(), file_id, sheet, name)
        return template

    def adapt_template(self, file_id: int, sheet: int, name: str) -> tuple[TemplateSpec, HeaderDiff]:
        """既存の定義を、このシートの見出しに合わせて更新する（追加・改名された列は未確認にする）。"""
        old = self.draft.templates[name]
        rows = self.rows(file_id, sheet)
        headers = rows[old.header_row] if old.header_row < len(rows) else []
        diff = compare_headers(old, headers)
        updated, unconfirmed = adapt_template(old, headers)
        confirmed = self.draft.confirmed.get(name, set()) & set(updated.headers)
        self.draft.put_template(updated, confirmed=confirmed - set(unconfirmed))
        self.save_draft()
        # 同じ定義を適用した他のファイルの見出しとの一致は、設定の確定時に確かめる
        self.wb.assign_sheet(self.ensure_run(), file_id, sheet, name)
        return updated, diff

    def assign(self, file_id: int, sheet: int, name: str | None) -> None:
        self.wb.assign_sheet(self.ensure_run(), file_id, sheet, name)

    def remove_file(self, file_id: int) -> None:
        self.wb.remove_input(self.ensure_run(), file_id)

    def set_region(self, name: str, header_row: int | None = None, skip_total_rows: bool | None = None,
                   outside_action=None) -> None:
        t = self.draft.templates[name]
        updated = TemplateSpec(
            t.name, t.columns, t.header_row if header_row is None else header_row,
            t.skip_total_rows if skip_total_rows is None else skip_total_rows,
            t.outside_action if outside_action is None else outside_action,
        )
        self.draft.put_template(updated)
        self.save_draft()
        if self.run_id:
            self.wb.back_to_draft(self.run_id)

    def set_options(self, file_id: int, options) -> None:
        self.wb.set_options(self.ensure_run(), file_id, options)

    # ---------------------------------------------------------------- ③ 設定

    def templates_in_run(self) -> list[str]:
        run = self.run_id
        if run is None:
            return []
        names: list[str] = []
        for u in self.wb.run_units(run):
            if u.template_name not in names:
                names.append(u.template_name)
        return names

    def column_values(self, name: str, limit: int | None = None) -> dict[str, list[str]]:
        """表の定義の各列の値（作業に含めた全シート分）。"""
        template = self.draft.templates[name]
        out: dict[str, list[str]] = {h: [] for h in template.headers}
        for u in self.wb.run_units(self.ensure_run()):
            if u.template_name != name:
                continue
            rows = self.rows(u.file_id, u.sheet)
            for r in template.data_rows(rows):
                row = rows[r]
                for i, h in enumerate(template.headers):
                    out[h].append(row[i] if i < len(row) else "")
                if limit and len(out[template.headers[0]]) >= limit:
                    return out
        return out

    def samples(self, name: str, n: int = 5) -> dict[str, list[str]]:
        """列ごとの先頭の異なる値（最大 n 件）。"""
        out = {}
        for h, values in self.column_values(name, limit=2000).items():
            seen: list[str] = []
            for v in values:
                if v and v not in seen:
                    seen.append(v)
                    if len(seen) >= n:
                        break
            out[h] = seen
        return out

    def update_column(self, name: str, index: int, spec) -> None:
        t = self.draft.templates[name]
        columns = list(t.columns)
        columns[index] = spec
        self.draft.put_template(TemplateSpec(t.name, columns, t.header_row, t.skip_total_rows, t.outside_action))
        self.draft.set_confirmed(name, spec.header, True)
        self.save_draft()
        if self.run_id:
            self.wb.back_to_draft(self.run_id)

    def confirm_column(self, name: str, header: str, confirmed: bool = True) -> None:
        self.draft.set_confirmed(name, header, confirmed)
        self.save_draft()

    def confirm_all_columns(self, name: str) -> None:
        self.draft.confirm_all(name)
        self.save_draft()

    def relation_candidates(self) -> list[RelationCandidate]:
        config = self.draft.config()
        values = {}
        for name in self.templates_in_run():
            for header, vals in self.column_values(name).items():
                values[(name, header)] = vals
        return [c for c in suggest_relations(config, values)
                if (c.relation.from_template, c.relation.from_column, c.relation.to_template,
                    c.relation.to_column) not in self.draft.rejected]

    def add_relation(self, rel: RelationSpec) -> None:
        self.draft.add_relation(rel)
        self.save_draft()

    def reject_relation(self, rel: RelationSpec) -> None:
        self.draft.reject_relation(rel)
        self.save_draft()

    def remove_relation(self, rel: RelationSpec) -> None:
        self.draft.remove_relation(rel)
        self.save_draft()

    def blocking_config_issues(self) -> list[str]:
        """④ へ進む前に解決が必要なこと（画面に表示する文）。"""
        issues = []
        run = self.run_id
        if run is None or not self.wb.run_files(run):
            return ["ファイルが取り込まれていません。"]
        units = self.wb.run_units(run)
        assigned = {(u.file_id, u.sheet) for u in units}
        for row in self.tables():
            if (row.file_id, row.sheet) not in assigned and row.visibility == "visible" and row.columns:
                if isinstance(self.source(row.file_id), CsvDocument):
                    issues.append("表の定義が決まっていない CSV ファイルがあります"
                                  "（②で決めるか、作業から外してください）。")
        for u in units:
            template = self.draft.templates.get(u.template_name)
            if template is None:
                issues.append(f"表の定義「{u.template_name}」がありません。")
                continue
            rows = self.rows(u.file_id, u.sheet)
            if match_template(Config(None, {template.name: template}), rows) is None:
                issues.append(f"表の定義「{template.name}」とファイルの見出しが一致しません"
                              "（取込画面で更新してください）。")
        for name, header in self.draft.unconfirmed(self.templates_in_run()):
            issues.append(f"未確認の列：{name}・{header}")
        for t in self.draft.templates.values():
            try:
                t.validate()
            except ConfigError:
                issues.append(f"表の定義「{t.name}」の列設定に不足があります（置換する列の種類・コード体系）。")
        return issues

    def confirm_config(self) -> PlanSummary:
        """設定を確定して計画を作る（④へ）。"""
        issues = self.blocking_config_issues()
        if issues:
            raise ConfigError("configuration is incomplete")
        current = self.wb.config()
        same = current.version_id is not None and not self.draft.changed and {
            n: t.to_json() for n, t in current.templates.items()} == {
            n: t.to_json() for n, t in self.draft.templates.items()} and current.relations == self.draft.relations
        if not same:
            self.draft.publish(self.project)
        run = self.ensure_run()
        if self.wb.state(run) is not RunState.DRAFT:
            self.wb.back_to_draft(run)
        return self.wb.plan(run)

