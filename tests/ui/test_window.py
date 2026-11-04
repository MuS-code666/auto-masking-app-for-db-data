"""画面の試験（段階3）。画面を表示せず（offscreen）、画面の操作で一連の流れが完了することを確かめる。"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from maskapp.app.registry import Registry  # noqa: E402
from maskapp.transform.reviews import Decision  # noqa: E402
from maskapp.ui.common import MASK  # noqa: E402
from maskapp.ui.labels import MASKED_TEXT, NOTICE  # noqa: E402
from maskapp.ui.window import MainWindow  # noqa: E402
from tests.scenarios.setup import resolve_reviews  # noqa: E402

NAMES = {"顧客": "顧客マスタ", "製品一覧": "製品一覧", "製造原価": "製造原価"}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    messages: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: messages.append(a[2]))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: messages.append(a[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    MASK.set(False)
    w = MainWindow(Registry(tmp_path / "app" / "projects.json"))
    w.messages = messages
    w.show()
    yield w
    w.close_project()
    w.close()
    MASK.set(False)


def _define_tables(w: MainWindow) -> None:
    page = w.imports
    for i, row in enumerate(list(page.rows)):
        if row.template is None:
            page.table.selectRow(i)
            name = NAMES.get(row.sheet_name) or row.file_name.split("_")[0].removesuffix(".csv")
            page.new_name.setText(name)
            page._create_template()


def test_full_flow_through_screens(window, tmp_path, dataset_dir):
    w = window
    assert w.stack.currentIndex() == 0
    w.create_project("画面操作", tmp_path / "projects", "#2e8b57")
    assert w.session is not None and w.stack.currentIndex() == 1  # ② ファイル取込
    assert w.step_buttons[2].isEnabled() is False

    files = [dataset_dir / "顧客マスタ.xlsx", dataset_dir / "売上明細_202604.csv",
             dataset_dir / "製造原価_202604.xlsx", tmp_path / "古い形式.xls"]
    files[-1].write_bytes(b"\xd0\xcf\x11\xe0")
    w.imports.add_paths(files)
    assert any("対象外" in m for m in w.messages)  # 取り込めないファイルの案内
    assert w.imports.table.rowCount() == 4  # 顧客・売上・製造原価・製品一覧
    assert {w.imports.table.item(i, 5).text() for i in range(4)} == {"新しい表"}
    w.imports.table.selectRow(2)
    assert w.imports.preview_model.rowCount() == 1001  # 表の内容を仮想表示

    _define_tables(w)
    assert all(r.template for r in w.imports.rows)
    w.go(3)
    assert w.stack.currentIndex() == 2
    page = w.config
    assert not page.next_btn.isEnabled()  # 未確認の列がある
    for i in range(page.template_combo.count()):
        page.template_combo.setCurrentIndex(i)
        page._confirm_all()
    # 列の設定を変える（売上明細の「区分」は維持のまま確認）
    page.template_combo.setCurrentText("売上明細")
    page.columns.selectRow(17)
    page._save_column()
    page.tabs.setCurrentIndex(1)
    page._load_relations()
    assert page.candidates.rowCount() >= 2
    while page.candidates.rowCount():
        page.candidates.selectRow(0)
        page._accept_candidate()
    assert page.relations.count() >= 2
    assert page.next_btn.isEnabled()

    w.confirm_config()
    assert w.stack.currentIndex() == 3  # ④
    review = w.review
    assert review.list.rowCount() > 0 and not review.next_btn.isEnabled()
    review._register_all()
    # 備考（文章内の語句）：規則で検出した電話番号は一括で、担当者名らしい語は1件ずつ種類を選んで登録する
    review._register_values()
    patterns = [i.subject.get("pattern") for i in review.items]
    assert "phone" not in patterns and "person_label" in patterns
    review.list.selectRow(patterns.index("person_label"))
    assert not review.term_row.isHidden() and review.kind_combo.currentData() == "PERSON"
    assert not review.buttons[Decision.REGISTER_NEW].isHidden()
    assert review.buttons[Decision.REGISTER_VALUE].isHidden()
    text = review.items[review.list.currentRow()].subject["text"]
    review._decide(Decision.REGISTER_NEW)
    assert text not in [i.subject.get("text") for i in review.items]
    resolve_reviews(w.session.wb, w.session.run_id)
    review.refresh(reload_units=True)
    assert review.next_btn.isEnabled()
    review.unit.setCurrentIndex(1)
    assert review.after_model.rowCount() > 0
    after = review.after_model.data(review.after_model.index(1, 4))
    assert after.startswith("顧客")

    w.go(5)
    report = w.output.verify()
    assert report is not None and report.ok, report.blocking[:3]
    out = w.output.publish(tmp_path / "export")
    assert len(out) == 3 and all(p.exists() for p in out)
    assert w.output.tree.topLevelItemCount() > 0

    # プロジェクト一覧に戻ると、状況が表示される
    w.close_project()
    w.projects.refresh()
    assert w.projects.table.item(0, 0).text() == "画面操作"
    assert w.projects.table.item(0, 5).text().startswith("出力済み")


def test_resume_opens_at_saved_step_and_mask(window, tmp_path, dataset_dir):
    w = window
    w.create_project("再開画面", tmp_path / "projects", None)
    w.imports.add_paths([dataset_dir / "顧客マスタ.xlsx"])
    _define_tables(w)
    root = w.session.project.root
    w.close_project()

    w.open_project(root)
    assert w.stack.currentIndex() == 2  # ③ から再開（設定の下書きも残っている）
    assert w.config.template_combo.count() == 1
    w.go(2)
    MASK.set(True)  # 元の値を伏せる
    assert w.imports.table.item(0, 0).text() == MASKED_TEXT
    cell = w.imports.preview_model.data(w.imports.preview_model.index(1, 1))
    assert cell == MASKED_TEXT
    assert not w.original_label.isVisible()
    MASK.set(False)
    assert w.imports.table.item(0, 0).text() == "顧客マスタ.xlsx"


def _screen_strings() -> list[str]:
    """画面のモジュールの文字列（説明用の docstring を除く）。"""
    import ast

    ui_dir = Path(__file__).resolve().parents[2] / "src" / "maskapp" / "ui"
    out = []
    for path in ui_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body
            and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
        }
        out += [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
    return out


def test_screen_text_never_claims_anonymization():
    """画面に「完全匿名化済み」「安全」等の表示をしない（設計書 04 章 6）。"""
    strings = _screen_strings()
    assert len(strings) > 200
    assert not [t for t in strings if "完全匿名化" in t or re.search(r"安全(です|に|な)", t)]
    assert "匿名化を保証するものではありません" in NOTICE
