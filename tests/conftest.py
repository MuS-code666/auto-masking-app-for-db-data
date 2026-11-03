"""試験全体の共通設定。

- 試験プロセスでは最初に通信遮断を入れる。以後の試験で外部通信が起きれば失敗する
  （段階0 受入条件「ネットワーク通信が発生しないこと」、受入シナリオ S14）。
- アプリ用フォルダ（ログ等）は試験ごとの一時フォルダに向ける。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maskapp.infra.netguard import install_network_guard
from tests.synthetic import Dataset, generate, write_dataset


def pytest_configure(config: pytest.Config) -> None:
    install_network_guard()


@pytest.fixture(autouse=True)
def _isolated_app_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "app_home"
    monkeypatch.setenv("MASKAPP_HOME", str(home))
    return home


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    return generate()


@pytest.fixture(scope="session")
def dataset_dir(dataset: Dataset, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("synthetic")
    write_dataset(dataset, out)
    return out
