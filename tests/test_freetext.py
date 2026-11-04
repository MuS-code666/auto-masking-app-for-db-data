"""文章内の語句の検出と置換（段階4、設計書 05 章 4）の単体試験。値はすべて架空。"""

from __future__ import annotations

from maskapp.transform.freetext import Dictionary, FreeTextPlanner, Target, detect_candidates


def _dictionary(*entries: tuple[str, str, bool]) -> Dictionary:
    """(元の語句, 置換後, コードか)。"""
    return Dictionary([(text, Target(f"t:{text}:{value}", value, None, "種類"), is_code)
                       for text, value, is_code in entries])


def _replace(dictionary: Dictionary, text: str) -> str:
    result = FreeTextPlanner(None, 0, dictionary).process(text, "s", "s", {})
    assert result is not None and result.value is not None
    return result.value


def test_longest_match_wins():
    d = _dictionary(("さくら商事", "顧客0001", False), ("さくら商", "顧客0009", False))
    assert _replace(d, "さくら商事と取引") == "顧客0001と取引"


def test_replacement_is_not_replaced_again():
    # 置換後の表記が別の語句と同じでも、置換は元の文字列から一度だけ組み立てる
    d = _dictionary(("ひかり物産", "顧客0002", False), ("顧客0002", "顧客0099", False))
    assert _replace(d, "ひかり物産へ連絡") == "顧客0002へ連絡"


def test_code_boundaries():
    d = _dictionary(("12345", "90001", True), ("C00777", "X00001", True))
    assert _replace(d, "伝票12345号") == "伝票90001号"  # 前後が英数字でなければ一致
    assert _replace(d, "123456") == "123456"  # 長い数字の一部には一致しない
    assert _replace(d, "AC00777") == "AC00777"
    assert _replace(d, "（C00777）") == "（X00001）"


def test_width_and_space_differences_are_matched():
    d = _dictionary(("ABC商事", "顧客0003", False), ("みなと 工業", "顧客0004", False))
    assert _replace(d, "ＡＢＣ商事様") == "顧客0003様"
    assert _replace(d, "みなと工業へ") == "顧客0004へ"
    assert _replace(d, "みなと　工業へ") == "顧客0004へ"


def test_kept_parts_exclude_replacements():
    d = _dictionary(("ひかり物産", "顧客0002", False))
    result = FreeTextPlanner(None, 0, d).process("前回ひかり物産より", "s", "s", {})
    assert result.kept == ("前回", "より") and result.changed


def _candidates(text: str) -> list[tuple[str, str]]:
    return [(c.pattern, c.text(text)) for c in detect_candidates(text, [False] * len(text))]


def test_rule_candidates():
    assert _candidates("099-0123-4567へ折り返し") == [("phone", "099-0123-4567")]
    assert _candidates("連絡先 staff01@example.com まで") == [("email", "staff01@example.com")]
    assert _candidates("〒100-0001 へ送付") == [("postal", "〒100-0001")]
    assert _candidates("詳細は https://example.com/a を参照") == [("url", "https://example.com/a")]
    assert _candidates("株式会社やまびこ運輸様より") == [("company", "株式会社やまびこ運輸")]
    assert _candidates("やまびこ運輸株式会社御中") == [("company", "やまびこ運輸株式会社")]
    assert _candidates("（株）こもれび堂へ") == [("company", "（株）こもれび堂")]
    assert _candidates("みなと台商店様") == [("honorific", "みなと台商店")]
    assert _candidates("納期調整（担当:辻堂）") == [("person_label", "辻堂")]


def test_common_words_are_not_candidates():
    assert _candidates("前回同様、お客様・皆様・各位へ") == []
    assert _candidates("仕様変更の件") == []
    assert _candidates("単価改定 2026/4/1適用") == []


def test_candidates_inside_replaced_terms_are_skipped():
    text = "株式会社あおば商事様より"
    covered = [True] * 9 + [False] * 3
    assert detect_candidates(text, covered) == []
