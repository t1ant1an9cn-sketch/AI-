import pytest

from voicedetect.analysis.text import comparable, count_syllables, normalize_units, read_integer, read_number


@pytest.mark.parametrize(
    ("digits", "spoken"),
    [
        ("0", "零"),
        ("10", "十"),
        ("15", "十五"),
        ("20", "二十"),
        ("105", "一百零五"),
        ("1005", "一千零五"),
        ("1010", "一千零一十"),
        ("10000", "一万"),
        ("10500", "一万零五百"),
        ("100500", "十万零五百"),
        ("120000", "十二万"),
        ("100010000", "一亿零一万"),
        ("0618", "零六一八"),
    ],
)
def test_read_integer_follows_spoken_chinese(digits, spoken):
    assert read_integer(digits) == spoken


def test_read_number_handles_decimals_percent_and_separators():
    assert read_number("3.5") == "三点五"
    assert read_number("50%") == "百分之五十"
    assert read_number("1,000") == "一千"
    assert read_number("618", by_digit=True) == "六一八"


def test_units_fold_width_case_and_keep_original_positions():
    text = "５００ＭＬ！保温"
    units = normalize_units(text)
    assert "".join(u.char for u in units) == "五百ml保温"
    # 数字读音的每个单元都指回整个“５００”
    assert {(u.start, u.end) for u in units[:2]} == {(0, 3)}
    assert text[units[-1].start : units[-1].end] == "温"
    assert units[-1].sound == "wen"


def test_number_aliases_compare_equal():
    assert comparable("两百块") == comparable("二百块") == comparable("200块")
    assert comparable("二〇二四") == comparable("二零二四")


def test_comparable_drops_punctuation_and_spaces():
    assert comparable("全网 最低，价！") == "全网最低价"


def test_syllable_count_expands_numbers_and_counts_latin_words_once():
    assert count_syllables(normalize_units("到手价99元")) == 7
    assert count_syllables(normalize_units("iPhone 很好")) == 3
    assert count_syllables(normalize_units("，。！")) == 0
