"""画面5：検証・出力（設計書 04 章 6）。「完全匿名化済み」「安全」等の表示はしない。"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from maskapp.app.session import Session
from maskapp.app.workbench import RunState
from maskapp.ui.common import MASK, run_safely
from maskapp.ui.labels import ORIGINS, SEVERITIES, finding_label

CHECKED = "構造（行数・列数・シート構成）、維持した値、置換の一貫性、内容削除、ファイル間の関連、元の語句の残り" \
          "（セル・数式・シート名・コメント等の全部品）、出力ファイル名、Excel ファイルの健全性"
NOT_CHECKED = "画像・バイナリ部品の中身、登録・検出されていない語句、言い換え・略称・ローマ字等の表記"


def location_text(loc: dict, file_names: dict[int, str]) -> str:
    parts = []
    if "file_id" in loc:
        parts.append(MASK.show(file_names.get(loc["file_id"], "")))
    if "sheet" in loc:
        parts.append(f"シート{loc['sheet'] + 1}")
    row = loc.get("record", loc.get("row"))
    if row is not None:
        parts.append(f"{row + 1}行目")
    if "column" in loc:
        from maskapp.app.session import column_letter

        parts.append(f"{column_letter(loc['column'])}列")
    if "part" in loc:
        parts.append({"formula": "数式", "filename": "ファイル名"}.get(loc["part"], loc["part"]))
    return "・".join(p for p in parts if p)


class OutputPage(QWidget):
    changed = Signal()
    back_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session: Session | None = None
        self.names = QTableWidget(0, 2)
        self.names.setHorizontalHeaderLabels(["元のファイル", "出力ファイル名"])
        self.names.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        verify = QPushButton("書き出して検証する")
        verify.clicked.connect(self._verify)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["項目", "件数・場所"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.relations = QTableWidget(0, 5)
        self.relations.setHorizontalHeaderLabels(["関連", "一致（前）", "一致（後）", "元からの未一致", "結合後の行数"])
        self.relations.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.relations.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.relations.setMaximumHeight(140)
        self.folder = QLineEdit()
        browse = QPushButton("出力先を選ぶ…")
        browse.clicked.connect(self._browse)
        self.publish_btn = QPushButton("対応表を確定して出力する")
        self.publish_btn.clicked.connect(self._publish)
        cancel = QPushButton("この作業を取り消す…")
        cancel.clicked.connect(self._cancel)
        back = QPushButton("④ 変換前後の確認に戻る")
        back.clicked.connect(lambda: self.back_requested.emit(4))

        head = QHBoxLayout()
        head.addWidget(verify)
        head.addWidget(self.status, 1)
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("出力先フォルダ"))
        out_row.addWidget(self.folder, 1)
        out_row.addWidget(browse)
        out_row.addWidget(self.publish_btn)
        bottom = QHBoxLayout()
        bottom.addWidget(back)
        bottom.addWidget(cancel)
        bottom.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("出力ファイル名（元のファイル名を使うと、名前に含まれる語句も検査します）"))
        layout.addWidget(self.names)
        layout.addLayout(head)
        layout.addWidget(self.tree, 1)
        layout.addWidget(QLabel("ファイル間の関連（変換前後の比較）"))
        layout.addWidget(self.relations)
        layout.addLayout(out_row)
        layout.addLayout(bottom)
        MASK.changed.connect(lambda _: self.refresh())

    def set_session(self, session: Session | None) -> None:
        self.session = session
        self.tree.clear()
        self.relations.setRowCount(0)
        self.refresh()

    def _file_names(self) -> dict[int, str]:
        s = self.session
        return {f.file_id: s.wb.file_info(f.file_id)["name"] for f in s.wb.run_files(s.run_id)} if s and s.run_id else {}

    def refresh(self) -> None:
        s = self.session
        run = s.run_id if s else None
        state = s.wb.state(run) if run else None
        names = self._file_names()
        staged = {}
        if run:
            staged = dict(s.project.conn.execute(
                "SELECT source_file_id, output_filename FROM run_output WHERE run_id = ?", (run,)).fetchall())
        self.names.setRowCount(len(names))
        for i, (fid, name) in enumerate(names.items()):
            item = QTableWidgetItem(MASK.show(name))
            item.setData(256, fid)
            self.names.setItem(i, 0, item)
            current = self.names.item(i, 1)
            if fid in staged or current is None:
                self.names.setItem(i, 1, QTableWidgetItem(staged.get(fid, "")))
        self.publish_btn.setEnabled(state in (RunState.VERIFIED, RunState.COMMITTED))
        if state is RunState.COMMITTED:
            self.status.setText("対応表は確定済みです。出力に失敗した場合は、出力先を確かめて再度出力できます。")
        elif state is RunState.VERIFIED:
            self.status.setText("検証に合格しました。出力先を選んで出力してください。")
        elif state is RunState.PLANNED:
            self.status.setText("「書き出して検証する」を押してください。")
        elif state is None:
            self.status.setText("進行中の作業はありません。")

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "出力先フォルダ", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def _output_names(self) -> dict[int, str]:
        out = {}
        for i in range(self.names.rowCount()):
            fid = self.names.item(i, 0).data(256)
            text = self.names.item(i, 1).text().strip() if self.names.item(i, 1) else ""
            if text:
                out[fid] = text
        return out

    def verify(self):
        """書出し・検証。結果を表示し、レポートを返す（試験からも使う）。"""
        s = self.session
        if not s or not s.run_id:
            return None
        run = s.run_id
        if s.wb.state(run) in (RunState.STAGED, RunState.VERIFIED):
            s.wb.back_to_draft(run)
            s.wb.plan(run)
        if run_safely(self, s.wb.stage, run, self._output_names()) is None:
            return None
        report = run_safely(self, s.wb.verify, run)
        if report is None:
            return None
        self._show(report)
        self.refresh()
        self.changed.emit()
        return report

    def _verify(self) -> None:
        self.verify()

    def _show(self, report) -> None:
        names = self._file_names()
        self.tree.clear()
        groups: dict[tuple[str, str], dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for f in report.findings:
            # 出力不可の項目は「変換で生じた不備」と「元データ由来」を分けて示す（設計書 04 章 6）
            groups[(f.severity, f.origin if f.severity == "block" else "")][f.kind].append(f)
        order = [("block", "transform"), ("block", "source"), ("block", "environment"), ("warn", ""), ("info", "")]
        for key in order:
            if key not in groups:
                continue
            severity, origin = key
            title = f"【{SEVERITIES[severity]}】{ORIGINS[origin]}" if origin else f"【{SEVERITIES[severity]}】"
            top = QTreeWidgetItem([title, ""])
            self.tree.addTopLevelItem(top)
            for kind, findings in groups[key].items():
                count = sum(f.detail.get("count", 1) if isinstance(f.detail.get("count"), int) else 1
                            for f in findings)
                child = QTreeWidgetItem([finding_label(kind), f"{count:,}件"])
                top.addChild(child)
                for f in findings[:50]:
                    child.addChild(QTreeWidgetItem(["", location_text(f.location, names)]))
            top.setExpanded(severity == "block")
        if not report.findings:
            self.tree.addTopLevelItem(QTreeWidgetItem(["検出事項はありません", ""]))
        self.relations.setRowCount(len(report.relations))
        for i, rel in enumerate(report.relations):
            b, a = rel["before"], rel["after"]
            values = [f"{rel['from']} → {rel['to']}", f"{b['matched_rows']:,}", f"{a['matched_rows']:,}",
                      f"{b['unmatched_rows']:,}", f"{a['join_rows']:,}"]
            for j, v in enumerate(values):
                self.relations.setItem(i, j, QTableWidgetItem(v))
        blocking = len(report.blocking)
        if blocking:
            self.status.setText(f"出力できません：出力不可の項目が {blocking} 件あります。"
                                "変換で生じた不備は④・③、元ファイル由来の停止は②（Excel ファイルの扱い）で対処してください。")
        else:
            self.status.setText("検証に合格しました。出力先を選んで出力してください。")

    def publish(self, folder: Path) -> list[Path] | None:
        s = self.session
        if not s or not s.run_id:
            return None
        run = s.run_id
        if s.wb.state(run) is RunState.VERIFIED and run_safely(self, s.wb.commit, run) is None:
            return None
        outputs = run_safely(self, s.wb.publish, run, folder)
        self.refresh()
        self.changed.emit()
        return outputs

    def _publish(self) -> None:
        folder = self.folder.text().strip()
        if not folder:
            QMessageBox.information(self, "出力", "出力先フォルダを選んでください。")
            return
        answer = QMessageBox.question(
            self, "出力",
            "対応表を確定して出力します。確定した対応（変換後の番号）は以後の作業でも同じものを使います。続けますか？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        outputs = self.publish(Path(folder))
        if outputs:
            QMessageBox.information(
                self, "出力しました",
                f"出力しました（{len(outputs)} ファイル）。\n\n検査項目：{CHECKED}\n\n検査の対象外：{NOT_CHECKED}\n\n"
                "名称・コード等の置き換えであり、匿名化を保証するものではありません。")

    def _cancel(self) -> None:
        s = self.session
        if not s or not s.run_id or s.wb.state(s.run_id) is RunState.COMMITTED:
            return
        answer = QMessageBox.question(self, "作業の取消",
                                      "この作業を取り消します。確保した番号は欠番になります。続けますか？")
        if answer == QMessageBox.StandardButton.Yes:
            run_safely(self, s.wb.cancel, s.run_id)
            self.changed.emit()
            self.back_requested.emit(2)
