"""段階4 受入シナリオ：文章内の語句（備考欄・表外）（設計書 09 章）。

受入条件：備考欄を含むシナリオで残存0件。短い語・曖昧な語が要確認に出る。
ヘッダー・フッター、グラフの文字は test_acceptance_xlsx（S9・S10）で確かめる。
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from maskapp.app import Workbench
from maskapp.config import Action, ColumnSpec, OutsideAction, Role, TemplateSpec
from maskapp.formats.csvfile import read_csv
from maskapp.formats.xlsx.book import XlsxBook
from maskapp.project import Project
from maskapp.transform.reviews import Decision, ReviewKind
from tests.scenarios.setup import (
    MASTER_ORDER,
    RELATIONS,
    TEMPLATES,
    XLSX_RELATIONS,
    XLSX_TEMPLATES,
    add_files,
    keep,
    run_through,
    write_table,
)
from tests.scenarios.test_acceptance_xlsx import _all_text
from tests.synthetic.generator import ALIAS_FULL, DUP_CUSTOMER_NAME, SHARED_NAME, Table

_PHONE = re.compile(r"099-0\d{3}-\d{4}")


def _freetext(templates: list[TemplateSpec]) -> list[TemplateSpec]:
    """備考を「文章内の語句を置換」にした表の定義。"""
    return [replace(t, columns=[replace(c, action=Action.FREETEXT) if c.header == "備考" else c
                                for c in t.columns]) for t in templates]


def test_remarks_are_replaced_with_no_residual(tmp_path, dataset, dataset_dir):
    """基準ケース（マスタ xlsx・売上/仕入 CSV・製造原価 xlsx）の備考を文章内の置換で出力する。"""
    project = Project.create(tmp_path, "備考")
    wb = Workbench(project)
    with project:
        wb.save_config(_freetext(TEMPLATES + XLSX_TEMPLATES), RELATIONS + XLSX_RELATIONS)
        files = [dataset_dir / f"{m}.xlsx" for m in MASTER_ORDER] + [
            dataset_dir / "売上明細_202604.csv", dataset_dir / "仕入明細_202604.csv",
            dataset_dir / "製造原価_202604.xlsx",
        ]
        run = wb.start_run()
        add_files(wb, run, files)
        wb.plan(run)
        candidates = [i for i in wb.reviews(run) if i.kind is ReviewKind.FREETEXT_CANDIDATE]
        assert {i.subject["pattern"] for i in candidates} >= {"phone", "person_label"}
        report, _ = run_through(wb, run)
        assert report.ok, report.blocking[:5]
        assert not report.by_kind("residual") and not report.by_kind("residual_short")
        assert report.by_kind("freetext_replaced")
        wb.commit(run)
        outputs = {p.name: p for p in wb.publish(run, tmp_path / "export")}

        # 元の名称・電話番号・担当者の姓が、出力のどこにも残っていない
        text = "\n".join(_all_text(p) for p in outputs.values())
        month = {name: tables[0] for name, tables in dataset.monthly.items()}
        remarks = [row[-1] or "" for t in month.values() for row in t.rows]
        phones = {m for r in remarks for m in _PHONE.findall(r)}
        surnames = {p["name"].split(" ")[0] for p in dataset.truth["entities"]["PERSON"]}
        assert phones and not [p for p in phones if p in text]
        assert not [s for s in surnames if s in text]
        leaked = [v for v in dataset.truth["sensitive_values"] if len(v) > 4 and v in text]
        assert leaked == []
        for value in (SHARED_NAME, DUP_CUSTOMER_NAME, ALIAS_FULL):
            assert value not in text

        # 「〇〇様より追加注文」は、同じ行の得意先名と同じ変換後名称になる（同名・略称でも同じ行の対象を使う）
        rows = read_csv(outputs["売上明細_01.csv"].read_bytes()).values()[1:]
        pairs = list(zip(month["売上明細"].rows, rows, strict=True))
        checked = 0
        for src, out in pairs:
            if src[19] == f"{src[4]}様より追加注文" and src[4] == src[4].strip():
                assert out[19] == f"{out[4]}様より追加注文"
                checked += 1
        assert checked > 10
        # 置換する語のない文章はそのまま
        kept = [(s[19], o[19]) for s, o in pairs if s[19] in ("前回同様", "梱包注意\n天地無用", '"至急"対応、送料込')]
        assert kept and all(a == b for a, b in kept)

        # xlsx（製造原価）の備考も置換される
        cost = XlsxBook.parse(outputs["製造原価_01.xlsx"].read_bytes()).sheets[0].grid()
        cost_remarks = [r[19] for r in cost[1:] if len(r) > 19]
        assert cost_remarks and not [r for r in cost_remarks if any(s in r for s in surnames)]


MEMO = TemplateSpec("連絡メモ", [keep("日付"), ColumnSpec("内容", Action.FREETEXT)])


def _memo(rows: list[list[str]]) -> Table:
    return Table("連絡メモ", ["日付", "内容"], rows)


def test_short_and_ambiguous_terms_and_candidates_need_review(tmp_path, dataset):
    """短い語・置換先が複数の語・規則で検出した候補は要確認になり、判断は翌月に引き継がれる。"""
    customers = dataset.masters["顧客マスタ"]
    other = next(r for r in customers.rows if r[1] not in (ALIAS_FULL, DUP_CUSTOMER_NAME, SHARED_NAME))
    rows = [
        ["4/1", f"{SHARED_NAME}の新商品について"],  # 顧客と製品で同じ名称（同じ行に対象なし）
        ["4/2", "砂糖の入荷待ち"],  # 原料名（短い語）
        ["4/3", f"{DUP_CUSTOMER_NAME}様より電話"],  # 同名の別顧客
        ["4/4", "やまびこ運輸株式会社御中 info@yamabiko.example.com"],  # 未登録の法人名・メール
        ["4/5", "前回同様、お客様へ連絡済み"],  # 置換しない
        ["4/6", f"{other[1]}様（{other[0]}）"],  # 名称とコード
    ]
    masters = {name: dataset.masters[name] for name in ("顧客マスタ", "製品マスタ", "原料マスタ")}
    project = Project.create(tmp_path, "メモ")
    wb = Workbench(project)
    with project:
        wb.save_config([t for t in TEMPLATES if t.name in masters] + [MEMO])
        paths = [write_table(t, tmp_path / f"{n}.csv", "utf-8", "\r\n", True) for n, t in masters.items()]
        memo = write_table(_memo(rows), tmp_path / "メモ_04.csv", "utf-8", "\r\n", True)
        run = wb.start_run()
        add_files(wb, run, paths + [memo])
        wb.plan(run)
        wb.decide_all(run, ReviewKind.NEW_ENTITY, Decision.REGISTER_NEW)
        wb.plan(run)
        items = {(i.kind, i.subject.get("term") or i.subject.get("text")): i for i in wb.reviews(run)}
        ambiguous = items[(ReviewKind.FREETEXT_AMBIGUOUS, SHARED_NAME)]
        assert {c["label"] for c in ambiguous.subject["candidates"]} == {"顧客", "製品"}
        same_name = items[(ReviewKind.FREETEXT_AMBIGUOUS, DUP_CUSTOMER_NAME)]
        short = items[(ReviewKind.FREETEXT_SHORT, "砂糖")]
        company = items[(ReviewKind.FREETEXT_CANDIDATE, "やまびこ運輸株式会社")]
        email = items[(ReviewKind.FREETEXT_CANDIDATE, "info@yamabiko.example.com")]
        assert company.subject["pattern"] == "company" and email.subject["info_type"] == "EMAIL"
        assert len(items) == 5  # 「前回同様」「お客様」は候補にしない
        with pytest.raises(RuntimeError):
            wb.stage(run)  # 判断するまで出力できない

        product = next(c for c in ambiguous.subject["candidates"] if c["label"] == "製品")
        wb.decide(ambiguous.id, Decision.REPLACE_TERM, target=product["key"])
        wb.decide(same_name.id, Decision.REPLACE_TERM, target=same_name.subject["candidates"][0]["key"])
        wb.decide(short.id, Decision.REPLACE_TERM, target=short.subject["candidates"][0]["key"])
        wb.decide(company.id, Decision.REGISTER_NEW, entity_type="SUPPLIER")
        wb.decide(email.id, Decision.REGISTER_VALUE, info_type="EMAIL")
        wb.plan(run)
        assert wb.reviews(run) == []
        wb.stage(run)
        report = wb.verify(run)
        assert report.ok, report.blocking
        wb.commit(run)
        out = {p.name: p for p in wb.publish(run, tmp_path / "export1")}
        memo_out = [r[1] for r in read_csv(out["連絡メモ_01.csv"].read_bytes()).values()[1:]]
        masked = dict(project.conn.execute(
            "SELECT n.original_name, e.masked_name FROM entity_name n JOIN entity e ON e.id = n.entity_id "
            "WHERE e.entity_type_id IN ('PRODUCT', 'MATERIAL', 'SUPPLIER')"))
        assert memo_out[0] == f"{masked[SHARED_NAME]}の新商品について"  # 製品を選んだ
        assert memo_out[1] == f"{masked['砂糖']}の入荷待ち"
        token = project.conn.execute("SELECT masked_value FROM value_token WHERE original_value = ?",
                                     ("info@yamabiko.example.com",)).fetchone()[0]
        assert memo_out[3] == f"{masked['やまびこ運輸株式会社']}御中 {token}"
        assert memo_out[4] == "前回同様、お客様へ連絡済み"
        assert other[1] not in memo_out[5] and other[0] not in memo_out[5]

    # 翌月：同じ語は前回の判断・登録で置換され、要確認は出ない
    project = Project.open(tmp_path / "メモ")
    wb = Workbench(project)
    with project:
        memo2 = write_table(_memo(rows + [["5/1", "砂糖の追加発注"]]), tmp_path / "メモ_05.csv", "utf-8", "\r\n",
                            True)
        run = wb.start_run()
        add_files(wb, run, [memo2])
        wb.plan(run)
        assert wb.reviews(run) == []
        remembered = [i for i in wb.reviews(run, state=None) if i.decision and i.decision.get("remembered")]
        assert {i.kind for i in remembered} >= {ReviewKind.FREETEXT_SHORT, ReviewKind.FREETEXT_AMBIGUOUS}
        report, staged = run_through(wb, run)
        assert report.ok, report.blocking
        text = _all_text(staged[0])
        for word in (SHARED_NAME, "砂糖", DUP_CUSTOMER_NAME, "やまびこ運輸", "yamabiko"):
            assert word not in text


def test_short_term_can_be_kept_as_is(tmp_path, dataset):
    """短い語を「問題なし」とすれば、その列ではそのまま出力する（残存検査でも止めない）。"""
    project = Project.create(tmp_path, "短い語")
    wb = Workbench(project)
    with project:
        wb.save_config([t for t in TEMPLATES if t.name == "原料マスタ"] + [MEMO])
        master = write_table(dataset.masters["原料マスタ"], tmp_path / "原料.csv", "utf-8", "\r\n", True)
        memo = write_table(_memo([["4/1", "砂糖の入荷待ち"]]), tmp_path / "メモ.csv", "utf-8", "\r\n", True)
        run = wb.start_run()
        add_files(wb, run, [master, memo])
        wb.plan(run)
        wb.decide_all(run, ReviewKind.NEW_ENTITY, Decision.REGISTER_NEW)
        wb.plan(run)
        (short,) = [i for i in wb.reviews(run) if i.kind is ReviewKind.FREETEXT_SHORT]
        wb.decide(short.id, Decision.FALSE_POSITIVE)
        wb.plan(run)
        staged = wb.stage(run)
        report = wb.verify(run)
        assert report.ok, report.blocking
        out = {p.name: p for p in staged}
        assert read_csv(out["連絡メモ_01.csv"].read_bytes()).values()[1][1] == "砂糖の入荷待ち"


def test_outside_area_is_replaced(tmp_path, dataset):
    """表外（見出しより上のタイトル）を「文章内の語句を置換」にした表。"""
    customers = dataset.masters["顧客マスタ"].rows[:10]
    lines = [f"{ALIAS_FULL} 様 ご請求一覧,", "顧客番号,顧客名", *(f"{r[0]},{r[1]}" for r in customers)]
    path = tmp_path / "請求.csv"
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("cp932"))
    template = TemplateSpec("請求", [
        ColumnSpec("顧客番号", Action.REPLACE, entity_type="CUSTOMER", role=Role.CODE, code_scheme="顧客コード"),
        ColumnSpec("顧客名", Action.REPLACE, entity_type="CUSTOMER", role=Role.NAME),
    ], header_row=1, outside_action=OutsideAction.FREETEXT)
    project = Project.create(tmp_path, "表外")
    wb = Workbench(project)
    with project:
        wb.save_config([template])
        run = wb.start_run()
        add_files(wb, run, [path])
        report, staged = run_through(wb, run)
        assert report.ok, report.blocking
        out = read_csv(staged[0].read_bytes()).values()
        assert customers[0][1] == ALIAS_FULL
        assert out[0][0] == f"{out[2][1]} 様 ご請求一覧"  # 1件目の顧客（正式名）の変換後名称
