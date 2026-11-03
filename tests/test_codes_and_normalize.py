from __future__ import annotations

import pytest

from maskapp.entity.codes import CodeExhaustedError, OutputRule, derive_rule
from maskapp.entity.normalize import normalize_for_search, normalize_name
from maskapp.verify.residual import ResidualScanner, TermKind


def test_numeric_codes_keep_length_and_avoid_original_range():
    rule = derive_rule(["000101", "000108", "004990"])
    assert rule.kind == "numeric"
    assert rule.make(1) == "900000"
    assert len(rule.make(99)) == 6


def test_numeric_codes_already_in_high_range_start_above_maximum():
    rule = derive_rule(["950000", "960000"])
    assert rule.make(1) == "960001"


def test_prefixed_codes_change_prefix():
    rule = derive_rule(["P00001", "P00100"])
    assert (rule.kind, rule.make(7)) == ("prefixed", "X00007")
    assert derive_rule(["X001"]).make(1) == "Z001"


def test_generic_codes():
    rule = derive_rule(["12-345", "AB-1"])
    assert rule.kind == "generic"
    assert rule.make(3).startswith("Z")


def test_exhaustion():
    rule = OutputRule("numeric", length=2, start=98)
    assert rule.capacity == 2
    rule.make(2)
    with pytest.raises(CodeExhaustedError):
        rule.make(3)


def test_rule_round_trips_through_json():
    rule = derive_rule(["000101"])
    assert OutputRule.from_json(rule.to_json()) == rule


def test_normalize_name_for_candidates():
    assert normalize_name("株式会社　さくら商事") == normalize_name("さくら商事")
    assert normalize_name("㈱サクラ") == normalize_name("サクラ")
    assert normalize_name("ｻｸﾗ-ﾌｰｽﾞ") == normalize_name("サクラーフーズ")
    assert normalize_name("さくら") != normalize_name("さくら商事")


def test_normalize_for_search_keeps_legal_form():
    assert normalize_for_search("株式会社 ＡＢＣ") == "株式会社abc"


def test_residual_scanner_boundaries_and_normalization():
    scanner = ResidualScanner([("123", TermKind.CODE), ("さくら商事", TermKind.NAME), ("AB", TermKind.NAME)])
    assert scanner.scan("1234") == []  # コードは英数字に隣接していれば一致としない
    assert [h.term for h in scanner.scan("No.123の件")] == ["123"]
    assert [h.term for h in scanner.scan("ｻｸﾗ")] == []
    assert [h.term for h in scanner.scan("さくら 商事様")] == ["さくら商事"]  # 空白の違いを吸収
    hit = scanner.scan("ab")[0]
    assert hit.short is True


def test_empty_scanner():
    assert ResidualScanner([]).scan("anything") == []
