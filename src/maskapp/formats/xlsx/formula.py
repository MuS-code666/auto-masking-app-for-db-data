"""Excel の数式の字句解析（設計書 06 章 4.1）。

数式全体に文字列置換をかけると、関数名・シート名・セル参照まで変わってしまうため、
字句に分けて「文字列定数」だけを置換対象にする。xlsx の中の数式は先頭の "=" を含まない。

扱う字句:
  string   "..."（"" は " 1文字）
  sheet    'シート名'! または シート名!（シート名の参照）
  func     関数名（直後が "("）。_xlfn. 等の接頭辞は除いて大文字で持つ
  external [1]Sheet!A1 のような外部ブック参照
  struct   テーブル名[列名] のような構造化参照
  error    #N/A 等
  op       & を含む演算子
  other    それ以外（セル参照・数値・名前・区切り）
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_IDENT = re.compile(r"[^\s\"'!&+\-*/^=<>(),;:{}\[\]%@#]+")
_ERROR = re.compile(r"#(NULL!|DIV/0!|VALUE!|REF!|NAME\?|NUM!|N/A|GETTING_DATA|SPILL!|CALC!|FIELD!|BLOCKED!)",
                    re.IGNORECASE)
_FUNC_PREFIXES = ("_XLFN._XLWS.", "_XLFN.", "_XLWS.", "_XLUDF.")

# 文字列を加工する関数。参照先の名称が置換されると、再計算結果がキャッシュ値と一致しないことがある
STRING_FUNCTIONS = frozenset({
    "LEFT", "LEFTB", "RIGHT", "RIGHTB", "MID", "MIDB", "CONCAT", "CONCATENATE", "TEXTJOIN", "SUBSTITUTE",
    "REPLACE", "REPLACEB", "UPPER", "LOWER", "PROPER", "TRIM", "CLEAN", "TEXT", "REPT", "ASC", "JIS", "DBCS",
    "PHONETIC", "TEXTBEFORE", "TEXTAFTER", "TEXTSPLIT", "FIND", "FINDB", "SEARCH", "SEARCHB", "LEN", "LENB",
    "VALUE", "NUMBERVALUE", "CODE", "UNICODE", "EXACT", "REGEXEXTRACT", "REGEXREPLACE", "REGEXTEST",
})


class FormulaSyntaxError(ValueError):
    """数式の字句が閉じていない（引用符・角括弧）。"""


@dataclass(frozen=True)
class Token:
    kind: str
    start: int
    end: int
    value: str  # string は復号後の文字列、sheet はシート名、func は正規化した関数名


@dataclass(frozen=True)
class FormulaInfo:
    tokens: tuple[Token, ...]

    @property
    def constants(self) -> list[Token]:
        return [t for t in self.tokens if t.kind == "string"]

    @property
    def functions(self) -> set[str]:
        return {t.value for t in self.tokens if t.kind == "func"}

    @property
    def sheets(self) -> list[str]:
        return [t.value for t in self.tokens if t.kind == "sheet"]

    @property
    def external(self) -> bool:
        return any(t.kind == "external" for t in self.tokens)

    @property
    def concatenates(self) -> bool:
        return any(t.kind == "op" and t.value == "&" for t in self.tokens)

    @property
    def manipulates_text(self) -> bool:
        return self.concatenates or bool(self.functions & STRING_FUNCTIONS)


def _normalize_function(name: str) -> str:
    upper = name.upper()
    for prefix in _FUNC_PREFIXES:
        if upper.startswith(prefix):
            return upper[len(prefix):]
    return upper


def _quoted(text: str, i: int, quote: str) -> tuple[str, int]:
    """引用符で囲まれた部分を読み、(復号後の文字列, 閉じ引用符の次の位置) を返す。"""
    out = []
    j = i + 1
    while j < len(text):
        ch = text[j]
        if ch == quote:
            if j + 1 < len(text) and text[j + 1] == quote:
                out.append(quote)
                j += 2
                continue
            return "".join(out), j + 1
        out.append(ch)
        j += 1
    raise FormulaSyntaxError("unterminated quoted text in formula")


def _bracket(text: str, i: int) -> int:
    """角括弧（入れ子あり）の終わりの次の位置を返す。"""
    depth = 0
    j = i
    while j < len(text):
        ch = text[j]
        if ch == "'" and depth:
            j += 2 if text[j + 1:j + 2] in ("[", "]", "#", "'") else 1  # 構造化参照のエスケープ
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise FormulaSyntaxError("unterminated bracket in formula")


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            value, j = _quoted(text, i, '"')
            tokens.append(Token("string", i, j, value))
        elif ch == "'":
            value, j = _quoted(text, i, "'")
            if j < n and text[j] == "!":
                if value.startswith("["):
                    tokens.append(Token("external", i, j + 1, value))
                else:
                    tokens.append(Token("sheet", i, j + 1, value))
                j += 1
            else:
                tokens.append(Token("other", i, j, value))
        elif ch == "[":
            j = _bracket(text, i)
            prev = tokens[-1] if tokens else None
            if prev is not None and prev.end == i and prev.kind in ("other", "struct"):
                tokens.append(Token("struct", i, j, text[i:j]))
            else:
                # [1]Sheet!A1 や [1]!名前 のような外部ブック参照
                tokens.append(Token("external", i, j, text[i:j]))
        elif ch == "#":
            m = _ERROR.match(text, i)
            j = m.end() if m else i + 1
            tokens.append(Token("error", i, j, text[i:j]))
        elif ch == "&":
            j = i + 1
            tokens.append(Token("op", i, j, "&"))
        else:
            m = _IDENT.match(text, i)
            if m:
                j = m.end()
                word = text[i:j]
                if j < n and text[j] == "(":
                    tokens.append(Token("func", i, j, _normalize_function(word)))
                elif j < n and text[j] == "!":
                    if tokens and tokens[-1].kind == "external" and tokens[-1].end == i:
                        tokens.append(Token("external", i, j + 1, word))
                    else:
                        tokens.append(Token("sheet", i, j + 1, word))
                    j += 1
                else:
                    tokens.append(Token("other", i, j, word))
            else:
                j = i + 1
                if not ch.isspace():
                    tokens.append(Token("op" if ch in "+-*/^=<>%:,;" else "other", i, j, ch))
        i = j
    return tokens


def analyze(text: str) -> FormulaInfo:
    return FormulaInfo(tuple(tokenize(text)))


def quote_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def replace_constants(text: str, info: FormulaInfo, replacements: dict[int, str]) -> str:
    """replacements: 文字列定数の開始位置 → 新しい値。それ以外の字句は元の文字列のまま残す。"""
    out = []
    pos = 0
    for token in info.constants:
        if token.start in replacements:
            out.append(text[pos:token.start])
            out.append(quote_string(replacements[token.start]))
            pos = token.end
    out.append(text[pos:])
    return "".join(out)
