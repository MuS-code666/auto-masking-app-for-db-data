"""表記の正規化（設計書 05 章 2.4）。照合候補の提示と残存検査にだけ使い、元の値は変えない。"""

from __future__ import annotations

import re
import unicodedata

# NFKC 後の表記（㈱ → (株)、全角括弧 → 半角括弧）
_LEGAL_FORMS = [
    "株式会社", "有限会社", "合同会社", "合資会社", "合名会社",
    "(株)", "(有)", "(同)", "(資)", "(名)",
]
_DASHES = re.compile("[\\-‐‑‒–—―−ーｰ]")
_SPACES = re.compile(r"\s+")


def normalize_for_search(text: str) -> str:
    """残存検査用：NFKC・大小統一・空白除去。"""
    return _SPACES.sub("", unicodedata.normalize("NFKC", text).casefold())


def normalize_name(text: str) -> str:
    """似た名称の候補用：残存検査用の正規化に加え、法人格表記を除き長音・ハイフン類を統一する。"""
    t = unicodedata.normalize("NFKC", text)
    for form in _LEGAL_FORMS:
        t = t.replace(form, "")
    t = _DASHES.sub("ー", t)
    return _SPACES.sub("", t.casefold())
