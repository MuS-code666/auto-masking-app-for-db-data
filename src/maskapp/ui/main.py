"""アプリの起動：python -m maskapp"""

from __future__ import annotations

import sys

from maskapp.infra.netguard import install_network_guard
from maskapp.infra.paths import log_dir
from maskapp.infra.safelog import configure_logging, get_logger


def run() -> int:
    install_network_guard()  # 業務データを扱うプロセスは最初に外部通信を遮断する
    configure_logging(log_dir())
    from PySide6.QtWidgets import QApplication

    from maskapp.ui.window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("MaskApp")
    window = MainWindow()
    window.show()
    get_logger("ui").event("app_started")
    return app.exec()


if __name__ == "__main__":
    sys.exit(run())
