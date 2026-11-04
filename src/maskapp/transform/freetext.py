"""文章内の語句の検出と置換（設計書 05 章 4.1〜4.2）。

辞書（対応表の元名称・別名・元コード・値対応の元値）の全語句を Aho–Corasick 法で一度に検索し、
左から順に、重なる場合は最長一致を優先して重ならない一致を選ぶ。置換は元の文字列の位置から一度だけ
組み立てるため、置換後の文字列（顧客0001 等）を再び検索して置換することはない。

照合は正規化した表記（NFKC・大小統一・空白除去）同士で行い、一致した範囲を元の文字列の位置に戻す。
このため全角半角や空白の違いがあっても置換できる（残存検査と同じ正規化）。

自動で置換しないもの（要確認）:
- 短い語（2文字以下、数字のみ4桁以下）：人が「置換する／問題なし」を決める。判断は列（範囲）ごと
- 置換先が複数ある語（顧客と製品で同じ名称、同名の別顧客）：同じ行の対象で1つに決まればそれを使い、
  決まらなければ人が置換先を選ぶ。判断は列（範囲）ごと
- 規則で検出した候補（電話番号・メール・郵便番号・URL・法人格付きの語・敬称付きの語・担当者名らしい語）と、
  AI（端末内の LLM）が検査済みの文章で示した候補：人が「対象・値として登録して置換／問題なし」を決める。
  判断は語句ごと（どの列でも同じ）。AI の候補は、保存済みの検査結果から作る（計画の作成時に AI は動かさない）

すべての判断は要確認項目（review_item）として記録し、確定した作業の判断は次の作業に引き継ぐ。
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

import ahocorasick

from maskapp.entity.normalize import normalize_for_search
from maskapp.llm.store import load_results, text_hash
from maskapp.llm.tasks import NOT_HONORIFIC, TYPE_TO_ENTITY, AiCandidate
from maskapp.transform import reviews
from maskapp.transform.reviews import Decision, ReviewItem, ReviewKind
from maskapp.verify.residual import is_short

_ACTIVE = "('draft', 'confirmed')"
_CONTEXT = 40  # 要確認に添える前後の文字数


@dataclass(frozen=True)
class Target:
    """語句の置換先。key は判断の記録に使う識別（対象の番号が変わっても同じ）。"""

    key: str  # "name:<entity_id>" / "code:<scheme_id>:<元コード>" / "value:<種類>:<元値>"
    value: str  # 置換後の表記
    entity_id: str | None
    label: str  # 画面表示用の種類名


@dataclass(frozen=True)
class Term:
    norm: str
    targets: tuple[Target, ...]
    code_only: bool  # すべてコード → 前後が英数字でない場合だけ一致とする

    @property
    def short(self) -> bool:
        return is_short(self.norm)


@dataclass(frozen=True)
class Match:
    start: int  # 元の文字列での位置
    end: int
    term: Term


def normalize_with_positions(text: str) -> tuple[str, list[int]]:
    """残存検査と同じ正規化を1文字ずつ行い、正規化後の各文字が元の何文字目から来たかを返す。"""
    out: list[str] = []
    pos: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        for x in unicodedata.normalize("NFKC", ch).casefold():
            if not x.isspace():
                out.append(x)
                pos.append(i)
    return "".join(out), pos


def _is_word_char(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


class Dictionary:
    """対応表から作る語句辞書（設計書 05 章 4.1）。"""

    def __init__(self, entries: list[tuple[str, Target, bool]]) -> None:
        targets: dict[str, dict[str, Target]] = {}
        codes: dict[str, bool] = {}
        for text, target, is_code in entries:
            norm = normalize_for_search(text)
            if not norm:
                continue
            targets.setdefault(norm, {})[target.key] = target
            codes[norm] = codes.get(norm, True) and is_code
        self.terms = {
            norm: Term(norm, tuple(sorted(ts.values(), key=lambda t: (t.label, t.value))), codes[norm])
            for norm, ts in targets.items()
        }
        self._automaton = None
        if self.terms:
            automaton = ahocorasick.Automaton()
            for norm, term in self.terms.items():
                automaton.add_word(norm, term)
            automaton.make_automaton()
            self._automaton = automaton

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> Dictionary:
        entries: list[tuple[str, Target, bool]] = []
        for name, eid, masked, label in conn.execute(
            f"SELECT n.original_name, e.id, e.masked_name, t.display_name FROM entity_name n "
            f"JOIN entity e ON e.id = n.entity_id JOIN entity_type t ON t.id = e.entity_type_id "
            f"WHERE n.status IN {_ACTIVE} AND e.status IN {_ACTIVE}"
        ):
            entries.append((name, Target(f"name:{eid}", masked, eid, label), False))
        for code, masked, eid, scheme_id, scheme in conn.execute(
            f"SELECT c.original_code, c.masked_code, c.entity_id, c.code_scheme_id, s.display_name "
            f"FROM entity_code c JOIN code_scheme s ON s.id = c.code_scheme_id "
            f"JOIN entity e ON e.id = c.entity_id WHERE c.status IN {_ACTIVE} AND e.status IN {_ACTIVE}"
        ):
            entries.append((code, Target(f"code:{scheme_id}:{code}", masked, eid, scheme), True))
        for value, masked, info_type in conn.execute(
            f"SELECT original_value, masked_value, info_type FROM value_token WHERE status IN {_ACTIVE}"
        ):
            entries.append((value, Target(f"value:{info_type}:{value}", masked, None, info_type), False))
        return cls(entries)

    def find(self, text: str) -> list[Match]:
        """重ならない一致（左から順に、重なる場合は最長一致を優先）。"""
        if self._automaton is None or not text:
            return []
        norm, pos = normalize_with_positions(text)
        found: list[tuple[int, int, Term]] = []
        for end, term in self._automaton.iter(norm):
            start = end - len(term.norm) + 1
            if term.code_only:
                before = norm[start - 1] if start > 0 else ""
                after = norm[end + 1] if end + 1 < len(norm) else ""
                if (before and _is_word_char(before)) or (after and _is_word_char(after)):
                    continue
            found.append((start, end, term))
        found.sort(key=lambda m: (m[0], -(m[1] - m[0])))
        matches: list[Match] = []
        last = -1
        for start, end, term in found:
            if start <= last:
                continue
            matches.append(Match(pos[start], pos[end] + 1, term))
            last = end
        return matches


# ---------------------------------------------------------------- 規則による候補の検出


@dataclass(frozen=True)
class Candidate:
    start: int
    end: int
    pattern: str  # phone / email / postal / url / company / honorific / person_label
    suggest: dict  # {"info_type": ...} または {"entity_type": ...}

    def text(self, source: str) -> str:
        return source[self.start:self.end]


_STOP = set(" \t\r\n　、。，．,.:：;；!！?？()（）「」『』【】[]［］〔〕<>＜＞\"'“”‘’/／|｜=＝・")
_LEGAL = ("株式会社", "有限会社", "合同会社", "合資会社", "合名会社")
_LEGAL_ABBR = ("(株)", "（株）", "㈱", "(有)", "（有）", "㈲")
_HONORIFICS = ("御中", "様", "殿")
_FOLLOWERS = ("御中", "様", "殿", "さん", "より", "から", "宛て", "宛", "向け", "まで", "へ")
_COMMON_BEFORE_HONORIFIC = {
    "お客", "客", "皆", "皆々", "各位", "先方", "担当者", "ご担当者", "ご担当", "担当", "貴社", "御社", "お得意",
    "得意先", "仕入先", "取引先", "顧客", "業者", "関係者", "お取引先", "ご依頼主", "依頼主",
}
_MAX_NAME = 20
_PATTERNS: list[tuple[str, re.Pattern, dict]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+"), {"info_type": "EMAIL"}),
    ("url", re.compile(r"(?:https?://|www\.)[^\s　<>\"'()（）「」]+"), {"info_type": "URL"}),
    ("phone", re.compile(
        r"(?<![0-9０-９\-－])(?:[0０][0-9０-９]{1,4}[-－‐(（][0-9０-９]{1,4}[-－‐)）][0-9０-９]{3,4}"
        r"|[0０][5789５７８９][0０][0-9０-９]{8})(?![0-9０-９\-－])"), {"info_type": "PHONE"}),
    ("postal", re.compile(
        r"〒\s*[0-9０-９]{3}[-－]?[0-9０-９]{4}|(?<![0-9０-９\-－])[0-9０-９]{3}[-－][0-9０-９]{4}(?![0-9０-９\-－])"),
     {"info_type": "POSTAL"}),
]
_PERSON_LABEL = re.compile(r"担当(?:者)?\s*[:：]\s*")


def _run_forward(text: str, start: int, covered: list[bool], limit: int = _MAX_NAME) -> int:
    end = start
    while end < len(text) and end - start < limit and text[end] not in _STOP and not covered[end]:
        end += 1
    # 敬称・助詞の手前で切る（「〇〇様より」→「〇〇」）
    for word in _FOLLOWERS:
        i = text.find(word, start, end)
        if i != -1:
            end = i
    return end


def _run_backward(text: str, end: int, covered: list[bool], limit: int = _MAX_NAME) -> int:
    start = end
    while (start > 0 and end - start < limit and text[start - 1] not in _STOP and not covered[start - 1]
           and not any(text.endswith(h, 0, start) for h in _HONORIFICS)):
        start -= 1
    return start


def _in_common_word(text: str, i: int, word: str) -> bool:
    """text[i:] の word が「同様」「仕様」等の一般的な語の一部か。"""
    for common in NOT_HONORIFIC:
        offset = common.find(word)
        if offset != -1 and i >= offset and text.startswith(common, i - offset):
            return True
    return False


def detect_candidates(text: str, covered: list[bool]) -> list[Candidate]:
    """規則による候補（設計書 05 章 4.2）。covered は辞書で置換する文字（候補から除く）。"""
    found: list[Candidate] = []
    for pattern, regex, suggest in _PATTERNS:
        for m in regex.finditer(text):
            found.append(Candidate(m.start(), m.end(), pattern, suggest))
    # 法人格付きの語
    for form in _LEGAL + _LEGAL_ABBR:
        i = text.find(form)
        while i != -1:
            after = _run_forward(text, i + len(form), covered)
            before = _run_backward(text, i, covered)
            if after > i + len(form):
                found.append(Candidate(i, after, "company", {"entity_type": "CUSTOMER"}))
            elif before < i:
                found.append(Candidate(before, i + len(form), "company", {"entity_type": "CUSTOMER"}))
            i = text.find(form, i + len(form))
    # 敬称付きの語（「〇〇様」「〇〇御中」）
    for word in _HONORIFICS:
        i = text.find(word)
        while i != -1:
            if _in_common_word(text, i, word):
                i = text.find(word, i + len(word))
                continue
            start = _run_backward(text, i, covered)
            name = text[start:i]
            if name and name not in _COMMON_BEFORE_HONORIFIC and not name.isdigit():
                found.append(Candidate(start, i, "honorific", {"entity_type": "CUSTOMER"}))
            i = text.find(word, i + len(word))
    # 担当者名らしい語（「担当:〇〇」）
    for m in _PERSON_LABEL.finditer(text):
        end = _run_forward(text, m.end(), covered, limit=10)
        if end > m.end():
            found.append(Candidate(m.end(), end, "person_label", {"entity_type": "PERSON"}))
    # 辞書で置換する範囲と重なる候補は除き、候補どうしの重なりは先に見つけた（優先度の高い）方を残す
    accepted: list[Candidate] = []
    for cand in found:
        if any(covered[cand.start:cand.end]):
            continue
        if any(cand.start < a.end and a.start < cand.end for a in accepted):
            continue
        if not text[cand.start:cand.end].strip():
            continue
        accepted.append(cand)
    return sorted(accepted, key=lambda c: c.start)


# ---------------------------------------------------------------- 計画


@dataclass(frozen=True)
class FreeTextResult:
    value: str | None  # 置換後の文字列。None は未解決（判断待ち）
    kept: tuple[str, ...]  # 置換しなかった部分（検証で残存検査する）
    changed: bool


def _context(text: str, start: int, end: int) -> str:
    a = max(0, start - _CONTEXT)
    b = min(len(text), end + _CONTEXT)
    return ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else "")


@dataclass
class FreeTextPlanner:
    """計画の作成の間だけ使う。辞書は最初に使うときに読む（その作業で仮登録した対象を含めるため）。"""

    conn: sqlite3.Connection
    run_id: int
    _dictionary: Dictionary | None = None
    _items: dict[str, ReviewItem] = field(default_factory=dict)
    _ai: dict[str, list[AiCandidate]] | None = None
    ai_checked: set[str] = field(default_factory=set)  # AI で検査済みの文章（ハッシュ）
    ai_unchecked: set[str] = field(default_factory=set)

    @property
    def ai_results(self) -> dict[str, list[AiCandidate]]:
        if self._ai is None:
            self._ai = load_results(self.conn) if self.conn is not None else {}
        return self._ai

    def _ai_candidates(self, text: str, covered: list[bool], found: list[Candidate]) -> list[Candidate]:
        digest = text_hash(text)
        if digest not in self.ai_results:
            self.ai_unchecked.add(digest)
            return found
        self.ai_checked.add(digest)
        out = list(found)
        for ai in self.ai_results[digest]:
            suggest = {"entity_type": TYPE_TO_ENTITY[ai.type]} if ai.type in TYPE_TO_ENTITY else {}
            start = text.find(ai.text)
            while start != -1:
                end = start + len(ai.text)
                if not any(covered[start:end]) and not any(start < c.end and c.start < end for c in out):
                    out.append(Candidate(start, end, "ai", suggest))
                start = text.find(ai.text, end)
        return sorted(out, key=lambda c: c.start)

    @property
    def dictionary(self) -> Dictionary:
        if self._dictionary is None:
            self._dictionary = Dictionary.load(self.conn)
        return self._dictionary

    def _review(self, kind: ReviewKind, key: str, subject: dict, location: dict) -> ReviewItem:
        item_key = f"{kind.value}:{key}"
        if item_key not in self._items:
            self._items[item_key] = reviews.upsert(self.conn, self.run_id, kind, key, subject, location)
        return self._items[item_key]

    def _reopen(self, kind: ReviewKind, key: str) -> None:
        """判断で選んだ置換先がなくなった（取消・統合された）場合は、判断し直す。"""
        item_key = f"{kind.value}:{key}"
        item = self._items[item_key]
        reviews.forget(self.conn, item.id)
        self._items[item_key] = reviews.get(self.conn, item.id)

    def _term_value(self, m: Match, text: str, scope: str, scope_label: str, location: dict,
                    context: frozenset[str]) -> tuple[str | None, bool]:
        """(置換後の値, 判断待ちか)。値が None で判断待ちでなければ置換しない。"""
        term = m.term
        targets = term.targets
        if len(targets) > 1:
            in_row = [t for t in targets if t.entity_id and t.entity_id in context]
            if len(in_row) == 1 and not term.short:
                return in_row[0].value, False  # 同じ行の対象で決まる
        if len(targets) == 1 and not term.short:
            return targets[0].value, False
        kind = ReviewKind.FREETEXT_SHORT if term.short else ReviewKind.FREETEXT_AMBIGUOUS
        subject = {
            "term": text[m.start:m.end], "norm": term.norm, "scope": scope_label,
            "candidates": [{"key": t.key, "masked_name": t.value, "label": t.label, "entity_id": t.entity_id}
                           for t in targets],
            "context": _context(text, m.start, m.end),
        }
        key = f"{scope}:{term.norm}"
        item = self._review(kind, key, subject, location)
        decision = item.decision
        if decision is None:
            return None, True
        if decision.get("decision") == Decision.FALSE_POSITIVE:
            return None, False
        chosen = next((t for t in targets if t.key == decision.get("target")), None)
        if chosen is None:
            self._reopen(kind, key)
            return None, True
        return chosen.value, False

    def process(self, text: str, scope: str, scope_label: str, location: dict,
                context: frozenset[str] = frozenset()) -> FreeTextResult | None:
        """1つの文字列を処理する。空の文字列は None、それ以外は変更がなくても結果（置換しなかった部分）を返す。"""
        if not text or not text.strip():
            return None
        matches = self.dictionary.find(text)
        covered = [False] * len(text)
        for m in matches:
            if not m.term.short:
                for i in range(m.start, m.end):
                    covered[i] = True
        candidates = self._ai_candidates(text, covered, detect_candidates(text, covered))
        # 候補（電話番号等）の中にある短い語は、候補として判断する
        matches = [m for m in matches if not (m.term.short and any(
            m.start < c.end and c.start < m.end for c in candidates))]

        pieces: list[tuple[int, int, str | None]] = []  # (開始, 終了, 置換後) 置換しない語は含めない
        pending = False
        for m in matches:
            value, waiting = self._term_value(m, text, scope, scope_label, location, context)
            pending = pending or waiting
            if value is not None:
                pieces.append((m.start, m.end, value))
        for cand in candidates:
            cand_text = cand.text(text)
            item = self._review(
                ReviewKind.FREETEXT_CANDIDATE, cand_text,
                {"text": cand_text, "pattern": cand.pattern, **cand.suggest, "scope": scope_label,
                 "context": _context(text, cand.start, cand.end)},
                location,
            )
            decision = item.decision
            if decision is None:
                pending = True
            elif decision.get("decision") != Decision.FALSE_POSITIVE:
                # 登録した候補：登録した語句の置換先で置換する（長い語句は辞書の一致として置換済み）
                term = self.dictionary.terms.get(normalize_for_search(cand_text))
                target = next((t for t in term.targets if t.key == decision.get("target")), None) if term else None
                if target is None:
                    self._reopen(ReviewKind.FREETEXT_CANDIDATE, cand_text)
                    pending = True
                else:
                    pieces.append((cand.start, cand.end, target.value))
        pieces.sort()
        out: list[str] = []
        kept: list[str] = []
        pos = 0
        for start, end, value in pieces:
            out.append(text[pos:start])
            kept.append(text[pos:start])
            out.append(value)
            pos = end
        out.append(text[pos:])
        kept.append(text[pos:])
        new_text = "".join(out)
        changed = new_text != text
        return FreeTextResult(None if pending else new_text, tuple(k for k in kept if k), changed)
