"""画面の手順を画面なしで確かめる試験（段階3：Session・設定の下書き・関連候補・対応表の管理・プロジェクト一覧）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from maskapp.app.registry import Registry
from maskapp.app.session import Session, guess_header_row
from maskapp.config import OutsideAction, RelationSpec
from maskapp.entity import admin
from maskapp.formats.csvfile import read_csv
from maskapp.project import Project
from maskapp.project.store import delete_project_folder
from maskapp.transform.reviews import Decision, ReviewKind
from tests.scenarios.setup import MASTER_ORDER, resolve_reviews, write_table
from tests.synthetic.generator import Table

NAMES = {"顧客": "顧客マスタ", "製品": "製品マスタ", "原料": "原料マスタ", "担当者": "担当者マスタ",
         "仕入先": "仕入先マスタ", "製造原価": "製造原価", "製品一覧": "製品一覧"}


@pytest.fixture
def registry(tmp_path) -> Registry:
    return Registry(tmp_path / "app" / "projects.json")


def _first_files(dataset_dir: Path) -> list[Path]:
    return [dataset_dir / f"{m}.xlsx" for m in MASTER_ORDER] + [
        dataset_dir / "売上明細_202604.csv", dataset_dir / "製造原価_202604.xlsx"]


def _define_all(session: Session) -> None:
    """新しい表に、候補どおりの表の定義を作って確認する（利用者の操作の代わり）。"""
    for row in session.tables():
        if row.template is None:
            name = NAMES.get(row.sheet_name) or row.file_name.split("_")[0].removesuffix(".csv")
            session.create_template(row.file_id, row.sheet, name)
    for name in session.templates_in_run():
        session.confirm_all_columns(name)


def _publish(session: Session, export: Path) -> list[Path]:
    run = session.run_id
    resolve_reviews(session.wb, run)
    session.wb.stage(run)
    report = session.wb.verify(run)
    assert report.ok, report.blocking[:5]
    session.wb.commit(run)
    return session.wb.publish(run, export)


def test_first_month_flow_with_suggested_settings(tmp_path, dataset_dir, registry):
    session = Session.create(tmp_path, "画面試験", "#3366cc", registry)
    assert session.project.protected is (sys.platform == "win32")
    assert session.step() == 2
    results = session.import_paths(_first_files(dataset_dir) + [tmp_path / "x.xls"])
    assert [r.error is None for r in results] == [True] * 7 + [False]
    assert "対象外" in results[-1].error
    rows = session.tables()
    assert {r.status for r in rows} == {"新しい表"}
    assert session.step() == 3

    _define_all(session)
    assert session.blocking_config_issues() == []
    candidates = session.relation_candidates()
    pairs = {(c.relation.from_template, c.relation.from_column, c.relation.to_template) for c in candidates}
    assert ("売上明細", "得意先CD", "顧客マスタ") in pairs
    assert ("製造原価", "製品CD", "製品一覧") in pairs or ("製造原価", "製品CD", "製品マスタ") in pairs
    for c in candidates:
        session.add_relation(c.relation)
    summary = session.confirm_config()
    assert session.step() == 4
    # 備考は「文章内を検査」の候補。規則で検出した電話番号・担当者名が要確認になり、判断までは未解決
    patterns = {i.subject.get("pattern") for i in session.wb.reviews(session.run_id)
                if i.kind is ReviewKind.FREETEXT_CANDIDATE}
    assert {"phone", "person_label"} <= patterns and summary.unresolved_cells > 0

    outputs = _publish(session, tmp_path / "export")
    assert len(outputs) == 7
    findings = session.wb.findings(session.wb.conn.execute("SELECT MAX(id) FROM run").fetchone()[0])
    assert any(f["kind"] == "relation_unmatched" and f["origin"] == "source" for f in findings)
    session.close()

    # プロジェクト一覧
    summary = registry.summaries()[0]
    assert (summary.name, summary.color, summary.files) == ("画面試験", "#3366cc", 7)
    assert summary.mappings["顧客"] >= 100 and summary.status.startswith("出力済み")


def test_resume_next_month_reuses_settings_and_decisions(tmp_path, dataset, dataset_dir, registry):
    session = Session.create(tmp_path, "再開試験", None, registry)
    session.import_paths([dataset_dir / "顧客マスタ.xlsx", dataset_dir / "売上明細_202604.csv"])
    _define_all(session)
    session.confirm_config()
    # 同じコードに表記の違う名称（全角空白付き）→「元データの誤りとして扱う」
    run = session.run_id
    for item in session.wb.reviews(run):
        if item.kind is ReviewKind.CODE_MULTI_NAME and item.subject["name"].endswith("　"):
            session.wb.decide(item.id, Decision.KEEP_AS_SOURCE_ERROR)
    _publish(session, tmp_path / "export1")
    session.close()

    # 再起動して翌月（同じ表記違いを含む行を作る）
    month = dataset.monthly["売上明細"][1]
    rows = [list(r) for r in month.rows]
    first = dataset.monthly["売上明細"][0].rows[13]
    rows[0][3], rows[0][4] = first[3], first[4]
    path = write_table(Table("売上明細_翌月", month.headers, rows), tmp_path / "売上明細_翌月.csv", "cp932", "\r\n",
                       False)
    session = Session.open(tmp_path / "再開試験", registry)
    assert session.step() == 2
    session.import_paths([path])
    assert [r.status for r in session.tables()] == ["一致"]  # 既存の定義を自動で適用
    assert session.blocking_config_issues() == []
    session.confirm_config()
    remembered = [i for i in session.wb.reviews(session.run_id, state=None)
                  if i.decision and i.decision.get("remembered")]
    assert remembered and remembered[0].subject["name"] == first[4]
    session.wb.forget_decision(remembered[0].id)  # 前回の判断を取り消して判断し直せる
    assert any(i.id == remembered[0].id for i in session.wb.reviews(session.run_id))
    session.close()


def test_header_diff_and_adapt(tmp_path, dataset, registry):
    session = Session.create(tmp_path, "差分試験", None, registry)
    table = dataset.masters["顧客マスタ"]
    p1 = write_table(table, tmp_path / "c1.csv", "cp932", "\r\n", False)
    session.import_paths([p1])
    _define_all(session)
    session.confirm_config()
    session.wb.cancel(session.run_id)

    headers = list(table.headers)
    headers[7] = "業種区分"  # 改名
    changed = Table("c2", headers + ["ランク"], [list(r) + ["A"] for r in table.rows])
    p2 = write_table(changed, tmp_path / "c2.csv", "cp932", "\r\n", False)
    session.import_paths([p2])
    row = session.tables()[0]
    assert (row.template, row.suggestion) == (None, "c1")
    updated, diff = session.adapt_template(row.file_id, 0, "c1")
    assert diff.renamed == [("業種", "業種区分")] and diff.added == ["ランク"]
    assert set(session.draft.unconfirmed(["c1"])) == {("c1", "業種区分"), ("c1", "ランク")}
    assert session.blocking_config_issues()
    session.confirm_all_columns("c1")
    assert session.blocking_config_issues() == []
    session.close()


def test_title_and_total_rows(tmp_path, registry):
    data = ("売上一覧（社外秘）\r\n\r\n得意先CD,得意先名,金額\r\n000101,株式会社さくら商事,100\r\n"
            "000108,株式会社もみじ物産,200\r\n合計,,300\r\n").encode("cp932")
    path = tmp_path / "t.csv"
    path.write_bytes(data)
    assert guess_header_row(read_csv(data).values()) == 2
    session = Session.create(tmp_path, "範囲試験", None, registry)
    session.import_paths([path])
    row = session.tables()[0]
    session.create_template(row.file_id, 0, "売上一覧", header_row=row.header_row)
    session.confirm_all_columns("売上一覧")
    assert [c.role.value if c.role else None for c in session.draft.templates["売上一覧"].columns] == [
        "code", "name", None]
    session.set_region("売上一覧", skip_total_rows=True, outside_action=OutsideAction.CLEAR)
    assert session.tables()[0].data_last == 4  # 合計行を除く
    session.confirm_config()
    out = _publish(session, tmp_path / "export")
    values = read_csv(out[0].read_bytes()).values()
    assert values[0] == [""] and values[5] == ["", "", ""]  # 表題と合計行は消える
    assert values[3][1].startswith("顧客") and values[3][2] == "100"
    session.close()


def test_master_admin_operations(tmp_path, dataset_dir, registry):
    session = Session.create(tmp_path, "管理試験", None, registry)
    session.import_paths([dataset_dir / "顧客マスタ.xlsx"])
    _define_all(session)
    session.confirm_config()
    _publish(session, tmp_path / "export")
    conn = session.project.conn
    rows = admin.search(conn, "顧客000")
    assert len(rows) >= 9
    a, b = rows[0], rows[1]
    assert admin.affected_outputs(conn, a.id)
    with session.project.transaction() as c:
        admin.add_alias(c, a.id, "略称テスト")
        admin.rename_masked(c, a.id, "顧客A")
        with pytest.raises(admin.MasterEditError):
            admin.rename_masked(c, b.id, "顧客A")
        admin.merge(c, b.id, a.id)
    merged = admin.entity(conn, a.id)
    assert merged.masked_name == "顧客A" and ("略称テスト", "alias") in merged.names
    assert len(merged.codes) == 2
    with session.project.transaction() as c:
        admin.remove_alias(c, a.id, "略称テスト")
        admin.void(c, a.id)
    assert admin.search(conn, "顧客A") == []
    kinds = [k for (k,) in conn.execute("SELECT kind FROM master_event WHERE run_id IS NULL ORDER BY seq")]
    assert kinds == ["alias_add", "rename_masked", "merge", "alias_remove", "void"]
    session.close()


def test_backup_restore_and_delete(tmp_path, registry):
    session = Session.create(tmp_path, "復元試験", None, registry)
    session.project.backup("manual")
    with session.project.transaction() as conn:
        conn.execute("UPDATE project_info SET color = '#ff0000'")
    backup = [p for p in session.project.backups() if p.name.endswith("-manual.db")][0]
    session.project.restore_backup(backup)
    assert session.project.color is None
    root = session.project.root
    session.close()
    with Project.open(root) as p:
        assert p.color is None
    delete_project_folder(root)
    registry.remove(root)
    assert not root.exists() and registry.paths() == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のアクセス権")
def test_project_folder_is_private(tmp_path, registry):
    session = Session.create(tmp_path, "権限試験", None, registry)
    out = subprocess.run(["icacls", str(session.project.root)], capture_output=True).stdout.decode("cp932", "ignore")
    session.close()
    lines = [line for line in out.splitlines()[:-2] if line.strip()]
    assert len(lines) == 1 and "(OI)(CI)(F)" in lines[0] and "(I)" not in lines[0]


def test_relation_candidate_can_be_rejected(tmp_path, dataset_dir, registry):
    session = Session.create(tmp_path, "関連試験", None, registry)
    session.import_paths([dataset_dir / "顧客マスタ.xlsx", dataset_dir / "売上明細_202604.csv"])
    _define_all(session)
    c = session.relation_candidates()[0]
    assert c.relation == RelationSpec("売上明細", "得意先CD", "顧客マスタ", "顧客番号")
    assert c.unmatched_keys == 2  # 顧客マスタにない得意先CD（元データの未一致）
    session.reject_relation(c.relation)
    assert session.relation_candidates() == []
    session.close()
