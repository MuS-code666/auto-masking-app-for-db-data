"""段階1 受入シナリオ（CSV）：S1〜S8, S11〜S14（設計書 09 章）。"""

from __future__ import annotations

import os
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

from maskapp.app import RunState, RunStateError, Workbench
from maskapp.app.workbench import OutputLocationError
from maskapp.formats.csvfile import read_csv
from maskapp.infra import netguard
from maskapp.project import Project
from maskapp.transform.reviews import Decision, ReviewKind
from tests.scenarios.setup import (
    MASTER_ORDER,
    RELATIONS,
    TEMPLATES,
    add_files,
    run_through,
    write_csv_dataset,
    write_table,
)
from tests.synthetic.generator import (
    ALIAS_FULL,
    ALIAS_SHORT,
    DUP_CUSTOMER_NAME,
    SHARED_NAME,
    UNREGISTERED_CUSTOMER_CODES,
    Table,
)


@pytest.fixture(scope="module")
def csv_files(dataset, tmp_path_factory) -> dict[str, list[Path]]:
    return write_csv_dataset(dataset, tmp_path_factory.mktemp("csv_in"))


def _open(tmp_path: Path, name: str = "受入試験") -> tuple[Project, Workbench]:
    project = Project.create(tmp_path, name)
    wb = Workbench(project)
    wb.save_config(TEMPLATES, RELATIONS)
    return project, wb


def _first_month(csv_files) -> list[Path]:
    return [p for k in MASTER_ORDER for p in csv_files[k]] + [csv_files["売上明細"][0], csv_files["仕入明細"][0]]


def _publish_all(wb: Workbench, files: list[Path], export: Path) -> tuple[int, list[Path]]:
    run = wb.start_run()
    add_files(wb, run, files)
    report, _ = run_through(wb, run)
    assert report.ok, [f for f in report.blocking]
    wb.commit(run)
    return run, wb.publish(run, export)


def _rows(path: Path) -> list[list[str]]:
    return read_csv(path.read_bytes()).values()


def _by_header(rows: list[list[str]], header: str) -> list[str]:
    idx = rows[0].index(header)
    return [r[idx] for r in rows[1:]]


def _masked_code(project: Project, scheme: str, original: str) -> str:
    return project.conn.execute(
        "SELECT c.masked_code FROM entity_code c JOIN code_scheme s ON s.id = c.code_scheme_id "
        "WHERE s.display_name = ? AND c.original_code = ? AND c.status = 'confirmed'",
        (scheme, original),
    ).fetchone()[0]


def _masked_name_by_code(project: Project, scheme: str, original: str) -> tuple[str, int]:
    return project.conn.execute(
        "SELECT e.masked_name, e.name_seq FROM entity e JOIN entity_code c ON c.entity_id = e.id "
        "JOIN code_scheme s ON s.id = c.code_scheme_id WHERE s.display_name = ? AND c.original_code = ? "
        "AND c.status = 'confirmed'",
        (scheme, original),
    ).fetchone()


# ---------------------------------------------------------------- S1 / S6 / S7


def test_s1_s6_s7_join_aggregate_structure(tmp_path, dataset, csv_files):
    project, wb = _open(tmp_path)
    with project:
        run, outputs = _publish_all(wb, _first_month(csv_files), tmp_path / "export")
        out_by_name = {p.name: p for p in outputs}
        report_relations = {(f["detail"]["from"]): f for f in wb.findings(run) if f["kind"] == "relation_unmatched"}

        # S6: 構造と維持値（検証 V1・V2 が合格）、行数・列数が同じ
        src_sales = _rows(csv_files["売上明細"][0])
        out_sales = _rows(out_by_name["売上明細_01.csv"])
        assert len(src_sales) == len(out_sales)
        assert {len(r) for r in out_sales} == {20}
        for header in ("伝票番号", "売上日", "数量", "単価", "金額", "消費税", "原価", "取引年月"):
            assert _by_header(src_sales, header) == _by_header(out_sales, header)

        # S1: 置換後も結合・集計できる（顧客別の金額合計が対応する変換後コードで一致）
        src_customers = _rows(csv_files["顧客マスタ"][0])
        out_customers = _rows(out_by_name["顧客マスタ_01.csv"])
        out_master_codes = set(_by_header(out_customers, "顧客番号"))
        totals_before: dict[str, Decimal] = defaultdict(Decimal)
        totals_after: dict[str, Decimal] = defaultdict(Decimal)
        for code, amount in zip(_by_header(src_sales, "得意先CD"), _by_header(src_sales, "金額"), strict=True):
            totals_before[code] += Decimal(amount)
        for code, amount in zip(_by_header(out_sales, "得意先CD"), _by_header(out_sales, "金額"), strict=True):
            totals_after[code] += Decimal(amount)
        for code, total in totals_before.items():
            if code:
                assert totals_after[_masked_code(project, "顧客コード", code)] == total
        src_master_codes = set(_by_header(src_customers, "顧客番号"))
        joined_before = sum(1 for c in _by_header(src_sales, "得意先CD") if c in src_master_codes)
        joined_after = sum(1 for c in _by_header(out_sales, "得意先CD") if c in out_master_codes)
        assert joined_before == joined_after > 0

        # S7: 元からの未登録コードは元データ由来の不備として残る（変換由来の不備は 0）
        unmatched = report_relations["売上明細.得意先CD"]
        assert unmatched["origin"] == "source"
        assert unmatched["detail"]["keys"] == len(UNREGISTERED_CUSTOMER_CODES)
        assert not [f for f in wb.findings(run) if f["origin"] == "transform"]
        for code in UNREGISTERED_CUSTOMER_CODES:
            assert _masked_code(project, "顧客コード", code) not in out_master_codes


# ---------------------------------------------------------------- S3 / S4 / S5


def test_s3_s4_s5_entity_identity(tmp_path, dataset, csv_files):
    project, wb = _open(tmp_path)
    with project:
        _, outputs = _publish_all(wb, _first_month(csv_files), tmp_path / "export")
        out = {p.name: _rows(p) for p in outputs}
        customers = dataset.masters["顧客マスタ"].rows
        products = dataset.masters["製品マスタ"].rows

        # S3: 同じ文字列の顧客名と製品名は別の種類として別々に置換される
        cust_code = next(r[0] for r in customers if r[1] == SHARED_NAME)
        prod_code = next(r[0] for r in products if r[1] == SHARED_NAME)
        cust_masked, _ = _masked_name_by_code(project, "顧客コード", cust_code)
        prod_masked, _ = _masked_name_by_code(project, "製品コード", prod_code)
        assert cust_masked.startswith("顧客") and prod_masked.startswith("製品")

        # S4: 同一名称の異なる顧客は別コードから区別される
        dup_codes = [r[0] for r in customers if r[1] == DUP_CUSTOMER_NAME]
        dup_masked = {_masked_name_by_code(project, "顧客コード", c)[0] for c in dup_codes}
        assert len(dup_masked) == 2
        out_names = _by_header(out["顧客マスタ_01.csv"], "顧客名")
        assert all(m in out_names for m in dup_masked)

        # S5: 略称・正式名は確認した別名登録で同じ対象につながる
        alias_code = next(r[0] for r in customers if r[1] == ALIAS_FULL)
        alias_masked, _ = _masked_name_by_code(project, "顧客コード", alias_code)
        names = project.conn.execute(
            "SELECT n.original_name, n.kind FROM entity_name n JOIN entity_code c ON c.entity_id = n.entity_id "
            "WHERE c.original_code = ? AND n.status = 'confirmed'",
            (alias_code,),
        ).fetchall()
        assert (ALIAS_SHORT, "alias") in names and (ALIAS_FULL, "primary") in names
        sales = out["売上明細_01.csv"]
        masked_code = _masked_code(project, "顧客コード", alias_code)
        assert {n for c, n in zip(_by_header(sales, "得意先CD"), _by_header(sales, "得意先名"), strict=True)
                if c == masked_code} == {alias_masked}

        # 元の名称・コードが出力に残っていない
        all_text = "\n".join(",".join(r) for rows in out.values() for r in rows)
        for value in (SHARED_NAME, DUP_CUSTOMER_NAME, ALIAS_FULL, ALIAS_SHORT, alias_code):
            assert value not in all_text


def test_alias_requires_review(tmp_path, csv_files):
    """略称は自動で同一視せず、要確認として人の判断を求める。"""
    project, wb = _open(tmp_path)
    with project:
        run = wb.start_run()
        add_files(wb, run, _first_month(csv_files))
        wb.plan(run)
        items = [i for i in wb.reviews(run) if i.kind is ReviewKind.CODE_MULTI_NAME]
        assert any(i.subject["name"] == ALIAS_SHORT for i in items)
        with pytest.raises(RunStateError):
            wb.commit(run)


# ---------------------------------------------------------------- S2 / S11 / S13


def _with_new_customer(table: Table, code: str, name: str) -> Table:
    rows = [list(r) for r in table.rows]
    rows[0][3], rows[0][4] = code, name
    return Table(table.name + "_追加", table.headers, rows)


def test_s2_s11_next_month_keeps_mapping(tmp_path, dataset, csv_files):
    project, wb = _open(tmp_path)
    with project:
        _publish_all(wb, _first_month(csv_files), tmp_path / "export1")
        before = dict(project.conn.execute(
            "SELECT c.original_code, e.masked_name FROM entity_code c JOIN entity e ON e.id = c.entity_id "
            "JOIN code_scheme s ON s.id = c.code_scheme_id WHERE s.display_name = '顧客コード'"
        ).fetchall())
        max_seq = project.conn.execute(
            "SELECT MAX(name_seq) FROM entity WHERE entity_type_id = 'CUSTOMER'"
        ).fetchone()[0]

        # 翌月：既存顧客＋新規顧客1件
        new_month = _with_new_customer(dataset.monthly["売上明細"][1], "008888", "株式会社しんき試験商会")
        path = write_table(new_month, tmp_path / "売上明細_翌月.csv", "cp932", "\r\n", False)
        run2 = wb.start_run()
        add_files(wb, run2, [path])
        wb.plan(run2)
        new_items = [i for i in wb.reviews(run2) if i.kind is ReviewKind.NEW_ENTITY]
        assert [i.subject["code"] for i in new_items] == ["008888"]  # 新規の対象だけが確認対象
        preview = {(p.record, p.header): p.after for p in wb.preview(run2, wb.import_csv(path).id)}
        report, _ = run_through(wb, run2)
        assert report.ok
        wb.commit(run2)
        outputs = wb.publish(run2, tmp_path / "export2")

        rows = _rows(outputs[0])
        src = _rows(path)
        for code, out_name in zip(_by_header(src, "得意先CD"), _by_header(rows, "得意先名"), strict=True):
            if code in before:
                assert out_name == before[code]  # 既存顧客は同じ変換先
        new_name, new_seq = _masked_name_by_code(project, "顧客コード", "008888")
        assert new_seq > max_seq
        # S11: プレビューで見えた番号がそのまま出力の番号
        assert preview[(1, "得意先名")] == new_name == rows[1][4]


def test_s11_rerun_and_restart_give_identical_output(tmp_path, csv_files):
    files = _first_month(csv_files)
    project, wb = _open(tmp_path)
    project.close()
    with Project.open(tmp_path / "受入試験") as project:
        wb = Workbench(project)
        _, first = _publish_all(wb, files, tmp_path / "export1")
    # 再起動後に同じファイルで再実行
    with Project.open(tmp_path / "受入試験") as project:
        wb = Workbench(project)
        run = wb.start_run()
        add_files(wb, run, files)
        wb.plan(run)
        assert [i for i in wb.reviews(run) if i.kind is ReviewKind.NEW_ENTITY] == []
        report, _ = run_through(wb, run)
        assert report.ok
        wb.commit(run)
        second = wb.publish(run, tmp_path / "export2")
    assert [p.read_bytes() for p in first] == [p.read_bytes() for p in second]


def test_s13_projects_do_not_share_masters(tmp_path, csv_files):
    files = _first_month(csv_files)
    pa, wa = _open(tmp_path / "a", "案件A")
    with pa:
        _publish_all(wa, files, tmp_path / "export_a")
        extra = csv_files["売上明細"][1]
        _publish_all(wa, [extra], tmp_path / "export_a2")
        count_a = pa.conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
    pb, wb = _open(tmp_path / "b", "案件B")
    with pb:
        assert pb.conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 0
        _publish_all(wb, [csv_files["売上明細"][1]], tmp_path / "export_b")
        # 案件Bは顧客マスタを持たないため、売上明細の初出順で番号が振られる（案件Aとは独立）
        count_b = pb.conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
        assert count_b < count_a
    with Project.open(tmp_path / "a" / "案件A") as pa:
        # 案件Bの作業は案件Aの対応表を変えない
        assert pa.conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == count_a


# ---------------------------------------------------------------- S12


def test_s12_crash_after_plan_and_stage_keeps_numbers(tmp_path, csv_files):
    files = _first_month(csv_files)
    project, wb = _open(tmp_path)
    run = wb.start_run()
    add_files(wb, run, files)
    wb.plan(run)
    file_id = wb.import_csv(files[0]).id
    before = {(p.record, p.column): p.after for p in wb.preview(run, file_id)}
    wb.decide_all(run, ReviewKind.NEW_ENTITY, Decision.REGISTER_NEW)
    project.close()  # 計画後に終了

    with Project.open(tmp_path / "受入試験") as project:
        wb = Workbench(project)
        assert wb.state(run) is RunState.PLANNED
        after = {(p.record, p.column): p.after for p in wb.preview(run, file_id)}
        assert after == before
        report, staged = run_through(wb, run)
        assert report.ok and staged[0].exists()
    # 書出し・検証後（確定前）に終了 → 作業領域は破棄され planned に戻る
    with Project.open(tmp_path / "受入試験") as project:
        wb = Workbench(project)
        assert wb.state(run) is RunState.PLANNED
        assert not staged[0].exists()
        assert project.conn.execute(
            "SELECT COUNT(*) FROM entity WHERE status = 'confirmed'"
        ).fetchone()[0] == 0


def test_s12_publish_failure_and_recovery(tmp_path, csv_files, monkeypatch):
    files = _first_month(csv_files)
    project, wb = _open(tmp_path)
    run = wb.start_run()
    add_files(wb, run, files)
    report, _ = run_through(wb, run)
    assert report.ok
    revision = wb.commit(run)
    export = tmp_path / "export"

    real_replace = os.replace
    calls = {"n": 0}

    def failing_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        wb.publish(run, export)
    monkeypatch.setattr(os, "replace", real_replace)
    assert list(export.glob("*.partial"))  # 書きかけが残っている
    project.close()

    with Project.open(tmp_path / "受入試験") as project:
        wb = Workbench(project)
        assert not list(export.glob("*.partial"))  # 復旧で書きかけを削除
        assert wb.state(run) is RunState.COMMITTED
        assert project.master_revision() == revision  # 対応表は確定済みのまま
        # 作業領域を失っても確定した対応から作り直して公開できる
        for p in (project.work_dir / "runs" / str(run)).iterdir():
            p.unlink()
    with Project.open(tmp_path / "受入試験") as project:
        wb = Workbench(project)
        outputs = wb.publish(run, export)
        assert wb.state(run) is RunState.PUBLISHED
        assert len(outputs) == len(files)


def test_s12_cancel_voids_numbers(tmp_path, csv_files):
    project, wb = _open(tmp_path)
    with project:
        run = wb.start_run()
        add_files(wb, run, [csv_files["顧客マスタ"][0]])
        wb.plan(run)
        reserved = project.conn.execute(
            "SELECT MAX(name_seq) FROM entity WHERE entity_type_id = 'CUSTOMER'"
        ).fetchone()[0]
        voided = wb.cancel(run)
        assert voided == reserved
        assert project.conn.execute(
            "SELECT COUNT(*) FROM entity WHERE status != 'void'"
        ).fetchone()[0] == 0

        run2 = wb.start_run()
        add_files(wb, run2, [csv_files["顧客マスタ"][0]])
        wb.plan(run2)
        first_new = project.conn.execute(
            "SELECT MIN(name_seq) FROM entity WHERE created_run_id = ? AND status = 'draft'", (run2,)
        ).fetchone()[0]
        assert first_new == reserved + 1  # 取り消した番号は再利用しない


def test_only_one_active_run(tmp_path, csv_files):
    project, wb = _open(tmp_path)
    with project:
        wb.start_run()
        with pytest.raises(RunStateError):
            wb.start_run()


# ---------------------------------------------------------------- 出力先・残存検査


def test_export_location_rules(tmp_path, csv_files):
    project, wb = _open(tmp_path)
    with project:
        run = wb.start_run()
        add_files(wb, run, [csv_files["顧客マスタ"][0]])
        report, _ = run_through(wb, run)
        wb.commit(run)
        with pytest.raises(OutputLocationError):
            wb.publish(run, project.root / "exports")  # プロジェクト内は不可
        with pytest.raises(OutputLocationError):
            wb.publish(run, csv_files["顧客マスタ"][0].parent)  # 原本と同じフォルダは不可
        export = tmp_path / "export"
        export.mkdir()
        (export / "顧客マスタ_01.csv").write_text("existing")
        with pytest.raises(OutputLocationError):
            wb.publish(run, export)  # 既存ファイルは上書きしない
        assert (export / "顧客マスタ_01.csv").read_text() == "existing"


def test_residual_in_kept_column_blocks_output(tmp_path, csv_files):
    """備考を「維持」にすると、備考に含まれる顧客名が残存として検出され出力できない。"""
    from dataclasses import replace

    from maskapp.config import Action

    templates = [
        replace(t, columns=[replace(c, action=Action.KEEP) if c.header == "備考" else c for c in t.columns])
        if t.name == "売上明細" else t
        for t in TEMPLATES
    ]
    project = Project.create(tmp_path, "残存試験")
    with project:
        wb = Workbench(project)
        wb.save_config(templates, RELATIONS)
        run = wb.start_run()
        add_files(wb, run, [csv_files["顧客マスタ"][0], csv_files["売上明細"][0]])
        report, _ = run_through(wb, run)
        assert not report.ok
        assert report.by_kind("residual")
        assert all(f.location["column"] == 19 for f in report.by_kind("residual"))
        assert wb.state(run) is RunState.PLANNED


def test_output_filename_with_original_name_is_blocked(tmp_path, csv_files):
    project, wb = _open(tmp_path)
    with project:
        run = wb.start_run()
        file_id = add_files(wb, run, [csv_files["顧客マスタ"][0]])[0]
        wb.plan(run)
        from tests.scenarios.setup import resolve_reviews

        resolve_reviews(wb, run)
        wb.stage(run, {file_id: f"{DUP_CUSTOMER_NAME}向け.csv"})
        report = wb.verify(run)
        assert report.by_kind("residual_filename")


# ---------------------------------------------------------------- S14


def test_s14_offline(tmp_path, csv_files):
    """試験プロセスは外部通信を遮断した状態で、一連の変換が完了する（LLM は段階5）。"""
    assert netguard.is_installed()
    project, wb = _open(tmp_path)
    with project:
        _publish_all(wb, [csv_files["顧客マスタ"][0]], tmp_path / "export")
