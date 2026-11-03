"""画面2：ファイル取込（設計書 04 章 3）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from maskapp.app.session import Session, TableRow, column_letter
from maskapp.config import OutsideAction
from maskapp.formats.xlsx.book import XlsxBook
from maskapp.transform.xlsx_plan import BookOptions
from maskapp.ui.common import HEADER_BG, MASK, OUTSIDE_BG, GridModel, run_safely
from maskapp.ui.labels import OUTSIDE

FILE_FILTER = "表のファイル (*.xlsx *.csv *.txt *.tsv);;すべてのファイル (*)"


class ImportPage(QWidget):
    changed = Signal()
    next_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session: Session | None = None
        self.rows: list[TableRow] = []
        self.setAcceptDrops(True)

        add = QPushButton("ファイルを追加…")
        add.clicked.connect(self._add_files)
        remove = QPushButton("作業から外す")
        remove.clicked.connect(self._remove_file)
        hint = QLabel("ファイルをここにドラッグ＆ドロップしても追加できます。表の定義は見出しで照合します"
                      "（ファイル名は使いません）。")
        hint.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(add)
        top.addWidget(remove)
        top.addWidget(hint, 1)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["ファイル", "シート", "見出し行", "データ範囲", "表の定義", "適用状態", "注意"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selected)

        # 選んだ表の操作
        self.template_combo = QComboBox()
        apply_btn = QPushButton("この定義を適用")
        apply_btn.clicked.connect(self._apply_template)
        self.new_name = QLineEdit()
        self.new_header = QSpinBox()
        self.new_header.setMinimum(1)
        self.new_header.setMaximum(1000)
        create_btn = QPushButton("新しい表の定義を作る")
        create_btn.clicked.connect(self._create_template)
        self.adapt_label = QLabel()
        self.adapt_label.setWordWrap(True)
        self.adapt_btn = QPushButton("既存の定義を見出しに合わせて更新")
        self.adapt_btn.clicked.connect(self._adapt_template)
        self.header_spin = QSpinBox()
        self.header_spin.setMinimum(1)
        self.header_spin.setMaximum(1000)
        self.skip_total = QCheckBox("合計行らしい行（「合計」「計」等）をデータに含めない")
        self.outside_combo = QComboBox()
        for key, label in OUTSIDE.items():
            self.outside_combo.addItem(label, key)
        region_btn = QPushButton("範囲を反映")
        region_btn.clicked.connect(self._apply_region)
        self.opt_comments = QCheckBox("コメントを除去して出力")
        self.opt_charts = QCheckBox("グラフを除去して出力")
        self.opt_images = QCheckBox("画像の中身は検査されないことを確認した")
        self.opt_formulas = QCheckBox("自動で置換できない数式を値に変換して出力")
        for box in (self.opt_comments, self.opt_charts, self.opt_images, self.opt_formulas):
            box.toggled.connect(self._options_changed)

        assign_box = QGroupBox("表の定義")
        f1 = QFormLayout(assign_box)
        row1 = QHBoxLayout()
        row1.addWidget(self.template_combo, 1)
        row1.addWidget(apply_btn)
        f1.addRow("既存の定義", row1)
        f1.addRow(self.adapt_label)
        f1.addRow(self.adapt_btn)
        row2 = QHBoxLayout()
        row2.addWidget(self.new_name, 1)
        row2.addWidget(QLabel("見出し行"))
        row2.addWidget(self.new_header)
        f1.addRow("新しい定義の名前", row2)
        f1.addRow(create_btn)
        region_box = QGroupBox("表の範囲（適用した定義に保存。同じ定義の全ファイルに適用）")
        f2 = QFormLayout(region_box)
        f2.addRow("見出し行", self.header_spin)
        f2.addRow(self.skip_total)
        f2.addRow("表外（見出しより上・合計行）", self.outside_combo)
        f2.addRow(region_btn)
        self.options_box = QGroupBox("Excel ファイルの扱い")
        f3 = QVBoxLayout(self.options_box)
        for box in (self.opt_comments, self.opt_charts, self.opt_images, self.opt_formulas):
            f3.addWidget(box)
        side = QVBoxLayout()
        side.addWidget(assign_box)
        side.addWidget(region_box)
        side.addWidget(self.options_box)
        side.addStretch()
        side_widget = QWidget()
        side_widget.setLayout(side)

        self.preview_model = GridModel(self)
        self.preview = QTableView()
        self.preview.setModel(self.preview_model)
        legend = QLabel("表示：青＝見出し行、灰＝表外（タイトル・注記・除いた合計行）")

        upper = QSplitter(Qt.Orientation.Horizontal)
        upper.addWidget(self.table)
        upper.addWidget(side_widget)
        upper.setStretchFactor(0, 3)
        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.addWidget(legend)
        lower_layout.addWidget(self.preview)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(upper)
        split.addWidget(lower)
        split.setSizes([420, 300])

        self.next_btn = QPushButton("③ 変換設定へ進む")
        self.next_btn.clicked.connect(self.next_requested.emit)
        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(self.next_btn)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        layout.addLayout(bottom)
        MASK.changed.connect(lambda _: self.refresh())

    # ---------------------------------------------------------------- 表示

    def set_session(self, session: Session | None) -> None:
        self.session = session
        self.refresh()

    def refresh(self) -> None:
        s = self.session
        self.rows = s.tables() if s else []
        current = self.table.currentRow()
        self.table.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            data_range = "―"
            if row.data_first is not None:
                data_range = (f"{row.data_first + 1:,}〜{row.data_last + 1:,}行目、"
                              f"A〜{column_letter(max(row.columns - 1, 0))}列")
            sheet = row.sheet_name
            if row.visibility != "visible":
                sheet += "（非表示）"
            values = [MASK.show(row.file_name), MASK.show(sheet), f"{row.header_row + 1}行目", data_range,
                      row.template or "（未割当）", row.status, "、".join(row.notes)]
            for j, v in enumerate(values):
                self.table.setItem(i, j, QTableWidgetItem(v))
        self.next_btn.setEnabled(bool(self.rows))
        if self.rows:
            self.table.selectRow(min(max(current, 0), len(self.rows) - 1))
        else:
            self.preview_model.clear()

    def current(self) -> TableRow | None:
        i = self.table.currentRow()
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def _selected(self) -> None:
        row = self.current()
        s = self.session
        if row is None or s is None:
            return
        self.template_combo.clear()
        self.template_combo.addItem("（未割当）", None)
        for name in s.draft.templates:
            self.template_combo.addItem(name, name)
        if row.template:
            self.template_combo.setCurrentIndex(self.template_combo.findData(row.template))
        self.new_name.setText(s.new_template_name())
        self.new_header.setValue(row.header_row + 1)
        if row.suggestion:
            self.adapt_label.setText(f"見出しが「{row.suggestion}」と一部違います（列の追加・名前の変更）。"
                                     "更新すると、変わった列は③で確認が必要になります。")
            self.adapt_btn.setEnabled(True)
        else:
            self.adapt_label.setText("")
            self.adapt_btn.setEnabled(False)
        template = s.draft.templates.get(row.template) if row.template else None
        self.header_spin.setValue(row.header_row + 1)
        self.skip_total.setChecked(bool(template and template.skip_total_rows))
        self.outside_combo.setCurrentIndex(self.outside_combo.findData(
            template.outside_action.value if template else "keep"))
        for w in (self.header_spin, self.skip_total, self.outside_combo):
            w.setEnabled(template is not None)
        is_xlsx = isinstance(s.source(row.file_id), XlsxBook)
        self.options_box.setVisible(is_xlsx)
        if is_xlsx:
            options = next((f.options for f in s.wb.run_files(s.run_id) if f.file_id == row.file_id), {})
            for box, key in self._option_boxes():
                box.blockSignals(True)
                box.setChecked(bool(options.get(key)))
                box.blockSignals(False)
        self._show_preview(row, template)

    def _show_preview(self, row: TableRow, template) -> None:
        rows = self.session.rows(row.file_id, row.sheet)
        data = set(template.data_rows(rows)) if template else set(range(row.header_row + 1, len(rows)))
        width = max((len(r) for r in rows), default=0)

        def cell(r: int, c: int) -> str:
            values = rows[r]
            return values[c] if c < len(values) else ""

        def background(r: int, c: int):
            if r == row.header_row:
                return HEADER_BG
            if r not in data:
                return OUTSIDE_BG
            return None

        self.preview_model.set_data(list(range(len(rows))), width, cell, background)

    # ---------------------------------------------------------------- 操作

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        self.add_paths([p for p in paths if p.is_file()])

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "取り込むファイル", "", FILE_FILTER)
        if files:
            self.add_paths([Path(f) for f in files])

    def add_paths(self, paths: list[Path]) -> None:
        if not self.session or not paths:
            return
        results = run_safely(self, self.session.import_paths, paths) or []
        failed = [r for r in results if r.error]
        if failed:
            lines = "\n".join(f"・{MASK.show(r.path.name)}：{r.error}" for r in failed)
            QMessageBox.warning(self, "取り込めないファイル", f"次のファイルは取り込めませんでした。\n{lines}")
        self._after_change()

    def _remove_file(self) -> None:
        row = self.current()
        if row and self.session:
            run_safely(self, self.session.remove_file, row.file_id)
            self._after_change()

    def _apply_template(self) -> None:
        row = self.current()
        if row and self.session:
            run_safely(self, self.session.assign, row.file_id, row.sheet, self.template_combo.currentData())
            self._after_change()

    def _create_template(self) -> None:
        row = self.current()
        if row and self.session:
            run_safely(self, self.session.create_template, row.file_id, row.sheet, self.new_name.text(),
                       self.new_header.value() - 1)
            self._after_change()

    def _adapt_template(self) -> None:
        row = self.current()
        if row and self.session and row.suggestion:
            run_safely(self, self.session.adapt_template, row.file_id, row.sheet, row.suggestion)
            self._after_change()

    def _apply_region(self) -> None:
        row = self.current()
        if row and row.template and self.session:
            run_safely(self, self.session.set_region, row.template, self.header_spin.value() - 1,
                       self.skip_total.isChecked(), OutsideAction(self.outside_combo.currentData()))
            self._after_change()

    def _option_boxes(self):
        return [(self.opt_comments, "remove_comments"), (self.opt_charts, "remove_charts"),
                (self.opt_images, "accept_images"), (self.opt_formulas, "formulas_to_values")]

    def _options_changed(self) -> None:
        row = self.current()
        if row and self.session:
            options = BookOptions(**{key: box.isChecked() for box, key in self._option_boxes()})
            run_safely(self, self.session.set_options, row.file_id, options)
            self.changed.emit()

    def _after_change(self) -> None:
        self.refresh()
        self.changed.emit()
