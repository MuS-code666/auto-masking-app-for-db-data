"""CSV の読込・書込の試験（受入シナリオ S8 の形式面）。"""

from __future__ import annotations

import pytest

from maskapp.formats.csvfile import CsvFormatError, UnencodableValueError, read_csv

SAMPLE = (
    "コード,名称,金額,日付,備考\r\n"
    '000123,"株式会社さくら商事",-1200.50,2026/4/1,"改行を\n含む"\r\n'
    '000124,,0,1-2,""\r\n'
    '000125,"引用""符"",付き",12345678901234567890,,"a,b"\r\n'
)


@pytest.mark.parametrize("encoding,bom", [("cp932", False), ("utf-8", False), ("utf-8", True)])
@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_round_trip_without_changes_is_byte_identical(encoding: str, bom: bool, newline: str):
    text = SAMPLE.replace("\r\n", newline)
    data = text.encode(encoding)
    if bom:
        data = "﻿".encode(encoding) + data
    doc = read_csv(data)
    assert doc.dialect.encoding == encoding
    assert doc.dialect.bom is bom
    assert doc.dialect.newline == newline
    assert doc.serialize({}) == data


def test_values_are_never_type_converted():
    doc = read_csv(SAMPLE.encode("utf-8"))
    rows = doc.values()
    assert rows[1] == ["000123", "株式会社さくら商事", "-1200.50", "2026/4/1", "改行を\n含む"]
    assert rows[2] == ["000124", "", "0", "1-2", ""]
    assert rows[3][0:4] == ["000125", '引用"符",付き', "12345678901234567890", ""]
    # 空欄（,,）と空文字（""）を区別して保持する
    assert doc.records[2].fields[1].quoted is False
    assert doc.records[2].fields[4].quoted is True


def test_only_changed_fields_are_rewritten():
    data = SAMPLE.encode("cp932")
    doc = read_csv(data)
    out = doc.serialize({(1, 1): "顧客0001", (2, 0): "900001", (3, 1): "顧客,0002"})
    lines = out.decode("cp932").split("\r\n")
    assert lines[0] == SAMPLE.split("\r\n")[0]  # 変更のない行は同じ
    assert lines[1].startswith('000123,"顧客0001",-1200.50')  # 元が引用符付きなら引用符を保つ
    assert lines[2].startswith("900001,,0,1-2")
    assert '"顧客,0002"' in lines[3]  # 区切り文字を含む値は引用符を付ける
    again = read_csv(out)
    assert again.values()[1][4] == "改行を\n含む"
    assert len(again.records) == len(doc.records)


def test_cp932_duplicate_codes_are_preserved_byte_for_byte():
    # 0xEEEF（NEC選定IBM拡張）と 0xFA40（IBM拡張）はどちらも U+2170 に復号される
    data = b"a,b\r\n\xee\xef,x\r\n\xfa\x40,y\r\n"
    doc = read_csv(data, encoding="cp932")
    assert doc.serialize({}) == data
    out = doc.serialize({(1, 1): "z"})
    assert out == b"a,b\r\n\xee\xef,z\r\n\xfa\x40,y\r\n"


def test_trailing_newline_absent_is_preserved():
    data = b"a,b\n1,2"
    doc = read_csv(data)
    assert doc.dialect.trailing_newline is False
    assert doc.serialize({(1, 1): "3"}) == b"a,b\n1,3"


def test_mixed_newlines_detected_and_kept():
    data = b"a,b\r\n1,2\n3,4\r\n"
    doc = read_csv(data)
    assert doc.dialect.mixed_newlines is True
    assert doc.serialize({(2, 0): "9"}) == b"a,b\r\n1,2\n9,4\r\n"


@pytest.mark.parametrize("delimiter", ["\t", ";", "|"])
def test_delimiter_detection(delimiter: str):
    data = delimiter.join(["a", "b", "c"]).encode() + b"\n" + delimiter.join(["1", "2,5", "3"]).encode() + b"\n"
    doc = read_csv(data)
    assert doc.dialect.delimiter == delimiter
    assert doc.values()[1] == ["1", "2,5", "3"]


def test_unterminated_quote_is_rejected():
    with pytest.raises(CsvFormatError):
        read_csv(b'a,b\n1,"oops\n')


def test_undecodable_file_is_rejected():
    with pytest.raises(CsvFormatError):
        read_csv(b"\x81\x00\xff\xfe\x80" * 3, encoding="utf-8")


def test_unencodable_replacement_is_reported():
    doc = read_csv("a\n1\n".encode("cp932"), encoding="cp932")
    with pytest.raises(UnencodableValueError):
        doc.serialize({(1, 0): "\U0001f600"})


def test_blank_line_is_kept_as_record():
    data = b"a,b\n\n1,2\n"
    doc = read_csv(data)
    assert doc.records[1].is_blank
    assert doc.serialize({}) == data
