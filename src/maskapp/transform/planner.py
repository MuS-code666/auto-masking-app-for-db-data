"""変換計画の作成（設計書 05 章 1〜3）。

確定した設定と対応表だけから、どのセルを何に変えるかを決める。プレビュー・出力・検証は
すべてこの計画を使う。新しい対象は計画の作成時に仮登録して番号を確保するため、
プレビューで見えた番号がそのまま出力の番号になる。

計画の単位は「表の定義を適用した表」（CSV は1ファイル、xlsx は1シート）。xlsx は表の単位の
変更に加えて、ブック全体の変更（数式・キャッシュ値・部品の除去）を xlsx_plan で決める。

決定性: 同じ入力・設定・対応表からは同じ計画を作る。新しい対象の番号は
「作業内のファイル順 → シート順 → 行順 → 列順」の初出順で確保する。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from maskapp.config.model import Action, Config, ConfigError, OutsideAction, Role, TemplateSpec
from maskapp.entity.codes import OutputRule
from maskapp.entity.master import Master
from maskapp.formats.csvfile import CsvDocument
from maskapp.formats.xlsx.book import XlsxBook
from maskapp.transform import reviews
from maskapp.transform.freetext import FreeTextPlanner
from maskapp.transform.reviews import ReviewKind

CAPACITY_WARN_RATIO = 0.2

Source = CsvDocument | XlsxBook


@dataclass(frozen=True)
class Assignment:
    """1セルの変換結果。value が None なら未解決（人の判断待ち）。"""

    value: str | None
    kind: str  # "entity_code" / "entity_name" / "token" / "clear" / "freetext"
    ref: str | None = None  # entity_id または info_type
    scheme_id: int | None = None


@dataclass(frozen=True)
class Grid:
    """表の値（すべて文字列）。CSV はレコード、xlsx はシートのセル（照合用の文字列）。"""

    rows: list[list[str]]
    blank: list[bool]

    @classmethod
    def of_csv(cls, doc: CsvDocument) -> Grid:
        return cls(doc.values(), [r.is_blank for r in doc.records])

    @classmethod
    def of_rows(cls, rows: list[list[str]]) -> Grid:
        return cls(rows, [not any(r) for r in rows])

    def cell(self, r: int, c: int | None) -> str:
        if c is None or r >= len(self.rows):
            return ""
        row = self.rows[r]
        return row[c] if c < len(row) else ""


def grid_of(source: Source, sheet: int) -> Grid:
    if isinstance(source, CsvDocument):
        return Grid.of_csv(source)
    return Grid.of_rows(source.sheet(sheet).grid())


@dataclass(frozen=True)
class RunFile:
    file_id: int
    position: int
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RunUnit:
    file_id: int
    sheet: int
    template_name: str


@dataclass
class FilePlan:
    """1つの表（CSV ファイル、または xlsx の1シート）の変更。"""

    file_id: int
    sheet: int
    position: int
    template: TemplateSpec
    grid: Grid
    changes: dict[tuple[int, int], Assignment] = field(default_factory=dict)
    data_rows: list[int] = field(default_factory=list)  # データ行（合計行を除く設定ならそれを除く）
    # 文章内を検査したセル → 置換しなかった部分（検証ではこの部分を残存検査する）
    freetext: dict[tuple[int, int], tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.data_rows:
            self.data_rows = self.template.data_rows(self.grid.rows)
        self._rows = set(self.data_rows)

    def is_outside(self, r: int) -> bool:
        """表外の行（見出しより上、または除いた合計行）。"""
        return r < self.template.header_row or (r > self.template.header_row and r not in self._rows)

    def text_changes(self) -> dict[tuple[int, int], str]:
        return {k: a.value for k, a in self.changes.items() if a.value is not None}

    @property
    def unresolved(self) -> int:
        return sum(1 for a in self.changes.values() if a.value is None)


@dataclass(frozen=True)
class PlanNote:
    origin: str  # "source" / "transform"
    severity: str  # "info" / "warn"
    kind: str
    detail: dict


@dataclass
class Plan:
    run_id: int
    config: Config
    files: list[FilePlan]  # 表の単位
    notes: list[PlanNote]
    run_files: list[RunFile] = field(default_factory=list)
    sources: dict[int, Source] = field(default_factory=dict)
    books: dict = field(default_factory=dict)  # file_id → xlsx_plan.BookPlan
    freetext: FreeTextPlanner | None = None

    @property
    def unresolved(self) -> int:
        return sum(f.unresolved for f in self.files) + sum(b.unresolved for b in self.books.values())

    def units_of(self, file_id: int) -> list[FilePlan]:
        return [f for f in self.files if f.file_id == file_id]


@dataclass(frozen=True)
class _Binding:
    entity_type: str
    code_col: int | None
    scheme_id: int | None
    name_col: int | None


def _scheme_id(conn, entity_type: str, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM code_scheme WHERE entity_type_id = ? AND display_name = ?", (entity_type, name)
    ).fetchone()
    if row is None:
        raise ConfigError(f"code scheme '{name}' is not defined")
    return row[0]


def _bindings(conn, template: TemplateSpec) -> list[_Binding]:
    groups: dict[str, dict] = {}
    for idx, c in enumerate(template.columns):
        if c.action is not Action.REPLACE or c.role not in (Role.CODE, Role.NAME):
            continue
        g = groups.setdefault(c.binding_key, {"entity_type": c.entity_type, "first": idx})
        if c.role is Role.CODE:
            g["code_col"] = idx
            g["scheme_id"] = _scheme_id(conn, c.entity_type, c.code_scheme)
        else:
            g["name_col"] = idx
    ordered = sorted(groups.values(), key=lambda g: g["first"])
    return [
        _Binding(g["entity_type"], g.get("code_col"), g.get("scheme_id"), g.get("name_col")) for g in ordered
    ]


class _Resolver:
    """対象の照合と仮登録（設計書 05 章 2）。1回の計画作成の間だけ使うキャッシュを持つ。"""

    def __init__(self, conn, run_id: int) -> None:
        self.conn = conn
        self.run_id = run_id
        self.master = Master(conn)
        self.rules: dict[int, OutputRule] = {}
        self._by_code: dict[tuple[int, str], str] = {}
        self._by_name: dict[tuple[str, str], str | None] = {}
        self._names: dict[str, set[str]] = {}
        self._masked_name: dict[str, str] = {}
        self._masked_code: dict[tuple[str, int], str | None] = {}
        self.notes: dict[tuple, int] = {}

    def note(self, origin: str, severity: str, kind: str, **detail) -> None:
        key = (origin, severity, kind, tuple(sorted(detail.items())))
        self.notes[key] = self.notes.get(key, 0) + 1

    def names(self, eid: str) -> set[str]:
        if eid not in self._names:
            self._names[eid] = self.master.names_of(eid)
        return self._names[eid]

    def masked_name(self, eid: str) -> str:
        if eid not in self._masked_name:
            self._masked_name[eid] = self.master.masked_name(eid)
        return self._masked_name[eid]

    def masked_code(self, eid: str, scheme_id: int) -> str | None:
        key = (eid, scheme_id)
        if key not in self._masked_code:
            self._masked_code[key] = self.master.masked_code(eid, scheme_id)
        return self._masked_code[key]

    def _add_name(self, eid: str, entity_type: str, name: str, kind: str) -> None:
        self.master.add_name(eid, name, kind, self.run_id)
        self._names.pop(eid, None)
        self._by_name.pop((entity_type, name), None)

    def _new_entity(self, entity_type: str, location: dict, code: str | None, name: str | None) -> str:
        eid = self.master.create_entity(entity_type, self.run_id)
        reviews.upsert(
            self.conn, self.run_id, ReviewKind.NEW_ENTITY, eid,
            {"entity_id": eid, "entity_type": entity_type, "code": code, "name": name,
             "masked_name": self.masked_name(eid)},
            location,
        )
        return eid

    def by_code(self, b: _Binding, code: str, name: str, location: dict) -> str:
        key = (b.scheme_id, code)
        eid = self._by_code.get(key) or self.master.entity_by_code(b.scheme_id, code)
        if eid is None:
            eid = self._new_entity(b.entity_type, location, code, name or None)
            self.master.add_code(eid, b.scheme_id, code, self.run_id, self.rules[b.scheme_id])
            self._masked_code.pop((eid, b.scheme_id), None)
            if name:
                others = [x for x in self.master.entities_by_name(b.entity_type, name) if x != eid]
                self._add_name(eid, b.entity_type, name, "primary")
                if others:
                    # 同じ名称で別のコードの対象がある。既定は別の対象として扱う（設計書 05 章 2.1）
                    self.note("source", "info", "name_multi_code", entity_type=b.entity_type)
        elif name and name not in self.names(eid):
            status, created_run = self.master.entity_status(eid)
            if status == "draft" and created_run == self.run_id and not self.names(eid):
                self._add_name(eid, b.entity_type, name, "primary")
            else:
                reviews.upsert(
                    self.conn, self.run_id, ReviewKind.CODE_MULTI_NAME, f"{eid}:{name}",
                    {"entity_id": eid, "entity_type": b.entity_type, "code": code, "name": name,
                     "known_names": sorted(self.names(eid)), "masked_name": self.masked_name(eid)},
                    location,
                )
        self._by_code[key] = eid
        return eid

    def by_name(self, entity_type: str, name: str, location: dict) -> str | None:
        key = (entity_type, name)
        if key in self._by_name:
            return self._by_name[key]
        ids = self.master.entities_by_name(entity_type, name)
        result: str | None
        if len(ids) == 1:
            result = ids[0]
        elif ids:
            item = reviews.upsert(
                self.conn, self.run_id, ReviewKind.NAME_AMBIGUOUS, f"{entity_type}:{name}",
                {"entity_type": entity_type, "name": name,
                 "candidates": [{"entity_id": i, "masked_name": self.masked_name(i)} for i in ids]},
                location,
            )
            result = item.decision["entity_id"] if item.decision else None
        else:
            candidates = self.master.entities_by_normalized(entity_type, name)
            if candidates:
                item = reviews.upsert(
                    self.conn, self.run_id, ReviewKind.SIMILAR_NAME, f"{entity_type}:{name}",
                    {"entity_type": entity_type, "name": name,
                     "candidates": [
                         {"entity_id": i, "masked_name": self.masked_name(i), "names": sorted(self.names(i))}
                         for i in candidates
                     ]},
                    location,
                )
                result = item.decision["entity_id"] if item.decision else None
            else:
                result = self._new_entity(entity_type, location, None, name)
                self._add_name(result, entity_type, name, "primary")
        self._by_name[key] = result
        return result


def build_plan(
    conn, run_id: int, config: Config, run_files: list[RunFile], units: list[RunUnit], sources: dict[int, Source]
) -> Plan:
    """呼び出し側のトランザクション内で使う。"""
    from maskapp.transform.xlsx_plan import plan_book

    resolver = _Resolver(conn, run_id)
    file_order = {f.file_id: f.position for f in run_files}
    ordered = sorted(units, key=lambda u: (file_order[u.file_id], u.sheet))
    layouts = {}
    for unit in ordered:
        template = config.template(unit.template_name)
        layouts[unit.template_name] = (template, _bindings(conn, template))
    grids = {(u.file_id, u.sheet): grid_of(sources[u.file_id], u.sheet) for u in ordered}

    # 1. コード体系ごとの生成規則（未設定なら今回の観測コードから作る）
    observed: dict[int, list[str]] = {}
    for unit in ordered:
        template, bindings = layouts[unit.template_name]
        grid = grids[(unit.file_id, unit.sheet)]
        for b in bindings:
            if b.code_col is None:
                continue
            values = observed.setdefault(b.scheme_id, [])
            for r in template.data_rows(grid.rows):
                value = grid.cell(r, b.code_col)
                if value:
                    values.append(value)
    for scheme_id, values in observed.items():
        resolver.rules[scheme_id] = resolver.master.output_rule(scheme_id, values)

    # 2. 行ごとの照合と割当て
    files: list[FilePlan] = []
    for position, unit in enumerate(ordered):
        template, bindings = layouts[unit.template_name]
        fp = FilePlan(unit.file_id, unit.sheet, position, template, grids[(unit.file_id, unit.sheet)])
        value_cols = [
            (i, c.info_type) for i, c in enumerate(template.columns)
            if c.action is Action.REPLACE and c.role is Role.VALUE
        ]
        clear_cols = [i for i, c in enumerate(template.columns) if c.action is Action.CLEAR]
        for r in fp.data_rows:
            if fp.grid.blank[r]:
                continue

            def cell(c: int | None, r=r, grid=fp.grid) -> str:
                return grid.cell(r, c)

            for b in bindings:
                code, name = cell(b.code_col), cell(b.name_col)
                location = {"file_id": unit.file_id, "sheet": unit.sheet, "record": r}
                if code:
                    eid = resolver.by_code(b, code, name, location)
                elif name:
                    eid = resolver.by_name(b.entity_type, name, location)
                    if b.code_col is not None:
                        resolver.note("source", "info", "code_missing", entity_type=b.entity_type)
                else:
                    continue
                if code:
                    fp.changes[(r, b.code_col)] = Assignment(
                        resolver.masked_code(eid, b.scheme_id), "entity_code", eid, b.scheme_id
                    )
                if name:
                    fp.changes[(r, b.name_col)] = Assignment(
                        resolver.masked_name(eid) if eid else None, "entity_name", eid
                    )
            for c, info_type in value_cols:
                value = cell(c)
                if value:
                    fp.changes[(r, c)] = Assignment(
                        resolver.master.value_token(info_type, value, run_id), "token", info_type
                    )
            for c in clear_cols:
                if cell(c):
                    fp.changes[(r, c)] = Assignment("", "clear")
        if template.outside_action is OutsideAction.CLEAR:
            for r, values in enumerate(fp.grid.rows):
                if fp.is_outside(r):
                    for c, value in enumerate(values):
                        if value:
                            fp.changes[(r, c)] = Assignment("", "clear")
        files.append(fp)

    # 3. 文章内の語句（対応表が出そろった後に、その作業で仮登録した対象も含めて検索する）
    freetext = FreeTextPlanner(conn, run_id)
    replaced = sum(_plan_freetext(fp, freetext) for fp in files)

    notes = [
        PlanNote(origin, severity, kind, {**dict(detail), "count": count})
        for (origin, severity, kind, detail), count in resolver.notes.items()
    ]
    for scheme_id in observed:
        used, capacity = resolver.master.code_usage(scheme_id)
        if capacity and capacity - used < capacity * CAPACITY_WARN_RATIO:
            notes.append(PlanNote("transform", "warn", "code_capacity_low",
                                  {"scheme_id": scheme_id, "used": used, "capacity": capacity}))
    plan = Plan(run_id, config, files, notes, sorted(run_files, key=lambda f: f.position), dict(sources),
                freetext=freetext)

    # 4. xlsx のブック全体の変更（対応表が出そろった後に、数式の文字列定数・キャッシュ値を決める）
    for rf in plan.run_files:
        source = sources[rf.file_id]
        if isinstance(source, XlsxBook):
            book_plan = plan_book(conn, plan, rf, source)
            plan.books[rf.file_id] = book_plan
            plan.notes.extend(book_plan.notes)
    if replaced:
        plan.notes.append(PlanNote("transform", "info", "freetext_replaced", {"count": replaced}))
    return plan


def _plan_freetext(fp: FilePlan, freetext: FreeTextPlanner) -> int:
    """「文章内を検査」の列と、表外を「文章内を検査」にした表の表外のセル。置換したセル数を返す。"""
    template = fp.template
    columns = [i for i, c in enumerate(template.columns) if c.action is Action.FREETEXT]
    outside = template.outside_action is OutsideAction.FREETEXT
    if not columns and not outside:
        return 0
    row_entities: dict[int, set[str]] = {}
    for (r, _), a in fp.changes.items():
        if a.kind in ("entity_code", "entity_name") and a.ref:
            row_entities.setdefault(r, set()).add(a.ref)

    replaced = 0

    def apply(r: int, c: int, scope: str, label: str) -> None:
        nonlocal replaced
        text = fp.grid.cell(r, c)
        location = {"file_id": fp.file_id, "sheet": fp.sheet, "record": r, "column": c}
        result = freetext.process(text, scope, label, location, frozenset(row_entities.get(r, ())))
        if result is None:
            return
        fp.freetext[(r, c)] = result.kept
        if result.value is None or result.changed:
            fp.changes[(r, c)] = Assignment(result.value, "freetext")
            replaced += result.changed

    for c in columns:
        header = template.columns[c].header
        for r in fp.data_rows:
            if not fp.grid.blank[r]:
                apply(r, c, f"col:{template.name}:{header}", f"{template.name}・{header}")
    if outside:
        for r, values in enumerate(fp.grid.rows):
            if fp.is_outside(r):
                for c in range(len(values)):
                    apply(r, c, f"outside:{template.name}", f"{template.name}・表外")
    return replaced


def render_file(plan: Plan, file_id: int) -> bytes:
    """計画どおりに変更した出力ファイルのバイト列。"""
    source = plan.sources[file_id]
    if isinstance(source, CsvDocument):
        units = plan.units_of(file_id)
        return source.serialize(units[0].text_changes() if units else {})
    from maskapp.formats.xlsx.book import render

    return render(source, plan.books[file_id].edits)
