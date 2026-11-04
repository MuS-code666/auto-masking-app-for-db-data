"""画面1：プロジェクト一覧（設計書 04 章 2）。"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from maskapp.app.registry import Registry
from maskapp.project.store import delete_project_folder

PRESET_COLORS = ["#3366cc", "#2e8b57", "#d2691e", "#8b008b", "#b22222", "#008b8b", "#696969"]


def default_parent() -> Path:
    return Path.home() / "Documents" / "MaskApp"


class NewProjectDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新しいプロジェクト")
        self.name = QLineEdit()
        self.folder = QLineEdit(str(default_parent()))
        browse = QPushButton("選ぶ…")
        browse.clicked.connect(self._browse)
        self.color = QComboBox()
        for c in PRESET_COLORS:
            self.color.addItem("■ " + c, c)
            self.color.setItemData(self.color.count() - 1, QColor(c), 9)  # ForegroundRole
        self.color.addItem("その他の色…", None)
        self.color.activated.connect(self._pick_color)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder)
        folder_row.addWidget(browse)
        form = QFormLayout()
        form.addRow("プロジェクト名", self.name)
        form.addRow("保存先フォルダ", folder_row)
        form.addRow("識別色", self.color)
        note = QLabel("プロジェクトフォルダには元の値（対応表・原本の複製）が保存されます。"
                      "フォルダはあなただけが開けるアクセス権で作成します。")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "保存先フォルダ", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def _pick_color(self, index: int) -> None:
        if self.color.itemData(index) is None:
            color = QColorDialog.getColor(parent=self)
            if color.isValid():
                self.color.insertItem(index, "■ " + color.name(), color.name())
                self.color.setCurrentIndex(index)

    def values(self) -> tuple[str, Path, str | None]:
        return self.name.text().strip(), Path(self.folder.text().strip()), self.color.currentData()


class ProjectsPage(QWidget):
    open_requested = Signal(Path)
    create_requested = Signal(str, Path, object)

    def __init__(self, registry: Registry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.registry = registry
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["プロジェクト名", "識別色", "最終更新（UTC）", "ファイル数",
                                              "対応表件数", "作業状況", "保存先"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(lambda _: self._open())

        buttons = QHBoxLayout()
        for text, slot in (("新規作成…", self._create), ("開く", self._open), ("一覧に追加…", self._add_existing),
                           ("フォルダを開く", self._open_folder), ("一覧から外す", self._remove),
                           ("削除…", self._delete)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch()
        title = QLabel("プロジェクトを選んでください。同時に開けるプロジェクトは1つです。")
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        summaries = self.registry.summaries()
        self.table.setRowCount(len(summaries))
        for i, s in enumerate(summaries):
            mappings = "／".join(f"{k} {v}" for k, v in s.mappings.items() if v)
            status = {"missing": "フォルダが見つかりません", "unreadable": "読み取れません"}.get(s.error or "", s.status)
            values = [s.name or s.path.name, "", s.updated_at[:16].replace("T", " "), str(s.files), mappings,
                      status, str(s.path)]
            for j, v in enumerate(values):
                item = QTableWidgetItem(v)
                if j == 1 and s.color:
                    item.setBackground(QColor(s.color))  # 色見本（文字なし）
                self.table.setItem(i, j, item)
            self.table.item(i, 0).setData(256, str(s.path))  # UserRole

    def selected_path(self) -> Path | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return Path(self.table.item(rows[0].row(), 0).data(256))

    def _create(self) -> None:
        dialog = NewProjectDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            name, folder, color = dialog.values()
            self.create_requested.emit(name, folder, color)

    def _open(self) -> None:
        path = self.selected_path()
        if path:
            self.open_requested.emit(path)

    def _add_existing(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "プロジェクトフォルダ")
        if folder:
            if not (Path(folder) / "project.db").is_file():
                QMessageBox.warning(self, "一覧に追加", "プロジェクトフォルダではありません。")
                return
            self.registry.add(Path(folder))
            self.refresh()

    def _open_folder(self) -> None:
        path = self.selected_path()
        if path and path.is_dir() and hasattr(os, "startfile"):
            os.startfile(path)  # noqa: S606 - エクスプローラーで開く

    def _remove(self) -> None:
        path = self.selected_path()
        if path:
            self.registry.remove(path)
            self.refresh()

    def _delete(self) -> None:
        path = self.selected_path()
        if not path:
            return
        first = QMessageBox.question(
            self, "プロジェクトの削除",
            "プロジェクトフォルダ（対応表・原本の複製・バックアップ）をすべて削除します。元に戻せません。続けますか？")
        if first != QMessageBox.StandardButton.Yes:
            return
        name, ok = QInputDialog.getText(self, "プロジェクトの削除（確認）",
                                        "確認のため、フォルダ名を入力してください：")
        if not ok or name.strip() != path.name:
            QMessageBox.information(self, "プロジェクトの削除", "名前が一致しないため削除しませんでした。")
            return
        try:
            delete_project_folder(path)
        except Exception:  # noqa: BLE001
            QMessageBox.warning(self, "プロジェクトの削除", "削除できませんでした（開かれている可能性があります）。")
            return
        self.registry.remove(path)
        self.refresh()
