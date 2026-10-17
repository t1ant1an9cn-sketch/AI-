"""关键词匹配：在一句转写里找出框架关键词，返回能回到原文的命中位置。

先逐字比较（“食品级”对“食品级”），再按拼音比较，容忍识别出的同音字（“食品级”对“食品及”）。
单字关键词只逐字比较，避免同音误报。含数字的关键词同时尝试数值读法和逐位读法（“618”也可读“六一八”）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .text import Unit, normalize_units


@dataclass(frozen=True, slots=True)
class Hit:
    """一次命中。start/end 是原文下标，text[start:end] 即证据原文。"""

    group: str  # 关键词所属分组，例如要点 ID、违规规则 ID
    keyword: str  # 框架里写的关键词
    start: int
    end: int
    exact: bool  # True 为逐字一致，False 为同音命中


@dataclass(frozen=True, slots=True)
class _Pattern:
    group: str
    keyword: str
    chars: tuple[str, ...]
    sounds: tuple[str, ...]


def keyword_variants(keyword: str) -> list[list[Unit]]:
    """关键词的可比较写法；含数字时多给一种逐位读法。"""
    variants = [normalize_units(keyword)]
    if any(char.isdigit() for char in keyword):
        by_digit = normalize_units(keyword, by_digit=True)
        if [u.char for u in by_digit] != [u.char for u in variants[0]]:
            variants.append(by_digit)
    return [units for units in variants if units]


class Matcher:
    """预先整理一组分组关键词，之后可对任意多句文本重复查找。"""

    def __init__(self, groups: Mapping[str, Iterable[str]], *, homophones: bool = True) -> None:
        self._by_char: dict[str, list[_Pattern]] = defaultdict(list)
        self._by_sound: dict[str, list[_Pattern]] = defaultdict(list)
        for group, keywords in groups.items():
            for keyword in keywords:
                for units in keyword_variants(keyword):
                    pattern = _Pattern(group, keyword, tuple(u.char for u in units), tuple(u.sound for u in units))
                    self._by_char[pattern.chars[0]].append(pattern)
                    if homophones and len(units) >= 2 and pattern.sounds != pattern.chars:
                        self._by_sound[pattern.sounds[0]].append(pattern)

    def scan(self, text: str) -> list[Hit]:
        """返回全部命中；同一分组内互不重叠，优先取更长、逐字一致的命中。"""
        units = normalize_units(text)
        chars = tuple(u.char for u in units)
        sounds = tuple(u.sound for u in units)
        candidates: list[tuple[_Pattern, int, bool]] = []
        for index in range(len(units)):
            for pattern in self._by_char.get(chars[index], ()):
                if chars[index : index + len(pattern.chars)] == pattern.chars:
                    candidates.append((pattern, index, True))
            for pattern in self._by_sound.get(sounds[index], ()):
                size = len(pattern.sounds)
                if sounds[index : index + size] == pattern.sounds and chars[index : index + size] != pattern.chars:
                    candidates.append((pattern, index, False))

        candidates.sort(key=lambda item: (-len(item[0].chars), not item[2], item[1]))
        taken: dict[str, list[tuple[int, int]]] = defaultdict(list)
        hits: list[Hit] = []
        for pattern, index, exact in candidates:
            span = (index, index + len(pattern.chars))
            if any(span[0] < end and start < span[1] for start, end in taken[pattern.group]):
                continue
            taken[pattern.group].append(span)
            hits.append(Hit(pattern.group, pattern.keyword, units[span[0]].start, units[span[1] - 1].end, exact))
        hits.sort(key=lambda hit: (hit.start, hit.group))
        return hits
