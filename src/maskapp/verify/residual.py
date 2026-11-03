"""元の語句の残存検査（設計書 07 章 1.2）。

辞書の全語句を Aho–Corasick 法で一度に検索する。検索は正規化した表記（NFKC・大小統一・
空白除去）同士で行うため、全角半角や空白の違いがあっても検出できる。
コードは前後が英数字でない場合だけ一致とする（"123" が "1234" の一部に一致しないように）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

import ahocorasick

from maskapp.entity.normalize import normalize_for_search


class TermKind(StrEnum):
    NAME = "name"
    CODE = "code"
    VALUE = "value"


@dataclass(frozen=True)
class Hit:
    term: str  # 正規化後の語句
    kinds: frozenset[TermKind]
    short: bool


def is_short(normalized: str) -> bool:
    """自動判定しない短い語（設計書 10 章 未決6：2文字以下、数字のみ4桁以下）。"""
    return len(normalized) <= 2 or (normalized.isdigit() and len(normalized) <= 4)


def _is_word_char(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


class ResidualScanner:
    def __init__(self, terms: Iterable[tuple[str, TermKind]]) -> None:
        kinds: dict[str, set[TermKind]] = {}
        for text, kind in terms:
            norm = normalize_for_search(text)
            if norm:
                kinds.setdefault(norm, set()).add(kind)
        self._automaton = None
        if kinds:
            automaton = ahocorasick.Automaton()
            for norm, ks in kinds.items():
                automaton.add_word(norm, Hit(norm, frozenset(ks), is_short(norm)))
            automaton.make_automaton()
            self._automaton = automaton
        self.size = len(kinds)

    def scan(self, text: str) -> list[Hit]:
        if self._automaton is None or not text:
            return []
        norm = normalize_for_search(text)
        hits = []
        for end, hit in self._automaton.iter(norm):
            start = end - len(hit.term) + 1
            if hit.kinds == {TermKind.CODE}:
                before = norm[start - 1] if start > 0 else ""
                after = norm[end + 1] if end + 1 < len(norm) else ""
                if (before and _is_word_char(before)) or (after and _is_word_char(after)):
                    continue
            hits.append(hit)
        return hits


_ACTIVE = "('draft', 'confirmed')"


def dictionary_terms(conn) -> list[tuple[str, TermKind]]:
    """残存検査の辞書：有効な対象の元名称・別名、元コード、値対応の元値。"""
    terms: list[tuple[str, TermKind]] = []
    terms += [(r[0], TermKind.NAME) for r in conn.execute(
        f"SELECT n.original_name FROM entity_name n JOIN entity e ON e.id = n.entity_id "
        f"WHERE n.status IN {_ACTIVE} AND e.status IN {_ACTIVE}"
    )]
    terms += [(r[0], TermKind.CODE) for r in conn.execute(
        f"SELECT original_code FROM entity_code WHERE status IN {_ACTIVE}"
    )]
    terms += [(r[0], TermKind.VALUE) for r in conn.execute(
        f"SELECT original_value FROM value_token WHERE status IN {_ACTIVE}"
    )]
    return terms


def replacement_map(conn) -> dict[str, set[str]]:
    """元の語句 → 置換後の値の集合。値が1つなら語句だけから置換先が決まる（数式の文字列定数等で使う）。

    同じ名称の別の対象（同名の別顧客、顧客と製品で同じ名称）があると値が複数になる。
    """
    out: dict[str, set[str]] = {}
    for original, masked in conn.execute(
        f"SELECT n.original_name, e.masked_name FROM entity_name n JOIN entity e ON e.id = n.entity_id "
        f"WHERE n.status IN {_ACTIVE} AND e.status IN {_ACTIVE}"
    ):
        out.setdefault(original, set()).add(masked)
    for original, masked in conn.execute(
        f"SELECT original_code, masked_code FROM entity_code WHERE status IN {_ACTIVE}"
    ):
        out.setdefault(original, set()).add(masked)
    for original, masked in conn.execute(
        f"SELECT original_value, masked_value FROM value_token WHERE status IN {_ACTIVE}"
    ):
        out.setdefault(original, set()).add(masked)
    return out
