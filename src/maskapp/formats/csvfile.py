"""CSV の読込・書込（設計書 06 章 5）。

- 全フィールドを文字列のまま扱い、型変換をしない。
- 文字コード・BOM・区切り文字・改行・引用符の有無を記録する。
- 書込では、変更のないレコードとフィールドは元のバイト列をそのまま出力し、
  変更したフィールドだけを組み立て直す（バイト一致はしないが差分は置換箇所に限られる）。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

BOMS = [
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
]
FALLBACK_ENCODINGS = ["utf-8", "cp932"]
DELIMITER_CANDIDATES = [",", "\t", ";", "|"]
QUOTE = '"'
_SNIFF_CHARS = 64 * 1024


class CsvFormatError(ValueError):
    """CSV として読めない、または安全に書き戻せない。"""


class UnencodableValueError(CsvFormatError):
    """置換後の値が元の文字コードで表現できない。"""


@dataclass(frozen=True)
class Dialect:
    encoding: str
    bom: bool
    delimiter: str
    newline: str
    mixed_newlines: bool
    trailing_newline: bool

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Dialect:
        return cls(**data)


@dataclass(frozen=True)
class Field:
    value: str
    quoted: bool
    start: int  # 元テキスト上の位置（引用符を含む）
    end: int
    malformed: bool = False  # 閉じ引用符の後に文字が続いていた等


@dataclass(frozen=True)
class Record:
    fields: list[Field]
    start: int
    end: int  # 改行を含まない
    terminator: str  # "\r\n" / "\n" / "\r" / ""（ファイル末尾）

    @property
    def values(self) -> list[str]:
        return [f.value for f in self.fields]

    @property
    def is_blank(self) -> bool:
        return len(self.fields) == 1 and self.fields[0].value == "" and not self.fields[0].quoted


class CsvDocument:
    """読み込んだ CSV。元のバイト列と、各境界のバイト位置を保持する。"""

    def __init__(self, data: bytes, dialect: Dialect, text: str, records: list[Record]) -> None:
        self.data = data
        self.dialect = dialect
        self.text = text
        self.records = records
        self._bom_len = _bom_length(data, dialect)
        self._byte_at = _byte_offsets(text, records, dialect.encoding, len(data) - self._bom_len)

    def values(self) -> list[list[str]]:
        return [r.values for r in self.records]

    def _slice(self, start: int, end: int) -> bytes:
        return self.data[self._bom_len + self._byte_at[start] : self._bom_len + self._byte_at[end]]

    def serialize(self, changes: dict[tuple[int, int], str]) -> bytes:
        """changes[(レコード番号, フィールド番号)] = 新しい値 を反映したバイト列を返す。"""
        by_record: dict[int, dict[int, str]] = {}
        for (r, c), value in changes.items():
            if r >= len(self.records) or c >= len(self.records[r].fields):
                raise CsvFormatError("change refers to a cell that does not exist")
            by_record.setdefault(r, {})[c] = value

        enc = self.dialect.encoding
        delimiter = self.dialect.delimiter.encode(enc)
        out = bytearray(self.data[: self._bom_len])
        for r, record in enumerate(self.records):
            term_end = record.end + len(record.terminator)
            record_changes = by_record.get(r)
            if not record_changes:
                out += self._slice(record.start, term_end)
                continue
            for c, f in enumerate(record.fields):
                if c:
                    out += delimiter
                if c in record_changes:
                    text = quote_value(record_changes[c], f.quoted, self.dialect.delimiter)
                    try:
                        out += text.encode(enc)
                    except UnicodeEncodeError as exc:
                        raise UnencodableValueError(
                            f"value for record {r} field {c} cannot be encoded as {enc}"
                        ) from exc
                else:
                    out += self._slice(f.start, f.end)
            out += self._slice(record.end, term_end)
        return bytes(out)


def quote_value(value: str, was_quoted: bool, delimiter: str) -> str:
    needs = was_quoted or any(ch in value for ch in (delimiter, QUOTE, "\r", "\n"))
    if not needs:
        return value
    return QUOTE + value.replace(QUOTE, QUOTE * 2) + QUOTE


# ---------------------------------------------------------------- 読込


def read_csv(data: bytes, encoding: str | None = None, delimiter: str | None = None) -> CsvDocument:
    enc, bom = _detect_encoding(data, encoding)
    body = data[len(_bom_bytes(enc)) :] if bom else data
    try:
        text = body.decode(enc)
    except UnicodeDecodeError as exc:
        raise CsvFormatError(f"file cannot be decoded as {enc}") from exc
    if delimiter is None:
        delimiter = _detect_delimiter(text)
    records = parse_text(text, delimiter)
    terminators = Counter(r.terminator for r in records if r.terminator)
    newline = terminators.most_common(1)[0][0] if terminators else "\r\n"
    dialect = Dialect(
        encoding=enc,
        bom=bom,
        delimiter=delimiter,
        newline=newline,
        mixed_newlines=len(terminators) > 1,
        trailing_newline=bool(records) and records[-1].terminator != "",
    )
    return CsvDocument(data, dialect, text, records)


def _bom_bytes(encoding: str) -> bytes:
    for bom, enc in BOMS:
        if enc == encoding:
            return bom
    return b""


def _bom_length(data: bytes, dialect: Dialect) -> int:
    return len(_bom_bytes(dialect.encoding)) if dialect.bom else 0


def _detect_encoding(data: bytes, override: str | None) -> tuple[str, bool]:
    for bom, enc in BOMS:
        if data.startswith(bom):
            if override and override.replace("_", "-").lower() not in (enc, "utf-8-sig"):
                raise CsvFormatError("specified encoding contradicts the byte order mark")
            return enc, True
    if override:
        return override, False
    for enc in FALLBACK_ENCODINGS:
        try:
            data.decode(enc)
        except UnicodeDecodeError:
            continue
        return enc, False
    raise CsvFormatError("file encoding could not be determined (tried UTF-8 and CP932)")


def _detect_delimiter(text: str) -> str:
    sample = text[:_SNIFF_CHARS]
    if len(text) > _SNIFF_CHARS:
        cut = max(sample.rfind("\n"), sample.rfind("\r"))
        sample = sample[: cut + 1] if cut > 0 else sample
    best, best_score = ",", (-1, -1)
    for cand in DELIMITER_CANDIDATES:
        try:
            records = parse_text(sample, cand)
        except CsvFormatError:
            continue
        counts = [len(r.fields) for r in records if not r.is_blank]
        if not counts:
            continue
        mode, freq = Counter(counts).most_common(1)[0]
        if mode < 2:
            continue
        score = (freq * 1000 // len(counts), mode)  # 一貫性を優先し、次に列数
        if score > best_score:
            best, best_score = cand, score
    return best


def parse_text(text: str, delimiter: str) -> list[Record]:
    if len(delimiter) != 1 or delimiter in ('"', "\r", "\n"):
        raise CsvFormatError("delimiter must be a single character other than quote or newline")
    unquoted = re.compile(f"[^{re.escape(delimiter)}\r\n]*")
    quoted = re.compile(r'"((?:[^"]|"")*)"')
    records: list[Record] = []
    pos, n = 0, len(text)
    while pos < n:
        record_start = pos
        fields: list[Field] = []
        while True:
            if pos < n and text[pos] == QUOTE:
                m = quoted.match(text, pos)
                if m is None:
                    raise CsvFormatError(f"unterminated quoted field in record {len(records)}")
                value, end, malformed = m.group(1).replace('""', '"'), m.end(), False
                if end < n and text[end] not in (delimiter, "\r", "\n"):
                    rest = unquoted.match(text, end)
                    value, end, malformed = value + rest.group(0), rest.end(), True
                fields.append(Field(value, True, pos, end, malformed))
            else:
                m = unquoted.match(text, pos)
                end = m.end()
                fields.append(Field(text[pos:end], False, pos, end))
            pos = end
            if pos < n and text[pos] == delimiter:
                pos += 1
                continue
            break
        record_end = pos
        if text.startswith("\r\n", pos):
            terminator = "\r\n"
        elif pos < n and text[pos] in "\r\n":
            terminator = text[pos]
        else:
            terminator = ""
        pos += len(terminator)
        records.append(Record(fields, record_start, record_end, terminator))
    return records


def _byte_offsets(text: str, records: list[Record], encoding: str, body_len: int) -> dict[int, int]:
    """境界の文字位置 → 本文のバイト位置。

    区間ごとに符号化した長さを積み上げる。CP932 の重複符号（NEC/IBM 拡張）のように
    再符号化でバイト列が変わる文字があっても長さは変わらないため、位置は正しく求まる。
    """
    points = {0, len(text)}
    for r in records:
        points.add(r.start)
        points.add(r.end)
        points.add(r.end + len(r.terminator))
        for f in r.fields:
            points.add(f.start)
            points.add(f.end)
    offsets: dict[int, int] = {}
    total, prev = 0, 0
    for p in sorted(points):
        total += len(text[prev:p].encode(encoding))
        offsets[p] = total
        prev = p
    if total != body_len:
        raise CsvFormatError("file cannot be reproduced byte-for-byte with its encoding")
    return offsets
