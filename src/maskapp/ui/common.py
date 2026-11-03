"""画面の共通部品：元の値を伏せる切替、仮想表示の表、処理中表示、エラー表示。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from maskapp.infra.safelog import get_logger
from maskapp.ui.labels import MASKED_TEXT

_log = get_logger("ui")
_NO_PARENT = QModelIndex()

HEADER_BG = QColor("#dbe8ff")
OUTSIDE_BG = QColor("#eeeeee")
CHANGED_BG = QColor("#fff2b3")
CLEARED_BG = QColor("#f6d6d6")


class MaskState(QObject):
    """「元の値を伏せる」の状態。画面共有のときに使う（設計書 04 章 0）。"""

    changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.masked = False

    def set(self, masked: bool) -> None:
        if masked != self.masked:
            self.masked = masked
            self.changed.emit(masked)

    def show(self, text: str) -> str:
        return MASKED_TEXT if self.masked and text else text


MASK = MaskState()


class GridModel(QAbstractTableModel):
    """大きな表を仮想表示する（画面に見えている範囲のセルだけを描く）。

    rows: 表示する行（元の行番号の一覧。絞り込み用）、cell(r, c) で値を返す。
    original=True の表は「元の値を伏せる」の対象にする。
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[int] = []
        self._cols = 0
        self._cell: Callable[[int, int], str] = lambda r, c: ""
        self._background: Callable[[int, int], QColor | None] = lambda r, c: None
        self._original: Callable[[int, int], bool] = lambda r, c: True
        self._headers: list[str] = []
        MASK.changed.connect(lambda _: self._refresh())

    def set_data(self, rows: list[int], cols: int, cell, background=None, original=None,
                 headers: list[str] | None = None) -> None:
        self.beginResetModel()
        self._rows, self._cols, self._cell = rows, cols, cell
        self._background = background or (lambda r, c: None)
        self._original = original or (lambda r, c: True)
        self._headers = headers or []
        self.endResetModel()

    def clear(self) -> None:
        self.set_data([], 0, lambda r, c: "")

    def _refresh(self) -> None:
        if self._rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, max(self._cols - 1, 0)))

    def source_row(self, row: int) -> int:
        return self._rows[row]

    def rowCount(self, parent=_NO_PARENT) -> int:  # noqa: N802 - Qt の名前
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=_NO_PARENT) -> int:  # noqa: N802
        return 0 if parent.isValid() else self._cols

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r, c = self._rows[index.row()], index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            text = self._cell(r, c)
            return MASK.show(text) if self._original(r, c) else text
        if role == Qt.ItemDataRole.BackgroundRole:
            color = self._background(r, c)
            return QBrush(color) if color else None
        return None

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Vertical:
            return str(self._rows[section] + 1) if section < len(self._rows) else None
        if section < len(self._headers):
            return self._headers[section]
        from maskapp.app.session import column_letter

        return column_letter(section)


@contextmanager
def busy() -> Iterator[None]:
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    QApplication.processEvents()
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


# 例外の英語の説明（値を含まない）→ 画面の文
_ERROR_TEXT = {
    "another conversion is in progress": "別の変換作業が進行中です。",
    "some cells are unresolved": "判断が済んでいないセルがあります。④で判断してください。",
    "export folder must be outside the project folder": "出力先にプロジェクトフォルダの中は指定できません。",
    "export folder must differ from the folder of the original files": "出力先に元のファイルと同じフォルダは指定できません。",
    "a file with the same name already exists": "出力先に同じ名前のファイルがあります（上書きしません）。",
    "invalid output file name": "出力ファイル名に使えない文字があります。",
    "output file names must be unique": "出力ファイル名が重複しています。",
    "a folder with this project name already exists": "同じ名前のフォルダが既にあります。",
    "project name": "プロジェクト名に使えない文字があるか、長すぎます。",
    "project is open in another window": "このプロジェクトは別の画面・アプリで開かれています。",
    "configuration is incomplete": "変換設定に未確認・不足の項目があります。",
    "does not match its table definition": "表の定義とファイルの見出しが一致しません。②で表の定義を更新してください。",
    "free-text processing is not available yet": "「文章内を検査」は今後の版で対応します。別の処理を選んでください。",
    "a table definition with this name already exists": "同じ名前の表の定義が既にあります。",
    "only confirmed entities can be edited": "確定済みの対象だけ変更できます。",
    "another entity already uses this masked name": "その変換後名称は別の対象で使われています。",
    "the masked name must not be an original name": "元の名称と同じ変換後名称にはできません。",
    "cannot remove the last name of an entity": "対象の最後の名称は削除できません。",
    "entities of different types cannot be merged": "種類の違う対象は統合できません。",
    "staged output changed after verification": "検証後に出力ファイルが変わりました。もう一度検証してください。",
}


def error_text(exc: BaseException) -> str:
    message = str(exc)
    for key, text in _ERROR_TEXT.items():
        if key in message:
            return text
    return f"処理を完了できませんでした（{type(exc).__name__}）。"


def show_error(parent: QWidget, exc: BaseException) -> None:
    _log.exception("ui_operation_failed", exc)
    QMessageBox.warning(parent, "処理できませんでした", error_text(exc))


def run_safely(parent: QWidget, func: Callable, *args, **kwargs):
    """操作を実行し、失敗したら案内を表示する（例外の内容は値を含まない形でログに残す）。"""
    try:
        with busy():
            return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 画面では全ての失敗を案内に変える
        show_error(parent, exc)
        return None
