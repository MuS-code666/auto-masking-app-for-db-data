"""画面4：変換前後の確認（設計書 04 章 5）。要確認の判断と、元／変換後の比較表示。"""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from maskapp.app.session import Session
from maskapp.transform.reviews import ALLOWED, Decision, ReviewItem, ReviewKind
from maskapp.ui.common import CHANGED_BG, CLEARED_BG, HEADER_BG, MASK, GridModel, run_safely
from maskapp.ui.labels import CANDIDATE_PATTERNS, DECISIONS, REVIEW_KINDS, VALUE_TYPES


def describe_item(item: ReviewItem) -> str:
    s = item.subject
    show = MASK.show
    if item.kind is ReviewKind.NEW_ENTITY:
        what = "・".join(show(v) for v in (s.get("code"), s.get("name")) if v)
        return f"{what} → {s.get('masked_name')}"
    if item.kind is ReviewKind.CODE_MULTI_NAME:
        known = "、".join(show(n) for n in s.get("known_names", []))
        return f"コード {show(s.get('code'))}：登録済みの名称「{known}」に対し「{show(s.get('name'))}」"
    if item.kind in (ReviewKind.NAME_AMBIGUOUS, ReviewKind.SIMILAR_NAME):
        cands = "、".join(c["masked_name"] for c in s.get("candidates", []))
        return f"「{show(s.get('name'))}」の候補：{cands}"
    if item.kind is ReviewKind.RESIDUAL_SHORT:
        loc = item.location
        where = f"行 {loc.get('record', loc.get('row', 0)) + 1}・列 {loc.get('column', 0) + 1}"
        return f"短い語「{show(s.get('term'))}」が {where} に残っています"
    if item.kind in (ReviewKind.FREETEXT_SHORT, ReviewKind.FREETEXT_AMBIGUOUS):
        cands = "、".join(f"{c['masked_name']}（{c['label']}）" for c in s.get("candidates", []))
        return f"「{show(s.get('term'))}」〔{s.get('scope')}〕置換先の候補：{cands}　例：{show(s.get('context'))}"
    if item.kind is ReviewKind.FREETEXT_CANDIDATE:
        pattern = CANDIDATE_PATTERNS.get(s.get("pattern"), "")
        return f"{pattern}「{show(s.get('text'))}」〔{s.get('scope')}〕　例：{show(s.get('context'))}"
    return ""


class ReviewPage(QWidget):
    changed = Signal()
    next_requested = Signal()
    back_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session: Session | None = None
        self.items: list[ReviewItem] = []
        self._changes: dict[tuple[int, int], str | None] = {}

        self.counts = QLabel()
        self.list = QTableWidget(0, 3)
        self.list.setHorizontalHeaderLabels(["種類", "内容", "判断"])
        self.list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.list.horizontalHeader().setStretchLastSection(True)
        self.list.itemSelectionChanged.connect(self._item_selected)
        self.show_decided = QComboBox()
        self.show_decided.addItem("未確認だけ表示", "open")
        self.show_decided.addItem("すべて表示（前回の判断を含む）", None)
        self.show_decided.currentIndexChanged.connect(self.refresh)

        self.candidate = QComboBox()
        self.candidate_label = QLabel("紐付ける既存の対象")
        # 文章内の候補の登録：登録する語句（検出した語句の一部に絞れる）と種類
        self.term_edit = QLineEdit()
        self.kind_combo = QComboBox()
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        self.buttons: dict[Decision, QPushButton] = {}
        actions = QHBoxLayout()
        for decision in Decision:
            b = QPushButton(DECISIONS[decision.value])
            b.clicked.connect(lambda _=False, d=decision: self._decide(d))
            self.buttons[decision] = b
            actions.addWidget(b)
        self.forget = QPushButton("前回の判断を取り消す")
        self.forget.clicked.connect(self._forget)
        actions.addWidget(self.forget)
        bulk = QPushButton("新しい対象をすべて登録")
        bulk.clicked.connect(self._register_all)
        bulk_values = QPushButton("検出した電話番号・メール等をすべて置換")
        bulk_values.setToolTip("規則で検出した電話番号・メール・郵便番号・URL を、値として登録して置換します")
        bulk_values.clicked.connect(self._register_values)
        actions.addStretch()
        actions.addWidget(bulk_values)
        actions.addWidget(bulk)
        self.term_row = QWidget()
        tr = QHBoxLayout(self.term_row)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.addWidget(QLabel("登録する語句"))
        tr.addWidget(self.term_edit, 1)
        tr.addWidget(QLabel("種類"))
        tr.addWidget(self.kind_combo)
        cand_row = QHBoxLayout()
        cand_row.addWidget(self.candidate_label)
        cand_row.addWidget(self.candidate, 1)

        # 比較表示
        self.unit = QComboBox()
        self.unit.currentIndexChanged.connect(self._load_compare)
        self.filter = QComboBox()
        self.filter.addItem("変更のある行", "changed")
        self.filter.addItem("全行", "all")
        self.filter.currentIndexChanged.connect(self._load_compare)
        self.before_model = GridModel(self)
        self.after_model = GridModel(self)
        self.before = QTableView()
        self.after = QTableView()
        self.before.setModel(self.before_model)
        self.after.setModel(self.after_model)
        self.before.verticalScrollBar().valueChanged.connect(self.after.verticalScrollBar().setValue)
        self.after.verticalScrollBar().valueChanged.connect(self.before.verticalScrollBar().setValue)
        note = QLabel("変換後の番号は確保済みのもので、出力でも同じになります。"
                      "黄＝置換（文章内の語句の置換を含む）、赤＝内容を消す。")
        compare_head = QHBoxLayout()
        compare_head.addWidget(QLabel("表"))
        compare_head.addWidget(self.unit, 1)
        compare_head.addWidget(self.filter)
        panes = QSplitter(Qt.Orientation.Horizontal)
        for title, view in (("元", self.before), ("変換後", self.after)):
            w = QWidget()
            lw = QVBoxLayout(w)
            lw.setContentsMargins(0, 0, 0, 0)
            lw.addWidget(QLabel(title))
            lw.addWidget(view)
            panes.addWidget(w)

        upper = QWidget()
        lu = QVBoxLayout(upper)
        head = QHBoxLayout()
        head.addWidget(self.counts, 1)
        head.addWidget(self.show_decided)
        lu.addLayout(head)
        lu.addWidget(self.list)
        lu.addWidget(self.term_row)
        lu.addLayout(cand_row)
        lu.addLayout(actions)
        lower = QWidget()
        ll = QVBoxLayout(lower)
        ll.addLayout(compare_head)
        ll.addWidget(note)
        ll.addWidget(panes)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(upper)
        split.addWidget(lower)

        back = QPushButton("③ 変換設定に戻る")
        back.clicked.connect(self.back_requested.emit)
        self.next_btn = QPushButton("⑤ 検証・出力へ")
        self.next_btn.clicked.connect(self.next_requested.emit)
        bottom = QHBoxLayout()
        bottom.addWidget(back)
        bottom.addStretch()
        bottom.addWidget(self.next_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)
        layout.addLayout(bottom)
        MASK.changed.connect(lambda _: self.refresh())

    def set_session(self, session: Session | None) -> None:
        self.session = session
        self.refresh(reload_units=True)

    # ---------------------------------------------------------------- 要確認

    def refresh(self, *_, reload_units: bool = False) -> None:
        s = self.session
        run = s.run_id if s else None
        if not s or run is None or s.wb.state(run).value == "draft":
            self.items = []
            self.list.setRowCount(0)
            self.counts.setText("計画がまだ作られていません（③で設定を確定してください）。")
            self.next_btn.setEnabled(False)
            return
        open_items = s.wb.reviews(run)
        counts = Counter(REVIEW_KINDS[i.kind.value] for i in open_items)
        text = "、".join(f"{k} {v}件" for k, v in counts.items()) or "なし"
        self.counts.setText(f"要確認（未判断）：{text}")
        self.items = s.wb.reviews(run, state=self.show_decided.currentData())
        self.list.setRowCount(len(self.items))
        for i, item in enumerate(self.items):
            decision = ""
            if item.decision:
                decision = DECISIONS.get(item.decision.get("decision"), "")
                if item.decision.get("remembered"):
                    decision += "（前回の判断）"
            for j, v in enumerate([REVIEW_KINDS[item.kind.value], describe_item(item), decision]):
                self.list.setItem(i, j, QTableWidgetItem(v))
        self.next_btn.setEnabled(not open_items)
        if reload_units or self.unit.count() == 0:
            self._load_units()
        else:
            self._load_compare()
        self._item_selected()

    def _current(self) -> ReviewItem | None:
        i = self.list.currentRow()
        return self.items[i] if 0 <= i < len(self.items) else None

    def _entity_types(self) -> list[tuple[str, str]]:
        if not self.session:
            return []
        return [tuple(r) for r in self.session.project.conn.execute(
            "SELECT id, display_name FROM entity_type ORDER BY rowid")]

    def _kind_changed(self) -> None:
        """候補の種類を変えたら、紐付け先（既存の対象）の一覧と押せる判断を変える。"""
        item = self._current()
        if item is None or item.kind is not ReviewKind.FREETEXT_CANDIDATE:
            return
        kind = self.kind_combo.currentData()
        is_value = kind in VALUE_TYPES
        open_ = item.state == "open"
        self.buttons[Decision.REGISTER_NEW].setVisible(open_ and not is_value)
        self.buttons[Decision.LINK_EXISTING].setVisible(open_ and not is_value)
        self.buttons[Decision.REGISTER_VALUE].setVisible(open_ and is_value)
        self.candidate.clear()
        if not is_value and self.session:
            for eid, masked in self.session.project.conn.execute(
                "SELECT id, masked_name FROM entity WHERE entity_type_id = ? AND status IN ('draft', 'confirmed') "
                "ORDER BY name_seq", (kind,)
            ):
                self.candidate.addItem(masked, eid)

    def _item_selected(self) -> None:
        item = self._current()
        allowed = ALLOWED[item.kind] if item and item.state == "open" else set()
        for decision, b in self.buttons.items():
            b.setVisible(decision in allowed)
        self.forget.setVisible(bool(item and item.decision and item.decision.get("remembered")))
        self.candidate.clear()
        is_candidate = item is not None and item.kind is ReviewKind.FREETEXT_CANDIDATE
        self.term_row.setVisible(is_candidate)
        self.candidate_label.setText(
            "置換先" if item is not None and item.kind in (ReviewKind.FREETEXT_SHORT, ReviewKind.FREETEXT_AMBIGUOUS)
            else "紐付ける既存の対象")
        if item is None:
            return
        if is_candidate:
            s = item.subject
            self.term_edit.setText(MASK.show(s.get("text", "")))
            self.term_edit.setEnabled(not MASK.masked)
            self.kind_combo.blockSignals(True)
            self.kind_combo.clear()
            for key, label in self._entity_types():
                self.kind_combo.addItem(label, key)
            for key, label in VALUE_TYPES.items():
                self.kind_combo.addItem(label, key)
            suggested = s.get("info_type") or s.get("entity_type")
            self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(suggested)))
            self.kind_combo.blockSignals(False)
            self._kind_changed()
            return
        for c in item.subject.get("candidates", []):
            if "key" in c:
                self.candidate.addItem(f"{c['masked_name']}（{c['label']}）", c["key"])
            else:
                self.candidate.addItem(c["masked_name"], c["entity_id"])
        if item.kind is ReviewKind.NEW_ENTITY and self.session:
            for eid, masked in self.session.project.conn.execute(
                "SELECT id, masked_name FROM entity WHERE entity_type_id = ? AND status = 'confirmed' "
                "ORDER BY name_seq", (item.subject["entity_type"],)
            ):
                self.candidate.addItem(masked, eid)

    def _decide(self, decision: Decision) -> None:
        item = self._current()
        if not item or not self.session:
            return
        entity_id = self.candidate.currentData() if decision is Decision.LINK_EXISTING else None
        if decision is Decision.LINK_EXISTING and not entity_id:
            return
        options = {}
        if decision is Decision.REPLACE_TERM:
            options["target"] = self.candidate.currentData()
        if item.kind is ReviewKind.FREETEXT_CANDIDATE:
            kind = self.kind_combo.currentData()
            if decision is Decision.REGISTER_NEW:
                options["entity_type"] = kind
            elif decision is Decision.REGISTER_VALUE:
                options["info_type"] = kind
            if not MASK.masked:
                options["text"] = self.term_edit.text()
        run_safely(self, self.session.wb.decide, item.id, decision, entity_id, **options)
        self._replan()

    def _register_values(self) -> None:
        if self.session and self.session.run_id:
            run_safely(self, self.session.wb.decide_all, self.session.run_id, ReviewKind.FREETEXT_CANDIDATE,
                       Decision.REGISTER_VALUE)
            self._replan()

    def _forget(self) -> None:
        item = self._current()
        if item and self.session:
            run_safely(self, self.session.wb.forget_decision, item.id)
            self.refresh()

    def _register_all(self) -> None:
        if self.session and self.session.run_id:
            run_safely(self, self.session.wb.decide_all, self.session.run_id, ReviewKind.NEW_ENTITY,
                       Decision.REGISTER_NEW)
            self._replan()

    def _replan(self) -> None:
        """判断を反映して計画を作り直す（判断で新しい要確認が出ることがある）。"""
        s = self.session
        if s and s.run_id and s.wb.state(s.run_id).value == "planned":
            run_safely(self, s.wb.plan, s.run_id)
        self.refresh()
        self.changed.emit()

    # ---------------------------------------------------------------- 比較表示

    def _load_units(self) -> None:
        s = self.session
        self.unit.blockSignals(True)
        self.unit.clear()
        if s and s.run_id:
            for u in s.wb.run_units(s.run_id):
                info = s.wb.file_info(u.file_id)
                sheet = dict((i, n) for i, n, _ in info["sheets"]).get(u.sheet, "")
                label = f"{u.template_name}：{MASK.show(info['name'])}" + (f"／{MASK.show(sheet)}" if sheet else "")
                self.unit.addItem(label, (u.file_id, u.sheet))
        self.unit.blockSignals(False)
        self._load_compare()

    def _load_compare(self) -> None:
        s = self.session
        data = self.unit.currentData()
        if not s or not data or not s.run_id or s.wb.state(s.run_id).value == "draft":
            self.before_model.clear()
            self.after_model.clear()
            return
        file_id, sheet = data
        preview = run_safely(self, s.wb.preview, s.run_id, file_id, True, sheet) or []
        changes = {(p.record, p.column): p.after for p in preview}
        rows = s.rows(file_id, sheet)
        width = max((len(r) for r in rows), default=0)
        template = s.draft.templates.get(next(u.template_name for u in s.wb.run_units(s.run_id)
                                              if (u.file_id, u.sheet) == (file_id, sheet)))
        header = template.header_row if template else 0
        if self.filter.currentData() == "changed":
            changed_rows = sorted({r for r, _ in changes} | {header})
        else:
            changed_rows = list(range(len(rows)))

        def before(r: int, c: int) -> str:
            row = rows[r]
            return row[c] if c < len(row) else ""

        def after(r: int, c: int) -> str:
            if (r, c) in changes:
                v = changes[(r, c)]
                return "（未解決）" if v is None else v
            return before(r, c)

        def background(r: int, c: int):
            if (r, c) in changes:
                return CLEARED_BG if changes[(r, c)] == "" else CHANGED_BG
            return HEADER_BG if r == header else None

        self.before_model.set_data(changed_rows, width, before, background)
        # 変換後の表は、置換したセル（生成した値）を伏せない
        self.after_model.set_data(changed_rows, width, after, background, original=lambda r, c: (r, c) not in changes)
