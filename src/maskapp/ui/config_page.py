"""画面3：変換設定（設計書 04 章 4）。タブA 列設定、タブB ファイル間の関連。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from maskapp.app.session import Session
from maskapp.config import Action, ColumnSpec, RelationSpec, Role
from maskapp.config.suggest import CODE_SCHEMES
from maskapp.ui.common import MASK, UNCONFIRMED_BG, highlight, run_safely
from maskapp.ui.labels import ACTIONS, OUTSIDE, ROLES, VALUE_TYPES


def describe(spec: ColumnSpec, entity_names: dict[str, str]) -> tuple[str, str, str]:
    """(処理, 種類, 詳細) の表示。"""
    action = ACTIONS[spec.action.value]
    if spec.action is not Action.REPLACE:
        return action, "", ""
    if spec.role is Role.VALUE:
        return action, VALUE_TYPES.get(spec.info_type or "", spec.info_type or ""), "その他の値"
    kind = entity_names.get(spec.entity_type or "", spec.entity_type or "")
    role = ROLES.get(spec.role.value if spec.role else "", "")
    detail = f"{role}（{spec.code_scheme}）" if spec.role is Role.CODE else role
    if spec.binding:
        detail += f"・組 {spec.binding}"
    return action, kind, detail


class ColumnEditor(QGroupBox):
    """選んだ列の設定を変える。"""

    def __init__(self, entity_types: list[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__("選んだ列の設定", parent)
        self.action = QComboBox()
        for key, label in ACTIONS.items():
            self.action.addItem(label, key)
        self.entity = QComboBox()
        for key, label in entity_types:
            self.entity.addItem(label, key)
        self.role = QComboBox()
        for key, label in ROLES.items():
            self.role.addItem(label, key)
        self.scheme = QComboBox()
        self.scheme.setEditable(True)
        self.value_type = QComboBox()
        for key, label in VALUE_TYPES.items():
            self.value_type.addItem(label, key)
        self.binding = QComboBox()
        self.binding.setEditable(True)
        self.binding.addItem("", None)
        self.save = QPushButton("この設定で確認済みにする")
        form = QFormLayout(self)
        form.addRow("処理", self.action)
        form.addRow("種類", self.entity)
        form.addRow("役割", self.role)
        form.addRow("コード体系", self.scheme)
        form.addRow("その他の値の種類", self.value_type)
        form.addRow("組（同じ種類の列が複数の組）", self.binding)
        form.addRow(self.save)
        for w in (self.action, self.role):
            w.currentIndexChanged.connect(self._enable)
        self._enable()

    def _enable(self) -> None:
        replace = self.action.currentData() == "replace"
        role = self.role.currentData()
        self.entity.setEnabled(replace and role != "value")
        self.role.setEnabled(replace)
        self.scheme.setEnabled(replace and role == "code")
        self.value_type.setEnabled(replace and role == "value")
        self.binding.setEnabled(replace and role != "value")

    def load(self, spec: ColumnSpec, schemes: list[str]) -> None:
        self.action.setCurrentIndex(self.action.findData(spec.action.value))
        if spec.entity_type:
            self.entity.setCurrentIndex(self.entity.findData(spec.entity_type))
        self.role.setCurrentIndex(self.role.findData(spec.role.value if spec.role else "name"))
        self.scheme.clear()
        for s in schemes:
            self.scheme.addItem(s)
        self.scheme.setCurrentText(spec.code_scheme or CODE_SCHEMES.get(self.entity.currentData() or "", ""))
        if spec.info_type in VALUE_TYPES:
            self.value_type.setCurrentIndex(self.value_type.findData(spec.info_type))
        self.binding.setCurrentText(spec.binding or "")
        self._enable()

    def spec(self, header: str) -> ColumnSpec:
        action = Action(self.action.currentData())
        if action is not Action.REPLACE:
            return ColumnSpec(header, action)
        role = Role(self.role.currentData())
        if role is Role.VALUE:
            return ColumnSpec(header, action, info_type=self.value_type.currentData(), role=role)
        entity = self.entity.currentData()
        scheme = (self.scheme.currentText().strip() or CODE_SCHEMES.get(entity, "コード")) if role is Role.CODE else None
        return ColumnSpec(header, action, entity_type=entity, role=role, code_scheme=scheme,
                          binding=self.binding.currentText().strip() or None)


class ConfigPage(QWidget):
    changed = Signal()
    next_requested = Signal()
    back_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session: Session | None = None
        self.tabs = QTabWidget()

        # ---- タブA 列設定
        self.template_combo = QComboBox()
        self.template_combo.currentIndexChanged.connect(self._load_columns)
        self.region_label = QLabel()
        confirm_all = QPushButton("推定どおりで全列を確認済みにする")
        confirm_all.clicked.connect(self._confirm_all)
        head = QHBoxLayout()
        head.addWidget(QLabel("表の定義"))
        head.addWidget(self.template_combo, 1)
        head.addWidget(confirm_all)
        self.columns = QTableWidget(0, 6)
        self.columns.setHorizontalHeaderLabels(["列名", "サンプル（先頭の異なる値）", "処理", "種類", "詳細", "確認"])
        self.columns.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.columns.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.columns.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.columns.itemSelectionChanged.connect(self._column_selected)
        self.editor = ColumnEditor([])
        self.editor.save.clicked.connect(self._save_column)
        tab_a = QWidget()
        la = QVBoxLayout(tab_a)
        la.addLayout(head)
        la.addWidget(self.region_label)
        body = QHBoxLayout()
        body.addWidget(self.columns, 3)
        body.addWidget(self.editor, 1)
        la.addLayout(body)
        self.tabs.addTab(tab_a, "A 列設定")

        # ---- タブB 関連
        self.candidates = QTableWidget(0, 5)
        self.candidates.setHorizontalHeaderLabels(["参照元（表.列）", "参照先（表.列）", "一致率", "参照先にない値の種類",
                                                   "参照先の重複"])
        self.candidates.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.candidates.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.candidates.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        accept = QPushButton("候補を確定")
        accept.clicked.connect(self._accept_candidate)
        reject = QPushButton("候補を却下")
        reject.clicked.connect(self._reject_candidate)
        self.relations = QListWidget()
        remove = QPushButton("確定した関連を外す")
        remove.clicked.connect(self._remove_relation)
        self.manual = [QComboBox() for _ in range(2)]
        add = QPushButton("手動で追加")
        add.clicked.connect(self._add_manual)
        tab_b = QWidget()
        lb = QVBoxLayout(tab_b)
        lb.addWidget(QLabel("同じコード体系の列どうしで、値の重なりから候補を出します（自動では確定しません）。"))
        lb.addWidget(self.candidates)
        row = QHBoxLayout()
        row.addWidget(accept)
        row.addWidget(reject)
        row.addStretch()
        lb.addLayout(row)
        lb.addWidget(QLabel("確定した関連（検証で、変換前後の結合件数を比べます）"))
        lb.addWidget(self.relations)
        row2 = QHBoxLayout()
        row2.addWidget(remove)
        row2.addWidget(QLabel("参照元"))
        row2.addWidget(self.manual[0], 1)
        row2.addWidget(QLabel("→ 参照先"))
        row2.addWidget(self.manual[1], 1)
        row2.addWidget(add)
        lb.addLayout(row2)
        self.tabs.addTab(tab_b, "B ファイル間の関連")
        self.tabs.currentChanged.connect(lambda i: self._load_relations() if i == 1 else None)

        self.issues = QListWidget()
        self.issues.setMaximumHeight(110)
        back = QPushButton("② ファイル取込に戻る")
        back.clicked.connect(self.back_requested.emit)
        self.next_btn = QPushButton("設定を確定して ④ 変換前後の確認へ")
        self.next_btn.clicked.connect(self.next_requested.emit)
        bottom = QHBoxLayout()
        bottom.addWidget(back)
        bottom.addStretch()
        bottom.addWidget(self.next_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(QLabel("④へ進む前に解決が必要なこと"))
        layout.addWidget(self.issues)
        layout.addLayout(bottom)
        MASK.changed.connect(lambda _: self._load_columns())

    # ---------------------------------------------------------------- 表示

    def set_session(self, session: Session | None) -> None:
        self.session = session
        if session:
            types = [tuple(r) for r in session.project.conn.execute(
                "SELECT id, display_name FROM entity_type ORDER BY rowid")]
            self.editor.entity.clear()
            for key, label in types:
                self.editor.entity.addItem(label, key)
        self.refresh()

    def refresh(self) -> None:
        s = self.session
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        if s:
            for name in s.templates_in_run():
                self.template_combo.addItem(name)
        self.template_combo.blockSignals(False)
        self._load_columns()
        if self.tabs.currentIndex() == 1:
            self._load_relations()
        self._load_issues()

    def _entity_names(self) -> dict[str, str]:
        return {self.editor.entity.itemData(i): self.editor.entity.itemText(i) for i in range(self.editor.entity.count())}

    def _load_columns(self) -> None:
        s = self.session
        name = self.template_combo.currentText()
        if not s or name not in s.draft.templates:
            self.columns.setRowCount(0)
            self.region_label.setText("")
            return
        template = s.draft.templates[name]
        samples = s.samples(name)
        confirmed = s.draft.confirmed.get(name, set())
        self.region_label.setText(
            f"見出し行：{template.header_row + 1}行目　合計行：{'除く' if template.skip_total_rows else '含める'}　"
            f"表外：{OUTSIDE[template.outside_action.value]}（②で変更できます）")
        names = self._entity_names()
        self.columns.setRowCount(len(template.columns))
        for i, spec in enumerate(template.columns):
            action, kind, detail = describe(spec, names)
            sample = "、".join(MASK.show(v) for v in samples.get(spec.header, []))
            done = spec.header in confirmed
            values = [spec.header, sample, action, kind, detail, "確認済み" if done else "未確認"]
            for j, v in enumerate(values):
                item = QTableWidgetItem(v)
                if not done:
                    highlight(item, UNCONFIRMED_BG)
                self.columns.setItem(i, j, item)

    def _column_selected(self) -> None:
        s = self.session
        name = self.template_combo.currentText()
        row = self.columns.currentRow()
        if not s or name not in s.draft.templates or row < 0:
            return
        schemes = [r[0] for r in s.project.conn.execute("SELECT DISTINCT display_name FROM code_scheme ORDER BY 1")]
        schemes += [v for v in CODE_SCHEMES.values() if v not in schemes]
        bindings = sorted({c.binding for t in s.draft.templates.values() for c in t.columns if c.binding})
        self.editor.binding.clear()
        self.editor.binding.addItem("")
        self.editor.binding.addItems(bindings)
        self.editor.load(s.draft.templates[name].columns[row], schemes)

    def _save_column(self) -> None:
        s = self.session
        name = self.template_combo.currentText()
        row = self.columns.currentRow()
        if not s or row < 0:
            return
        header = s.draft.templates[name].columns[row].header
        run_safely(self, s.update_column, name, row, self.editor.spec(header))
        self._after_change(row)

    def _confirm_all(self) -> None:
        s = self.session
        name = self.template_combo.currentText()
        if s and name:
            s.confirm_all_columns(name)
            self._after_change()

    def _load_relations(self) -> None:
        s = self.session
        self.candidates.setRowCount(0)
        self.relations.clear()
        for combo in self.manual:
            combo.clear()
        if not s:
            return
        self._candidates = run_safely(self, s.relation_candidates) or []
        self.candidates.setRowCount(len(self._candidates))
        for i, c in enumerate(self._candidates):
            r = c.relation
            values = [f"{r.from_template}.{r.from_column}", f"{r.to_template}.{r.to_column}",
                      f"{c.overlap:.0%}（{c.matched:,}/{c.from_values:,}行）", f"{c.unmatched_keys:,}",
                      f"{c.duplicate_target_keys:,}"]
            for j, v in enumerate(values):
                self.candidates.setItem(i, j, QTableWidgetItem(v))
        for r in s.draft.relations:
            self.relations.addItem(f"{r.from_template}.{r.from_column} → {r.to_template}.{r.to_column}")
        for name in s.templates_in_run():
            for h in s.draft.templates[name].headers:
                for combo in self.manual:
                    combo.addItem(f"{name}.{h}", (name, h))

    def _accept_candidate(self) -> None:
        i = self.candidates.currentRow()
        if self.session and 0 <= i < len(self._candidates):
            self.session.add_relation(self._candidates[i].relation)
            self._after_change()
            self._load_relations()

    def _reject_candidate(self) -> None:
        i = self.candidates.currentRow()
        if self.session and 0 <= i < len(self._candidates):
            self.session.reject_relation(self._candidates[i].relation)
            self._load_relations()

    def _remove_relation(self) -> None:
        i = self.relations.currentRow()
        if self.session and 0 <= i < len(self.session.draft.relations):
            self.session.remove_relation(self.session.draft.relations[i])
            self._after_change()
            self._load_relations()

    def _add_manual(self) -> None:
        a, b = (c.currentData() for c in self.manual)
        if self.session and a and b and a[0] != b[0]:
            self.session.add_relation(RelationSpec(a[0], a[1], b[0], b[1]))
            self._after_change()
            self._load_relations()

    def _load_issues(self) -> None:
        self.issues.clear()
        issues = self.session.blocking_config_issues() if self.session else []
        if not issues and self.session:
            self.issues.addItem("なし（④へ進めます）")
        for text in issues[:200]:
            self.issues.addItem(text)
        if len(issues) > 200:
            self.issues.addItem(f"ほか {len(issues) - 200} 件")
        self.next_btn.setEnabled(bool(self.session) and not issues)

    def _after_change(self, select_row: int | None = None) -> None:
        self._load_columns()
        self._load_issues()
        if select_row is not None and select_row + 1 < self.columns.rowCount():
            self.columns.selectRow(select_row + 1)
        self.changed.emit()

