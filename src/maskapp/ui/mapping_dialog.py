"""補助画面：対応表の管理（設計書 04 章 7）。

元の情報を含む画面。外部出力フォルダへの書き出しは行わない。
変更は過去の出力の表記と食い違い得るため、実行前に影響する出力の一覧を示して確認を求める。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from maskapp.app.session import Session
from maskapp.entity import admin
from maskapp.ui.common import MASK, run_safely


class MappingDialog(QDialog):
    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("対応表の管理")
        self.resize(1000, 640)
        self.rows: list[admin.EntityRow] = []
        warning = QLabel("元の情報を含む画面です。外部への共有（画面共有・印刷等）に使わないでください。")
        warning.setStyleSheet("color: #b00000; font-weight: bold;")
        self.query = QLineEdit()
        self.query.setPlaceholderText("元の名称・別名・元のコード・変換後の名称・変換後のコードで検索")
        self.query.returnPressed.connect(self.search)
        self.type_combo = QComboBox()
        self.type_combo.addItem("すべての種類", None)
        for key, label in session.project.conn.execute("SELECT id, display_name FROM entity_type ORDER BY rowid"):
            self.type_combo.addItem(label, key)
        find = QPushButton("検索")
        find.clicked.connect(self.search)
        self.results = QTableWidget(0, 4)
        self.results.setHorizontalHeaderLabels(["種類", "変換後の名称", "元の名称・別名", "コード（元→変換後）"])
        self.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.results.horizontalHeader().setStretchLastSection(True)
        self.results.itemSelectionChanged.connect(self._selected)
        self.names = QListWidget()
        self.outputs = QListWidget()
        self.locked = QLabel()
        self.locked.setWordWrap(True)
        buttons = QHBoxLayout()
        self.action_buttons = []
        for text, slot in (("別名を追加…", self._add_alias), ("選んだ別名を削除", self._remove_alias),
                           ("変換後の名称を変更…", self._rename), ("別の対象に統合…", self._merge),
                           ("取消（欠番化）…", self._void)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
            self.action_buttons.append(b)
        buttons.addStretch()
        search_row = QHBoxLayout()
        search_row.addWidget(self.query, 1)
        search_row.addWidget(self.type_combo)
        search_row.addWidget(find)
        detail = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("名称・別名"))
        left.addWidget(self.names)
        right = QVBoxLayout()
        right.addWidget(QLabel("変更すると表記が食い違い得る出力（この対象を確定した作業以降）"))
        right.addWidget(self.outputs)
        detail.addLayout(left)
        detail.addLayout(right)
        layout = QVBoxLayout(self)
        layout.addWidget(warning)
        layout.addLayout(search_row)
        layout.addWidget(self.results, 2)
        layout.addLayout(detail, 1)
        layout.addWidget(self.locked)
        layout.addLayout(buttons)
        editable = session.run_id is None
        for b in self.action_buttons:
            b.setEnabled(editable)
        self.locked.setText("" if editable else
                            "変換作業の途中は対応表を変更できません（検索・表示のみ）。作業を出力または取消してから変更してください。")
        self.search()

    def search(self) -> None:
        self.rows = admin.search(self.session.project.conn, self.query.text().strip(), self.type_combo.currentData())
        types = dict(self.session.project.conn.execute("SELECT id, display_name FROM entity_type"))
        self.results.setRowCount(len(self.rows))
        for i, e in enumerate(self.rows):
            names = "、".join(MASK.show(n) for n, _ in e.names)
            codes = "、".join(f"{MASK.show(o)}→{m}" for _, o, m in e.codes)
            for j, v in enumerate([types.get(e.entity_type, e.entity_type), e.masked_name, names, codes]):
                self.results.setItem(i, j, QTableWidgetItem(v))

    def current(self) -> admin.EntityRow | None:
        i = self.results.currentRow()
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def _selected(self) -> None:
        e = self.current()
        self.names.clear()
        self.outputs.clear()
        if e is None:
            return
        for name, kind in e.names:
            self.names.addItem(f"{MASK.show(name)}（{'正式名' if kind == 'primary' else '別名'}）")
        for o in admin.affected_outputs(self.session.project.conn, e.id):
            self.outputs.addItem(f"作業{o.run_id}：{o.filename}" + ("" if o.published_at else "（未公開）"))

    def _confirm(self, what: str) -> bool:
        n = self.outputs.count()
        text = f"{what}\n\n過去に出力したファイル {n} 件の表記と食い違うことがあります。続けますか？" if n else \
            f"{what}\n\n続けますか？"
        return QMessageBox.question(self, "対応表の変更", text) == QMessageBox.StandardButton.Yes

    def _apply(self, func, *args) -> None:
        def work():
            with self.session.project.transaction() as conn:
                func(conn, *args)

        run_safely(self, work)
        self.search()

    def _add_alias(self) -> None:
        e = self.current()
        if not e:
            return
        name, ok = QInputDialog.getText(self, "別名を追加", "追加する別名（元の表記）：")
        if ok and name.strip() and self._confirm("別名を追加します。"):
            self._apply(admin.add_alias, e.id, name)

    def _remove_alias(self) -> None:
        e = self.current()
        i = self.names.currentRow()
        if e and 0 <= i < len(e.names) and self._confirm("選んだ名称を削除します。"):
            self._apply(admin.remove_alias, e.id, e.names[i][0])

    def _rename(self) -> None:
        e = self.current()
        if not e:
            return
        name, ok = QInputDialog.getText(self, "変換後の名称を変更", "新しい変換後の名称：", text=e.masked_name)
        if ok and name.strip() and self._confirm(f"変換後の名称を「{e.masked_name}」から「{name.strip()}」に変えます。"):
            self._apply(admin.rename_masked, e.id, name)

    def _merge(self) -> None:
        e = self.current()
        if not e:
            return
        target, ok = QInputDialog.getText(self, "別の対象に統合", "統合先の変換後の名称：")
        if not ok or not target.strip():
            return
        found = [r for r in admin.search(self.session.project.conn, target.strip(), e.entity_type)
                 if r.masked_name == target.strip()]
        if not found:
            QMessageBox.information(self, "統合", "統合先が見つかりません。")
            return
        if self._confirm(f"「{e.masked_name}」を「{target.strip()}」に統合します。名称・コードは統合先のものになります。"):
            self._apply(admin.merge, e.id, found[0].id)

    def _void(self) -> None:
        e = self.current()
        if e and self._confirm(f"「{e.masked_name}」を取り消します（番号は欠番）。次の作業で同じ値が現れると新しい対象になります。"):
            self._apply(admin.void, e.id)
