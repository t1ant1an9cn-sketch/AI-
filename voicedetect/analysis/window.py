"""按话术框架分析一个时间窗口内的讲解。

每句定稿转写到达时先 inspect 一次：关键词、违规表达、口头禅和字数都只算这一次。
窗口分析只汇总这些结果，所以“每分钟分析过去 5 分钟”的开销很小，也能对全场做总结。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
import math

from ..frameworks import Framework, Point, Stage
from .matcher import Hit, Matcher
from .report import (
    Evidence,
    Finding,
    ForbiddenHit,
    Highlight,
    Metrics,
    MinuteRate,
    Pause,
    PointCoverage,
    Segment,
    WindowReport,
)
from .text import count_syllables, normalize_units

MIN_SPEECH_FOR_RATE = 20.0  # 说话不足 20 秒时不评价语速
MIN_SPEECH_FOR_MINUTE_RATE = 10.0
MAX_EVIDENCE = 3
# 窗口平均会把十几秒的抢话摊薄，所以长句单独看：超过上限 1.3 倍算“说得太快”。
SPIKE_MIN_SECONDS = 5.0
SPIKE_FACTOR = 1.3


@dataclass(frozen=True, slots=True)
class SegmentFacts:
    """一句转写的分析结果，窗口分析时直接复用。"""

    segment: Segment
    syllables: int
    point_hits: tuple[Hit, ...]
    forbidden_hits: tuple[Hit, ...]
    filler_hits: tuple[Hit, ...]

    def evidence(self, hit: Hit) -> Evidence:
        segment = self.segment
        return Evidence(
            segment_id=segment.id,
            text=segment.text[hit.start : hit.end],
            sentence=segment.text,
            start=segment.start,
            end=segment.end,
            exact=hit.exact,
        )


def describe_span(seconds: float, *, final: bool = False) -> str:
    """“过去 5 分钟”“过去 90 秒”或“本场”。"""
    if final:
        return "本场"
    seconds = round(seconds)
    if seconds >= 60 and seconds % 60 == 0:
        return f"过去 {seconds // 60} 分钟"
    return f"过去 {seconds} 秒"


class FrameworkAnalyzer:
    """把一个话术框架编译成匹配器，负责单句检查和窗口分析。"""

    def __init__(self, framework: Framework) -> None:
        self.framework = framework
        self._points: dict[str, tuple[Stage, Point]] = {point.id: (stage, point) for stage, point in framework.points()}
        self._rules = {rule.id: rule for rule in framework.forbidden}
        self._point_matcher = Matcher({point_id: point.keywords for point_id, (_, point) in self._points.items()})
        self._forbidden_matcher = Matcher({rule.id: rule.keywords for rule in framework.forbidden})
        self._filler_matcher = Matcher({word: [word] for word in framework.fillers}, homophones=False)

    def inspect(self, segment: Segment) -> SegmentFacts:
        return SegmentFacts(
            segment=segment,
            syllables=count_syllables(normalize_units(segment.text)),
            point_hits=tuple(self._point_matcher.scan(segment.text)),
            forbidden_hits=tuple(self._forbidden_matcher.scan(segment.text)),
            filler_hits=tuple(self._filler_matcher.scan(segment.text)),
        )

    def highlights(self, facts: SegmentFacts) -> list[Highlight]:
        items = []
        for hit in facts.point_hits:
            stage, point = self._points[hit.group]
            label = f"{stage.name}·{point.name}"
            items.append(
                Highlight(start=hit.start, end=hit.end, kind="point", ref=hit.group, label=label, exact=hit.exact)
            )
        for hit in facts.forbidden_hits:
            rule = self._rules[hit.group]
            items.append(
                Highlight(
                    start=hit.start, end=hit.end, kind="forbidden", ref=hit.group, label=rule.name, exact=hit.exact
                )
            )
        for hit in facts.filler_hits:
            items.append(Highlight(start=hit.start, end=hit.end, kind="filler", ref=hit.group, label="口头禅"))
        return sorted(items, key=lambda item: (item.start, item.kind))

    def forbidden_findings(self, facts: Sequence[SegmentFacts], span: str) -> list[Finding]:
        """把违规命中按规则归并成问题；实时提醒和窗口报告共用。"""
        grouped: dict[str, list[tuple[SegmentFacts, Hit]]] = defaultdict(list)
        for item in facts:
            for hit in item.forbidden_hits:
                grouped[hit.group].append((item, hit))
        findings = []
        for rule_id, hits in grouped.items():
            rule = self._rules[rule_id]
            words = "、".join(dict.fromkeys(item.segment.text[hit.start : hit.end] for item, hit in hits))
            findings.append(
                Finding(
                    key=f"forbidden:{rule_id}",
                    kind="forbidden",
                    severity=rule.severity,
                    title=f"出现违规表达「{words.split('、')[0]}」",
                    detail=f"{span}「{rule.name}」出现 {len(hits)} 次：{words}。",
                    suggestion=rule.suggestion,
                    evidence=[item.evidence(hit) for item, hit in hits[:MAX_EVIDENCE]],
                )
            )
        return findings

    def analyze(
        self,
        history: Sequence[SegmentFacts],
        start: float,
        end: float,
        *,
        full: bool,
        final: bool = False,
        speech_since: float | None = None,
    ) -> WindowReport:
        """分析 [start, end) 窗口。

        history 是会话至今按时间排序的全部句子（用于判断窗口开头是否处在停顿中）；
        speech_since 是正在说、尚未定稿的那句话的开始时间，用来截断末尾的停顿。
        """
        window = [item for item in history if item.segment.end > start and item.segment.start < end]
        metrics = self._metrics(history, window, start, end, speech_since)
        coverage = self._coverage(window)
        forbidden = [
            ForbiddenHit(
                rule_id=hit.group,
                rule_name=self._rules[hit.group].name,
                severity=self._rules[hit.group].severity,
                keyword=hit.keyword,
                evidence=item.evidence(hit),
            )
            for item in window
            for hit in item.forbidden_hits
        ]
        span = describe_span(end - start, final=final)
        findings = self._coverage_findings(coverage, span) if full or final else []
        findings += self.forbidden_findings(window, span)
        findings += self._pace_findings(metrics, span)
        if not any(finding.kind == "rate_fast" for finding in findings):
            findings += self._spike_findings(window, span)
        return WindowReport(
            start=start,
            end=end,
            full=full,
            final=final,
            metrics=metrics,
            coverage=coverage,
            forbidden=forbidden,
            findings=findings,
        )

    def _metrics(
        self,
        history: Sequence[SegmentFacts],
        window: list[SegmentFacts],
        start: float,
        end: float,
        speech_since: float | None,
    ) -> Metrics:
        duration = max(end - start, 0.0)
        speech, worded, syllables = _speech_and_syllables(window, start, end)
        pauses = self._pauses(history, start, end, speech_since)
        fillers = Counter(hit.group for item in window for hit in item.filler_hits)
        minutes = []
        for minute in range(math.floor(start / 60), math.ceil(end / 60)):
            low, high = max(minute * 60.0, start), min(minute * 60.0 + 60, end)
            if high <= low:
                continue
            minute_speech, minute_worded, minute_syllables = _speech_and_syllables(window, low, high)
            minutes.append(
                MinuteRate(
                    start=low,
                    end=high,
                    speech_seconds=round(minute_speech, 2),
                    rate=_rate(minute_syllables, minute_worded, MIN_SPEECH_FOR_MINUTE_RATE),
                )
            )
        return Metrics(
            window_seconds=round(duration, 2),
            speech_seconds=round(speech, 2),
            speech_ratio=round(speech / duration, 3) if duration else 0.0,
            syllables=round(syllables),
            rate=_rate(syllables, worded, MIN_SPEECH_FOR_RATE),
            segment_count=sum(1 for item in window if item.segment.text),
            pauses=pauses,
            longest_pause=max((pause.seconds for pause in pauses), default=0.0),
            fillers=dict(fillers.most_common()),
            fillers_per_minute=round(sum(fillers.values()) / duration * 60, 2) if duration else 0.0,
            minute_rates=minutes,
        )

    def _pauses(
        self, history: Sequence[SegmentFacts], start: float, end: float, speech_since: float | None
    ) -> list[Pause]:
        """超过阈值的停顿。开播前第一句话之前的静音不算。"""
        threshold = self.framework.pace.max_pause_seconds
        gaps: list[tuple[float, float]] = []
        previous_end: float | None = None
        for item in history:
            segment = item.segment
            if segment.start >= end:
                break
            if previous_end is not None and segment.start - previous_end >= threshold:
                gaps.append((previous_end, segment.start))
            previous_end = segment.end if previous_end is None else max(previous_end, segment.end)
        if previous_end is not None and previous_end < end:
            tail = end if speech_since is None or speech_since <= previous_end else min(speech_since, end)
            if tail - previous_end >= threshold:
                gaps.append((previous_end, tail))
        return [
            Pause(start=round(low, 2), end=round(high, 2), seconds=round(high - low, 1))
            for low, high in gaps
            if high > start and low < end
        ]

    def _coverage(self, window: list[SegmentFacts]) -> list[PointCoverage]:
        found: dict[str, list[tuple[SegmentFacts, Hit]]] = defaultdict(list)
        for item in window:
            for hit in item.point_hits:
                found[hit.group].append((item, hit))
        coverage = []
        for stage, point in self.framework.points():
            hits = found.get(point.id, [])
            if not point.keywords:
                status = "unchecked"
            elif len(hits) >= point.min_mentions:
                status = "covered"
            elif hits:
                status = "insufficient"
            else:
                status = "missing"
            coverage.append(
                PointCoverage(
                    stage_id=stage.id,
                    stage_name=stage.name,
                    point_id=point.id,
                    point_name=point.name,
                    required=point.required,
                    min_mentions=point.min_mentions,
                    mentions=len(hits),
                    status=status,
                    evidence=[item.evidence(hit) for item, hit in hits[-MAX_EVIDENCE:]],
                    first_at=hits[0][0].segment.start if hits else None,
                    last_at=hits[-1][0].segment.start if hits else None,
                )
            )
        return coverage

    def _coverage_findings(self, coverage: list[PointCoverage], span: str) -> list[Finding]:
        findings = []
        for item in coverage:
            if not item.required:
                continue
            _, point = self._points[item.point_id]
            if item.status == "missing":
                keywords = "、".join(point.keywords[:4])
                findings.append(
                    Finding(
                        key=f"missing:{item.point_id}",
                        kind="missing_point",
                        severity="medium",
                        title=f"没讲「{item.point_name}」",
                        detail=f"{span}没有讲到「{item.stage_name}·{item.point_name}」（关键词：{keywords}）。",
                        suggestion=point.description,
                    )
                )
            elif item.status == "insufficient":
                findings.append(
                    Finding(
                        key=f"few:{item.point_id}",
                        kind="few_mentions",
                        severity="low",
                        title=f"「{item.point_name}」讲得太少",
                        detail=f"{span}只讲了 {item.mentions} 次，要求至少 {item.min_mentions} 次。",
                        suggestion=point.description,
                        evidence=item.evidence,
                    )
                )
        if self.framework.check_order:
            findings += self._order_findings(coverage)
        return findings

    def _order_findings(self, coverage: list[PointCoverage]) -> list[Finding]:
        """环节第一次被讲到的先后，与框架顺序不一致时提示一次。"""
        first: dict[str, float] = {}
        for item in coverage:
            if item.first_at is not None:
                first[item.stage_id] = min(first.get(item.stage_id, math.inf), item.first_at)
        position = {stage.id: index for index, stage in enumerate(self.framework.stages)}
        names = {stage.id: stage.name for stage in self.framework.stages}
        spoken = sorted(first, key=lambda stage_id: first[stage_id])
        for earlier, later in pairwise(spoken):
            if position[later] < position[earlier]:
                return [
                    Finding(
                        key="order",
                        kind="order",
                        severity="low",
                        title="讲解顺序与框架不一致",
                        detail=f"先讲了「{names[earlier]}」，后讲「{names[later]}」；框架要求「{names[later]}」在前。",
                        suggestion="按框架顺序讲解，让观众先了解产品再听价格和促单",
                    )
                ]
        return []

    def _pace_findings(self, metrics: Metrics, span: str) -> list[Finding]:
        pace = self.framework.pace
        findings = []
        if metrics.rate is not None and metrics.rate > pace.max_rate:
            findings.append(
                Finding(
                    key="rate_fast",
                    kind="rate_fast",
                    severity="medium" if metrics.rate > pace.max_rate * 1.15 else "low",
                    title="语速偏快",
                    detail=f"{span}语速约 {metrics.rate:.0f} 字/分钟，建议不超过 {pace.max_rate:.0f}。",
                    suggestion="放慢语速，把价格、规格等关键信息说清楚",
                )
            )
        elif metrics.rate is not None and metrics.rate < pace.min_rate:
            findings.append(
                Finding(
                    key="rate_slow",
                    kind="rate_slow",
                    severity="medium" if metrics.rate < pace.min_rate * 0.85 else "low",
                    title="语速偏慢",
                    detail=f"{span}语速约 {metrics.rate:.0f} 字/分钟，建议不低于 {pace.min_rate:.0f}。",
                    suggestion="提高节奏和情绪，减少拖音和重复",
                )
            )
        if metrics.pauses:
            threshold = pace.max_pause_seconds
            findings.append(
                Finding(
                    key="long_pause",
                    kind="long_pause",
                    severity="medium" if metrics.longest_pause >= threshold * 2 else "low",
                    title="出现冷场",
                    detail=(
                        f"{span}有 {len(metrics.pauses)} 次停顿超过 {threshold:g} 秒，"
                        f"最长 {metrics.longest_pause:.0f} 秒。"
                    ),
                    suggestion="提前准备互动话题或产品细节，避免长时间没人说话",
                )
            )
        if metrics.window_seconds >= 60 and metrics.speech_ratio < pace.min_speech_ratio:
            findings.append(
                Finding(
                    key="low_speech",
                    kind="low_speech",
                    severity="medium",
                    title="说话时间偏少",
                    detail=f"{span}说话时间只占 {metrics.speech_ratio:.0%}，建议不低于 {pace.min_speech_ratio:.0%}。",
                    suggestion="保持持续讲解，减少空档",
                )
            )
        filler_total = sum(metrics.fillers.values())
        if filler_total >= 3 and metrics.fillers_per_minute > pace.max_fillers_per_minute:
            top = "、".join(f"{word}×{count}" for word, count in list(metrics.fillers.items())[:4])
            findings.append(
                Finding(
                    key="fillers",
                    kind="fillers",
                    severity="low",
                    title="口头禅偏多",
                    detail=f"{span}每分钟约 {metrics.fillers_per_minute:.1f} 次：{top}。",
                    suggestion="想不起来时用短暂停顿代替口头禅",
                )
            )
        return findings

    def _spike_findings(self, window: list[SegmentFacts], span: str) -> list[Finding]:
        limit = self.framework.pace.max_rate * SPIKE_FACTOR
        spikes = []
        for item in window:
            seconds = item.segment.end - item.segment.start
            if seconds >= SPIKE_MIN_SECONDS and item.syllables / seconds * 60 > limit:
                spikes.append((item, item.syllables / seconds * 60))
        if not spikes:
            return []
        fastest = max(rate for _, rate in spikes)
        return [
            Finding(
                key="rate_spike",
                kind="rate_spike",
                severity="low",
                title="有句子说得太快",
                detail=f"{span}有 {len(spikes)} 句语速超过 {limit:.0f} 字/分钟，最快约 {fastest:.0f}。",
                suggestion="长句子中间停一停，把卖点一个一个说清楚",
                evidence=[
                    Evidence(
                        segment_id=item.segment.id,
                        text=item.segment.text,
                        sentence=item.segment.text,
                        start=item.segment.start,
                        end=item.segment.end,
                    )
                    for item, _ in spikes[:MAX_EVIDENCE]
                ],
            )
        ]


def _speech_and_syllables(window: list[SegmentFacts], start: float, end: float) -> tuple[float, float, float]:
    """窗口内的说话秒数、其中认出了字的秒数，以及字数；跨窗口边界的句子按时长比例计入。

    没认出字的说话算说话时间，但不参与语速，免得把语速算低。
    """
    speech = worded = syllables = 0.0
    for item in window:
        segment = item.segment
        overlap = min(segment.end, end) - max(segment.start, start)
        if overlap <= 0:
            continue
        speech += overlap
        if segment.text:
            worded += overlap
            syllables += item.syllables * overlap / max(segment.end - segment.start, 1e-6)
    return speech, worded, syllables


def _rate(syllables: float, speech: float, minimum: float) -> float | None:
    return round(syllables / speech * 60, 1) if speech >= minimum else None
