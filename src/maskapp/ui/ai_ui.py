"""AI（端末内の LLM）の画面部品：設定、別スレッドでの実行と進捗表示（設計書 04 章 12）。

AI への問い合わせだけを別スレッドで行う。project.db への保存は画面のスレッドで行う（接続を共有しない）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEventLoop, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QWidget,
)

from maskapp.llm import LLMClient, LLMSettings, LLMUnavailable
from maskapp.llm.settings import DEVICES

DEVICE_LABELS = {"GPU": "GPU（内蔵グラフィックス・推奨）", "NPU": "NPU（AI 専用の処理装置）", "CPU": "CPU"}


class Worker(QThread):
    """func(emit, should_stop) を別スレッドで動かす。emit した値は item シグナルで画面のスレッドに届く。"""

    item = Signal(object)
    failed = Signal(str)

    def __init__(self, func: Callable, parent=None) -> None:
        super().__init__(parent)
        self.func = func
        self.stop_requested = False
        self.result = None

    def run(self) -> None:
        try:
            self.result = self.func(self.item.emit, lambda: self.stop_requested)
        except LLMUnavailable:
            self.failed.emit("unavailable")
        except Exception as exc:  # noqa: BLE001 - 型名だけを画面に渡す
            self.failed.emit(type(exc).__name__)


def run_in_background(parent: QWidget, title: str, func: Callable, total: int = 0,
                      on_item: Callable[[object], int] | None = None) -> tuple[object, str | None]:
    """進捗を表示しながら別スレッドで実行し、終わるまで待つ。(結果, 失敗の種類) を返す。

    on_item は届いた値を画面のスレッドで処理し、進んだ件数を返す。
    """
    dialog = QProgressDialog(title, "中止", 0, max(total, 0), parent)
    dialog.setWindowTitle("AI")
    dialog.setMinimumDuration(0)
    dialog.setValue(0)
    worker = Worker(func)
    done = {"count": 0, "error": None}
    loop = QEventLoop()

    def handle(value) -> None:
        if on_item:
            done["count"] += on_item(value)
            dialog.setValue(min(done["count"], total) if total else 0)

    def failed(kind: str) -> None:
        done["error"] = kind

    worker.item.connect(handle)
    worker.failed.connect(failed)
    worker.finished.connect(loop.quit)
    dialog.canceled.connect(lambda: setattr(worker, "stop_requested", True))
    worker.start()
    loop.exec()
    worker.wait()
    dialog.close()
    return worker.result, done["error"]


class AiSettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI の設定")
        s = LLMSettings.load()
        self.enabled = QCheckBox("AI による候補の提案を使う（この PC の中だけで動きます。外部へは送りません）")
        self.enabled.setChecked(s.enabled)
        self.model = QLineEdit(s.model_dir)
        browse = QPushButton("選ぶ…")
        browse.clicked.connect(self._browse)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model, 1)
        model_row.addWidget(browse)
        self.device = QComboBox()
        for d in DEVICES:
            self.device.addItem(DEVICE_LABELS[d], d)
        self.device.setCurrentIndex(max(0, self.device.findData(s.device)))
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(10, 1800)
        self.timeout.setValue(s.timeout_s)
        self.timeout.setSuffix(" 秒")
        self.batch = QSpinBox()
        self.batch.setRange(1, 50)
        self.batch.setValue(s.batch_size)
        self.result = QLabel()
        self.result.setWordWrap(True)
        test = QPushButton("動作を確認する")
        test.clicked.connect(self._test)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("やめる")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.addRow(self.enabled)
        form.addRow("モデルのフォルダ（OpenVINO 形式）", model_row)
        form.addRow("処理装置", self.device)
        form.addRow("1回の問い合わせの上限時間", self.timeout)
        form.addRow("1回に問い合わせる文章の数", self.batch)
        form.addRow(test, self.result)
        form.addRow(QLabel("AI の出力は候補としてだけ使い、必ず人が確認します。AI が止まっても、規則と辞書だけで処理を続けます。"))
        form.addRow(buttons)

    def settings(self) -> LLMSettings:
        return LLMSettings(enabled=self.enabled.isChecked(), model_dir=self.model.text().strip(),
                           device=self.device.currentData(), timeout_s=self.timeout.value(),
                           batch_size=self.batch.value())

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "モデルのフォルダ", self.model.text())
        if folder:
            self.model.setText(folder)

    def _test(self) -> None:
        settings = self.settings()
        if not settings.usable:
            self.result.setText("「AI による候補の提案を使う」を選び、モデルのフォルダを指定してください。")
            return
        if not (Path(settings.model_dir) / "openvino_model.xml").exists():
            self.result.setText("指定したフォルダに OpenVINO 形式のモデル（openvino_model.xml）が見つかりません。")
            return

        def work(emit, should_stop):
            start = time.time()
            with LLMClient(settings) as client:
                answer = client.generate("「OK」とだけ答えてください。", "確認", max_new_tokens=8)
            return answer, time.time() - start

        result, error = run_in_background(self, "モデルを読み込んでいます（初回は数分かかることがあります）", work)
        if error or result is None or result[0] is None:
            self.result.setText("モデルを使えませんでした。処理装置を変えるか、モデルのフォルダを確かめてください。")
        else:
            self.result.setText(f"動作を確認しました（読込と応答に {result[1]:.0f} 秒）。")

    def _save(self) -> None:
        self.settings().save()
        self.accept()


def require_settings(parent: QWidget) -> LLMSettings | None:
    settings = LLMSettings.load()
    if not settings.usable:
        QMessageBox.information(parent, "AI", "メニュー「AI」→「AI の設定」で、AI を使う設定とモデルのフォルダを指定してください。")
        return None
    return settings
