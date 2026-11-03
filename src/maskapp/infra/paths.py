"""アプリ全体（プロジェクトに属さない）の保存場所。

ここに置くのは値を含まないログと設定だけ。元の値を含むデータはプロジェクトフォルダに置く。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "MASKAPP_HOME"


def app_home() -> Path:
    """アプリ用フォルダ。環境変数 MASKAPP_HOME で差し替え可能（試験用）。"""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "MaskApp"


def log_dir() -> Path:
    return app_home() / "logs"
