"""主画面（設計書 04 章 0）：プロジェクト帯・手順バー・各画面・注意文・「元の値を伏せる」。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from maskapp.app.registry import Registry
from maskapp.app.session import Session
from maskapp.app.workbench import RunState
from maskapp.ui.ai_ui import AiSettingsDialog
from maskapp.ui.common import MASK, run_safely
from maskapp.ui.config_page import ConfigPage
from maskapp.ui.import_page import ImportPage
from maskapp.ui.labels import NOTICE, ORIGINAL_SHOWN, STEPS
from maskapp.ui.mapping_dialog import MappingDialog
from maskapp.ui.output_page import OutputPage
from maskapp.ui.projects_page import ProjectsPage
from maskapp.ui.review_page import ReviewPage


class MainWindow(QMainWindow):
    def __init__(self, registry: Registry | None = None) -> None:
        super().__init__()
        self.setWindowTitle("名称・コード置換アプリ")
        self.resize(1280, 820)
        self.registry = registry or Registry()
        self.session: Session | None = None

        # プロジェクト帯
        self.color_bar = QFrame()
        self.color_bar.setFixedWidth(14)
        self.project_label = QLabel("プロジェクト未選択")
        self.project_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.step_label = QLabel()
        self.save_label = QLabel()
        self.folder_note = QLabel()
        self.folder_note.setStyleSheet("color: #8a5a00;")
        band = QHBoxLayout()
        band.addWidget(self.color_bar)
        band.addWidget(self.project_label)
        band.addWidget(self.step_label)
        band.addStretch()
        band.addWidget(self.folder_note)
        band.addWidget(self.save_label)

        # 手順バー
        self.step_buttons: list[QPushButton] = []
        steps = QHBoxLayout()
        for i, text in enumerate(STEPS, start=1):
            b = QPushButton(text)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, n=i: self.go(n))
            self.step_buttons.append(b)
            steps.addWidget(b)

        self.projects = ProjectsPage(self.registry)
        self.imports = ImportPage()
        self.config = ConfigPage()
        self.review = ReviewPage()
        self.output = OutputPage()
        self.stack = QStackedWidget()
        for page in (self.projects, self.imports, self.config, self.review, self.output):
            self.stack.addWidget(page)
        self.projects.open_requested.connect(self.open_project)
        self.projects.create_requested.connect(self.create_project)
        self.imports.next_requested.connect(lambda: self.go(3))
        self.config.next_requested.connect(self.confirm_config)
        self.config.back_requested.connect(lambda: self.go(2))
        self.review.next_requested.connect(lambda: self.go(5))
        self.review.back_requested.connect(lambda: self.go(3))
        self.output.back_requested.connect(self.go)
        for page in (self.imports, self.config, self.review, self.output):
            page.changed.connect(self.update_band)

        # 注意文と「元の値を伏せる」
        notice = QLabel(NOTICE)
        notice.setStyleSheet("color: #444;")
        self.original_label = QLabel(ORIGINAL_SHOWN)
        self.original_label.setStyleSheet("color: #b00000;")
        self.mask_box = QCheckBox("元の値を伏せる（画面共有のとき）")
        self.mask_box.toggled.connect(MASK.set)
        MASK.changed.connect(lambda _: self.update_band())
        footer = QHBoxLayout()
        footer.addWidget(notice)
        footer.addStretch()
        footer.addWidget(self.original_label)
        footer.addWidget(self.mask_box)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(band)
        layout.addLayout(steps)
        layout.addWidget(self.stack, 1)
        layout.addLayout(footer)
        self.setCentralWidget(central)

        menu = self.menuBar().addMenu("プロジェクト")
        for text, slot in (("対応表の管理…", self.open_mapping), ("バックアップから復元…", self.restore_backup),
                           ("プロジェクトを閉じる", self.close_project)):
            action = QAction(text, self)
            action.triggered.connect(slot)
            menu.addAction(action)
        ai_menu = self.menuBar().addMenu("AI")
        ai_settings = QAction("AI の設定…", self)
        ai_settings.triggered.connect(lambda: AiSettingsDialog(self).exec())
        ai_menu.addAction(ai_settings)
        self.go(1)

    # ---------------------------------------------------------------- プロジェクト

    def open_project(self, path: Path) -> None:
        self.close_project()
        session = run_safely(self, Session.open, path, self.registry)
        if session:
            self._attach(session)

    def create_project(self, name: str, folder: Path, color) -> None:
        self.close_project()
        folder.mkdir(parents=True, exist_ok=True)
        session = run_safely(self, Session.create, folder, name, color, self.registry)
        if session:
            if not session.project.protected:
                QMessageBox.warning(self, "プロジェクトフォルダ",
                                    "フォルダのアクセス権を本人のみに設定できませんでした。"
                                    "保存先の場所・権限を確認してください。")
            self._attach(session)

    def _attach(self, session: Session) -> None:
        self.session = session
        for page in (self.imports, self.config, self.review, self.output):
            page.set_session(session)
        self.go(session.step())

    def close_project(self) -> None:
        if self.session:
            self.session.close()
            self.session = None
            for page in (self.imports, self.config, self.review, self.output):
                page.set_session(None)
            self.projects.refresh()
            self.go(1)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.session:
            self.session.close()
        super().closeEvent(event)

    # ---------------------------------------------------------------- 手順

    def allowed_steps(self) -> set[int]:
        s = self.session
        if not s:
            return {1}
        allowed = {1, 2}
        run = s.run_id
        if run and s.wb.run_files(run):
            allowed.add(3)
        state = s.wb.state(run) if run else None
        if state and state is not RunState.DRAFT:
            allowed.add(4)
            if not s.wb.reviews(run) or state is not RunState.PLANNED:
                allowed.add(5)
        return allowed

    def go(self, step: int) -> None:
        allowed = self.allowed_steps()
        if step not in allowed:
            step = max(allowed)
        s = self.session
        if s and s.run_id and step in (2, 3) and s.wb.state(s.run_id) is RunState.COMMITTED:
            QMessageBox.information(self, "作業", "対応表を確定済みの作業があります。⑤で出力を完了してください。")
            step = 5
        self.stack.setCurrentIndex(step - 1)
        page = self.stack.currentWidget()
        if page is self.projects:
            self.projects.refresh()
        elif hasattr(page, "refresh"):
            page.refresh()
        self.current_step = step
        self.update_band()

    def confirm_config(self) -> None:
        if self.session and run_safely(self, self.session.confirm_config) is not None:
            self.review.refresh(reload_units=True)
            self.go(4)

    def update_band(self) -> None:
        s = self.session
        allowed = self.allowed_steps()
        for i, b in enumerate(self.step_buttons, start=1):
            b.setEnabled(i in allowed)
            b.setChecked(i == getattr(self, "current_step", 1))
        if s:
            self.project_label.setText(MASK.show(s.project.name) if MASK.masked else s.project.name)
            self.color_bar.setStyleSheet(f"background: {s.project.color or '#999999'};")
            self.step_label.setText(f"現在の手順：{STEPS[getattr(self, 'current_step', 1) - 1]}")
            self.save_label.setText("保存済み（変更はそのつど保存されます）")
            self.folder_note.setText("機密情報を含むフォルダです")
        else:
            self.project_label.setText("プロジェクト未選択")
            self.color_bar.setStyleSheet("background: transparent;")
            self.step_label.setText("")
            self.save_label.setText("")
            self.folder_note.setText("")
        self.original_label.setVisible(bool(s) and not MASK.masked and getattr(self, "current_step", 1) >= 2)

    # ---------------------------------------------------------------- 補助

    def open_mapping(self) -> None:
        if self.session:
            MappingDialog(self.session, self).exec()
            self.update_band()

    def restore_backup(self) -> None:
        s = self.session
        if not s:
            return
        if s.run_id:
            QMessageBox.information(self, "バックアップから復元", "変換作業の途中は復元できません。作業を出力または取消してください。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "復元するバックアップ", str(s.project.backup_dir),
                                              "バックアップ (project-*.db)")
        if not path:
            return
        answer = QMessageBox.question(self, "バックアップから復元",
                                      "対応表・設定をバックアップの時点に戻します（今の状態も先にバックアップします）。続けますか？")
        if answer == QMessageBox.StandardButton.Yes:
            root = s.project.root
            if run_safely(self, s.project.restore_backup, Path(path)) is not None:
                self.close_project()
                self.open_project(root)
