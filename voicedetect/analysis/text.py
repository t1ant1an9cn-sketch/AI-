"""转写文本归一化：把原文变成可比较的“单元”序列，并记住每个单元在原文中的位置。

识别结果与话术框架的写法常有出入：全角半角、标点、“500”与“五百”、同音字。
匹配时先逐字比较，再按无声调拼音比较；统计语速时按单元计数（数字按读音展开）。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata

from pypinyin import Style, lazy_pinyin

_DIGITS = "零一二三四五六七八九"
_GROUP_UNITS = ("千", "百", "十", "")
_BIG_UNITS = ("", "万", "亿")
# 同一个数字的不同写法，统一后再比较（“两百”与“二百”）。
_CHAR_ALIASES = {"两": "二", "〇": "零", "幺": "一"}
_NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?%?")


@dataclass(frozen=True, slots=True)
class Unit:
    """归一化后的一个字符。"""

    char: str
    sound: str  # 汉字为无声调拼音，其他字符为自身
    start: int  # 在原文中的起始下标
    end: int  # 在原文中的结束下标（不含）


def read_digits(digits: str) -> str:
    """逐位读数字："618" → "六一八"。"""
    return "".join(_DIGITS[int(d)] for d in digits)


def _read_group(value: int) -> str:
    """读 1–9999 之间的数。"""
    parts: list[str] = []
    started = zero = False
    for digit, unit in zip(f"{value:04d}", _GROUP_UNITS, strict=True):
        if digit == "0":
            zero = started
            continue
        if zero:
            parts.append("零")
            zero = False
        parts.append(_DIGITS[int(digit)] + unit)
        started = True
    return "".join(parts)


def read_integer(digits: str) -> str:
    """按数值读整数："105" → "一百零五"；超过 12 位或以 0 开头时逐位读。"""
    if len(digits) > 12 or (len(digits) > 1 and digits[0] == "0"):
        return read_digits(digits)
    value = int(digits)
    if value == 0:
        return "零"
    groups: list[int] = []
    while value:
        groups.append(value % 10000)
        value //= 10000
    parts: list[str] = []
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if group == 0:
            continue
        if parts and group < 1000:
            parts.append("零")
        parts.append(_read_group(group) + _BIG_UNITS[index])
    text = "".join(parts)
    # 口语里 10–19 读“十几”，不读“一十几”。
    return text[1:] if text.startswith("一十") else text


def read_number(token: str, *, by_digit: bool = False) -> str:
    """读一个数字记号，支持小数和百分号："3.5" → "三点五"，"50%" → "百分之五十"。"""
    percent = token.endswith("%")
    if percent:
        token = token[:-1]
    whole, _, fraction = token.replace(",", "").partition(".")
    text = read_digits(whole) if by_digit else read_integer(whole)
    if fraction:
        text += "点" + read_digits(fraction)
    return "百分之" + text if percent else text


def is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF or 0x20000 <= code <= 0x3134F
    )


@lru_cache(maxsize=8192)
def sound_of(char: str) -> str:
    """单字的无声调拼音。逐字取音，保证同一个字在任何上下文里读法一致。"""
    if not is_cjk(char):
        return char
    return lazy_pinyin(char, style=Style.NORMAL)[0]


def _folded_chars(text: str) -> list[tuple[str, int, int]]:
    """逐个原文字符做兼容分解（全角转半角）和小写，记录来源位置。"""
    result: list[tuple[str, int, int]] = []
    for index, original in enumerate(text):
        for char in unicodedata.normalize("NFKC", original).lower():
            result.append((_CHAR_ALIASES.get(char, char), index, index + 1))
    return result


def normalize_units(text: str, *, by_digit: bool = False) -> list[Unit]:
    """原文 → 单元序列。数字按读音展开，标点、空白和符号丢弃。

    by_digit 为 True 时数字逐位读，用于给关键词生成另一种读法。
    """
    folded = _folded_chars(text)
    flat = "".join(char for char, _, _ in folded)
    units: list[Unit] = []
    position = 0
    for match in _NUMBER.finditer(flat):
        units.extend(_plain_units(folded[position : match.start()]))
        start, end = folded[match.start()][1], folded[match.end() - 1][2]
        for char in read_number(match.group(), by_digit=by_digit):
            units.append(Unit(char, sound_of(char), start, end))
        position = match.end()
    units.extend(_plain_units(folded[position:]))
    return units


def _plain_units(chars: list[tuple[str, int, int]]) -> list[Unit]:
    units = []
    for char, start, end in chars:
        if unicodedata.category(char)[0] in "LN":
            units.append(Unit(char, sound_of(char), start, end))
    return units


def comparable(text: str) -> str:
    """去掉标点和空白、统一写法后的字符串，用于判断引文是否出自原文。"""
    return "".join(unit.char for unit in normalize_units(text))


def _is_latin(unit: Unit) -> bool:
    return unit.char.isascii() and unit.char.isalpha()


def count_syllables(units: list[Unit]) -> int:
    """估算音节数：汉字（含数字读音）一个字算一个，原文中相连的拉丁字母算一个词。"""
    count = 0
    previous: Unit | None = None
    for unit in units:
        same_word = previous is not None and _is_latin(unit) and _is_latin(previous) and previous.end == unit.start
        if not same_word:
            count += 1
        previous = unit
    return count
