"""手元の AI モデルでの確認（段階5。開発時に実行する。モデルがない環境では飛ばす）。

  MASKAPP_TEST_MODEL=<モデルのフォルダ> [MASKAPP_TEST_DEVICE=GPU] .venv/Scripts/python -m pytest -m llm

合成の文章での検出率と、出力がすべて検証を通ることを確かめる。精度は合成データでの値である。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from maskapp.app.ai import suggest_columns_by_ai
from maskapp.config import Action, Role
from maskapp.llm import LLMClient, LLMSettings
from tests.llm.bench import run

pytestmark = pytest.mark.llm
MODEL = os.environ.get("MASKAPP_TEST_MODEL", "")
DEVICE = os.environ.get("MASKAPP_TEST_DEVICE", "GPU")


@pytest.fixture(scope="module", autouse=True)
def _model():
    if not MODEL or not (Path(MODEL) / "openvino_model.xml").exists():
        pytest.skip("MASKAPP_TEST_MODEL is not set")


def test_freetext_candidates_on_synthetic_texts():
    result = run(MODEL, DEVICE, batch=10, n=30)
    assert result["failed_texts"] == 0
    assert result["recall"] >= 0.7, result


def test_column_types():
    columns = [("伝票番号", ["S20260400001", "S20260400002"]), ("得意先名", ["株式会社あおば商事", "合同会社ひかり"]),
               ("得意先CD", ["000101", "000108"]), ("金額", ["12000", "3400"]), ("備考", ["前回同様", "至急"])]
    settings = LLMSettings(enabled=True, model_dir=MODEL, device=DEVICE, timeout_s=300)
    with LLMClient(settings) as client:
        specs = suggest_columns_by_ai(client, columns)
    assert specs is not None
    assert specs["得意先名"].role is Role.NAME and specs["得意先CD"].role is Role.CODE
    assert specs["金額"].action is Action.KEEP and specs["備考"].action is Action.FREETEXT
