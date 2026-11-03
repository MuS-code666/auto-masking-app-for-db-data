"""コードのない名称列の照合（設計書 05 章 2.2）。自動で同一視せず、曖昧な場合は人の判断を求める。"""

from __future__ import annotations

from pathlib import Path

import pytest

from maskapp.app import Workbench
from maskapp.config import Action, ColumnSpec, ConfigError, TemplateSpec
from maskapp.project import Project
from maskapp.transform.reviews import Decision, ReviewKind
from tests.scenarios.setup import TEMPLATES, add_files, keep, name, write_csv_dataset
from tests.synthetic.generator import ALIAS_FULL, ALIAS_SHORT, DUP_CUSTOMER_NAME

VISITS = TemplateSpec("訪問記録", [keep("訪問日"), name("訪問先", "CUSTOMER"), keep("件数")])


def _visits_csv(path: Path, names: list[str]) -> Path:
    lines = ["訪問日,訪問先,件数"] + [f"2026/04/{i + 1:02d},{n},{i}" for i, n in enumerate(names)]
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("cp932"))
    return path


@pytest.fixture
def setup(tmp_path, dataset):
    files = write_csv_dataset(dataset, tmp_path / "in")
    project = Project.create(tmp_path, "名称照合")
    wb = Workbench(project)
    wb.save_config(TEMPLATES + [VISITS])
    yield project, wb, files, dataset
    project.close()


def test_name_only_column_resolution(setup, tmp_path):
    project, wb, files, dataset = setup
    unique_name = dataset.masters["顧客マスタ"].rows[10][1]
    visits = _visits_csv(tmp_path / "visits.csv", [unique_name, DUP_CUSTOMER_NAME, ALIAS_SHORT, "株式会社まったく新規"])
    run = wb.start_run()
    add_files(wb, run, [files["顧客マスタ"][0], visits])
    wb.plan(run)
    wb.decide_all(run, ReviewKind.NEW_ENTITY, Decision.REGISTER_NEW)
    summary = wb.plan(run)

    kinds = {i.kind: i for i in wb.reviews(run)}
    # 同名の顧客が2件 → どちらか選ぶまで未解決
    assert set(kinds) == {ReviewKind.NAME_AMBIGUOUS, ReviewKind.SIMILAR_NAME}
    assert len(kinds[ReviewKind.NAME_AMBIGUOUS].subject["candidates"]) == 2
    # 略称は正規化で正式名と一致 → 候補として提示（自動で同一視しない）
    similar = kinds[ReviewKind.SIMILAR_NAME]
    assert similar.subject["name"] == ALIAS_SHORT
    assert ALIAS_FULL in similar.subject["candidates"][0]["names"]
    assert summary.unresolved_cells == 2

    visits_id = wb.import_csv(visits).id
    preview = {p.before: p.after for p in wb.preview(run, visits_id)}
    assert preview[unique_name].startswith("顧客")  # 一意な名称はそのまま照合
    assert preview[DUP_CUSTOMER_NAME] is None
    assert preview["株式会社まったく新規"].startswith("顧客")  # 未登録の名称は新しい対象

    # 判断：同名は2件目を選ぶ、略称は正式名の対象に別名登録
    chosen = kinds[ReviewKind.NAME_AMBIGUOUS].subject["candidates"][1]
    wb.decide(kinds[ReviewKind.NAME_AMBIGUOUS].id, Decision.LINK_EXISTING, chosen["entity_id"])
    wb.decide(similar.id, Decision.LINK_EXISTING, similar.subject["candidates"][0]["entity_id"])
    wb.plan(run)
    assert wb.reviews(run) == []
    preview = {p.before: p.after for p in wb.preview(run, visits_id)}
    assert preview[DUP_CUSTOMER_NAME] == chosen["masked_name"]
    assert preview[ALIAS_SHORT] == similar.subject["candidates"][0]["masked_name"]


def test_link_new_entity_to_existing_leaves_gap(setup, tmp_path):
    project, wb, files, dataset = setup
    visits = _visits_csv(tmp_path / "visits.csv", ["株式会社べつめい"])
    run = wb.start_run()
    add_files(wb, run, [files["顧客マスタ"][0], visits])
    wb.plan(run)
    new_item = next(i for i in wb.reviews(run) if i.subject.get("name") == "株式会社べつめい")
    target = next(i for i in wb.reviews(run) if i.subject.get("code") == dataset.masters["顧客マスタ"].rows[5][0])
    wb.decide(new_item.id, Decision.LINK_EXISTING, target.subject["entity_id"])
    wb.decide_all(run, ReviewKind.NEW_ENTITY, Decision.REGISTER_NEW)
    wb.plan(run)
    preview = {p.before: p.after for p in wb.preview(run, wb.import_csv(visits).id)}
    assert preview["株式会社べつめい"] == target.subject["masked_name"]
    status = project.conn.execute(
        "SELECT status FROM entity WHERE id = ?", (new_item.subject["entity_id"],)
    ).fetchone()[0]
    assert status == "void"  # 紐付けた仮登録の番号は欠番


def test_freetext_is_rejected_until_phase4(tmp_path):
    with Project.create(tmp_path, "自由記述") as project:
        wb = Workbench(project)
        wb.save_config([TemplateSpec("t", [ColumnSpec("備考", Action.FREETEXT)])])
        path = tmp_path / "t.csv"
        path.write_bytes("備考\r\nこんにちは\r\n".encode("cp932"))
        run = wb.start_run()
        add_files(wb, run, [path])
        with pytest.raises(ConfigError):
            wb.plan(run)
