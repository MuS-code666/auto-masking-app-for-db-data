"""変換後コードの生成規則（設計書 05 章 3.2）。

観測した元コードの形式から規則を作る。
- 数字のみ: 同じ桁数の数字。元コードと重ならない範囲（既定は先頭 9 から）で連番
- 英字の接頭語＋数字: 同じ長さ。接頭語を別の文字（X、元が X なら Z）にした連番
- その他: "Z" ＋ 数字の連番
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass

_PREFIXED = re.compile(r"^([A-Za-z]+)(\d+)$")


class CodeExhaustedError(RuntimeError):
    """コード体系で生成できる変換後コードが尽きた。"""


@dataclass(frozen=True)
class OutputRule:
    kind: str  # "numeric" / "prefixed" / "generic"
    length: int = 0  # numeric の桁数
    start: int = 0  # numeric の開始値
    prefix: str = ""
    digits: int = 0  # prefixed / generic の数字部の桁数

    @property
    def capacity(self) -> int:
        if self.kind == "numeric":
            return 10**self.length - self.start
        return 10**self.digits - 1

    def make(self, seq: int) -> str:
        """seq 番目（1 起点）の変換後コード。"""
        if seq < 1 or seq > self.capacity:
            raise CodeExhaustedError("no more codes available in this code scheme")
        if self.kind == "numeric":
            return str(self.start + seq - 1).zfill(self.length)
        return f"{self.prefix}{seq:0{self.digits}d}"

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> OutputRule | None:
        data = json.loads(text or "{}")
        return cls(**data) if data else None


def derive_rule(observed: Iterable[str]) -> OutputRule:
    codes = [c for c in observed if c]
    if codes and all(c.isascii() and c.isdigit() for c in codes):
        length = max(len(c) for c in codes)
        start = 9 * 10 ** (length - 1)
        top = max(int(c) for c in codes)
        if top >= start:
            start = top + 1
        if length == 1 or start >= 10**length:
            length += 1  # 同じ桁数で重ならない範囲が取れない
            start = 9 * 10 ** (length - 1)
        return OutputRule("numeric", length=length, start=start)
    matches = [_PREFIXED.match(c) for c in codes]
    if codes and all(matches):
        prefixes = {m.group(1) for m in matches}
        if len(prefixes) == 1:
            prefix = prefixes.pop()
            new_prefix = ("Z" if prefix.upper() == "X" * len(prefix) else "X") * len(prefix)
            digits = max(len(m.group(2)) for m in matches)
            return OutputRule("prefixed", prefix=new_prefix, digits=digits)
    width = max((len(c) for c in codes), default=6)
    return OutputRule("generic", prefix="Z", digits=max(width - 1, 4))
