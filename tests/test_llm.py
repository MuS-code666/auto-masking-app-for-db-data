"""段階5：AI（端末内の LLM）の補助。試験用のワーカー（backend "fake"）で、正常時と失敗時（S14）を確かめる。

実際のモデルでの計測は tests/llm/（-m llm）で行う。
"""

from __future__ import annotations

import pytest

from maskapp.app import Workbench
from maskapp.app.ai import scan_freetext, suggest_columns_by_ai
from maskapp.config import Action, ColumnSpec, Role, TemplateSpec
from maskapp.formats.csvfile import read_csv
from maskapp.llm import LLMClient, LLMSettings, LLMUnavailable
from maskapp.llm.tasks import validate_freetext
from maskapp.project import Project
from maskapp.transform.reviews import Decision, ReviewKind
from tests.scenarios.setup import add_files, keep, run_through

MEMO = TemplateSpec("連絡メモ", [keep("日付"), ColumnSpec("内容", Action.FREETEXT)])
ROWS = ["4/1,やまびこ運輸へ連絡済み", "4/2,前回同様", "4/3,やまびこ運輸の件、再送"]


def _settings(**kw) -> LLMSettings:
    return LLMSettings(enabled=True, backend="fake", timeout_s=kw.pop("timeout_s", 20.0), **kw)


def _project(tmp_path):
    project = Project.create(tmp_path, "AI")
    wb = Workbench(project)
    wb.save_config([MEMO])
    path = tmp_path / "memo.csv"
    path.write_bytes(("日付,内容\r\n" + "\r\n".join(ROWS) + "\r\n").encode("cp932"))
    run = wb.start_run()
    add_files(wb, run, [path])
    wb.plan(run)
    return project, wb, run


def test_ai_candidates_become_reviews_and_are_reused(tmp_path):
    project, wb, run = _project(tmp_path)
    with project, LLMClient(_settings(), env={"MASKAPP_FAKE_TERMS": "やまびこ運輸"}) as client:
        summary = scan_freetext(wb, run, client)
        assert (summary.total, summary.checked, summary.failed) == (3, 3, 0)
        (item,) = [i for i in wb.reviews(run) if i.kind is ReviewKind.FREETEXT_CANDIDATE]
        # 入力にない語（AI が作った語）は捨て、文章にある語だけが候補になる
        assert item.subject["text"] == "やまびこ運輸" and item.subject["pattern"] == "ai"
        assert item.subject["entity_type"] == "CUSTOMER"
        wb.decide(item.id, Decision.REGISTER_NEW, entity_type="SUPPLIER")
        report, staged = run_through(wb, run)
        assert report.ok, report.blocking
        assert report.by_kind("ai_checked")
        out = read_csv(staged[0].read_bytes()).values()
        assert "やまびこ運輸" not in out[1][1] and out[1][1].endswith("へ連絡済み")
        # 同じ文章は検査済みとして再利用し、問い合わせない
        requests = client.stats["requests"]
        assert scan_freetext(wb, run, client).already == 3 and client.stats["requests"] == requests


@pytest.mark.parametrize("mode,kind", [("hang", "timeouts"), ("crash", "crashes"), ("garbage", None)])
def test_ai_failures_do_not_stop_the_work(tmp_path, mode, kind):
    """S14：AI が止まる・落ちる・不正な出力を返しても、規則と辞書だけで処理を完了できる。"""
    project, wb, run = _project(tmp_path)
    settings = _settings(timeout_s=1.0, batch_size=1)
    with project, LLMClient(settings, env={"MASKAPP_FAKE_LLM": mode}) as client:
        summary = scan_freetext(wb, run, client)
        assert summary.checked == 0
        if kind:
            assert client.stats[kind] >= 1
        # 失敗は「AI候補なし」として記録して続け、ワーカーが続けて失敗したら問い合わせをやめる
        # （読めない出力はワーカーの失敗ではないため、問い合わせは続ける）
        assert summary.failed == 3 and client.available is (mode == "garbage")
        report, _ = run_through(wb, run)
        assert report.ok, report.blocking
        assert report.by_kind("ai_not_checked")[0].detail["count"] == 3


def test_model_that_cannot_be_loaded(tmp_path):
    project, wb, run = _project(tmp_path)
    empty = tmp_path / "model"
    empty.mkdir()
    settings = LLMSettings(enabled=True, backend="openvino", model_dir=str(empty), device="CPU")
    with project, LLMClient(settings) as client:
        summary = scan_freetext(wb, run, client)
        assert summary.stopped and summary.checked == 0
        with pytest.raises(LLMUnavailable):
            client.generate("s", "u")
        report, _ = run_through(wb, run)
        assert report.ok


def test_worker_blocks_external_network():
    with LLMClient(_settings(), env={"MASKAPP_FAKE_LLM": "probe_network"}) as client:
        assert client.generate("s", "u") == "NetworkBlockedError"


def test_column_types_by_ai():
    columns = [("取引先", ["株式会社あおば商事"]), ("取引先CD", ["000101"]), ("金額", ["1200"])]
    with LLMClient(_settings()) as client:
        specs = suggest_columns_by_ai(client, columns)
    assert specs["取引先"] == ColumnSpec("取引先", Action.REPLACE, entity_type="CUSTOMER", role=Role.NAME)


def test_output_validation():
    sent = ["やまびこ運輸へ〇〇から連絡", "特になし"]
    output = ('思考中… {"c": [[0, "やまびこ運輸", "会社"], [0, "〇〇", "人"], [1, "存在しない", "会社"],'
              '[5, "特に", "他"], [0, "や", "他"], [0, "やまびこ運輸へ", "会社"]]}')
    result = validate_freetext(output, sent)
    # 助詞が付いた語句も文章にあれば候補に残す（名称の末尾の「と」等と区別できないため。登録時に絞れる）
    assert [[c.text for c in r] for r in result] == [["やまびこ運輸", "やまびこ運輸へ"], []]
    assert validate_freetext("候補はありません", sent) is None
    assert validate_freetext('{"c": "x"}', sent) is None
    assert validate_freetext(output, sent)[0][0].type == "会社名"
