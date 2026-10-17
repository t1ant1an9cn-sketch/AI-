import pytest

from voicedetect.analysis.report import Segment
from voicedetect.analysis.window import FrameworkAnalyzer, describe_span
from voicedetect.frameworks import ForbiddenRule, Framework, PaceRules, Point, Stage

FRAMEWORK = Framework(
    id="fw",
    name="测试框架",
    stages=[
        Stage(
            id="product",
            name="产品介绍",
            points=[
                Point(id="material", name="材质", keywords=["不锈钢"]),
                Point(id="capacity", name="容量", keywords=["500毫升"], description="说明容量 500 毫升"),
                Point(id="color", name="颜色", keywords=["颜色"], required=False),
                Point(id="free", name="自由发挥", keywords=[]),
            ],
        ),
        Stage(
            id="price", name="价格优惠", points=[Point(id="cta", name="下单引导", keywords=["下单"], min_mentions=2)]
        ),
    ],
    forbidden=[ForbiddenRule(id="absolute", name="绝对化用语", keywords=["全网最低"], suggestion="改说很实惠")],
    fillers=["嗯", "就是说"],
    pace=PaceRules(min_rate=150, max_rate=330, max_pause_seconds=8, min_speech_ratio=0.5, max_fillers_per_minute=8),
)


def facts(analyzer, *rows):
    """rows: (start, end, text)。"""
    return [analyzer.inspect(Segment(id=i, start=s, end=e, text=t)) for i, (s, e, t) in enumerate(rows, 1)]


def kinds(report):
    return {finding.key: finding for finding in report.findings}


def test_full_window_reports_missing_and_insufficient_points_with_evidence():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    history = facts(analyzer, (0, 5, "这个杯子是不锈钢的"), (6, 10, "大家赶紧下单"))
    report = analyzer.analyze(history, 0, 60, full=True)

    coverage = {item.point_id: item for item in report.coverage}
    assert coverage["material"].status == "covered"
    assert coverage["material"].evidence[0].text == "不锈钢"
    assert coverage["material"].evidence[0].sentence == "这个杯子是不锈钢的"
    assert (coverage["material"].first_at, coverage["material"].evidence[0].end) == (0, 5)
    assert coverage["capacity"].status == "missing"
    assert coverage["cta"].status == "insufficient" and coverage["cta"].mentions == 1
    assert coverage["free"].status == "unchecked"

    found = kinds(report)
    assert found["missing:capacity"].severity == "medium"
    assert found["missing:capacity"].suggestion == "说明容量 500 毫升"
    assert "500毫升" in found["missing:capacity"].detail
    assert found["few:cta"].detail == "过去 1 分钟只讲了 1 次，要求至少 2 次。"
    assert "missing:color" not in found  # 非必讲要点不报
    assert "missing:free" not in found  # 没有关键词的要点交给大模型判断


def test_partial_window_does_not_claim_points_were_skipped():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    report = analyzer.analyze(facts(analyzer, (0, 5, "开始")), 0, 60, full=False)
    assert {item.status for item in report.coverage} == {"missing", "unchecked"}
    assert not [f for f in report.findings if f.kind in {"missing_point", "few_mentions"}]


def test_final_summary_judges_coverage_even_when_shorter_than_window():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    report = analyzer.analyze(facts(analyzer, (0, 5, "开始")), 0, 40, full=False, final=True)
    assert kinds(report)["missing:material"].detail.startswith("本场没有讲到")


def test_forbidden_words_are_reported_with_original_quotes():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    history = facts(analyzer, (3, 7, "今天全网最低价"), (8, 12, "真的是全网最低"))
    report = analyzer.analyze(history, 0, 60, full=False)
    assert [(hit.rule_id, hit.evidence.text, hit.evidence.start) for hit in report.forbidden] == [
        ("absolute", "全网最低", 3),
        ("absolute", "全网最低", 8),
    ]
    finding = kinds(report)["forbidden:absolute"]
    assert (finding.severity, finding.suggestion) == ("high", "改说很实惠")
    assert finding.detail == "过去 1 分钟「绝对化用语」出现 2 次：全网最低。"


def test_fast_and_slow_speech_rate():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    fast = facts(analyzer, *[(i * 6, i * 6 + 6, "快" * 40) for i in range(10)])  # 400 字/分钟
    report = analyzer.analyze(fast, 0, 60, full=True)
    assert report.metrics.rate == 400
    assert kinds(report)["rate_fast"].severity == "medium"

    slow = facts(analyzer, *[(i * 6, i * 6 + 6, "慢" * 14) for i in range(10)])  # 140 字/分钟
    finding = kinds(analyzer.analyze(slow, 0, 60, full=True))["rate_slow"]
    assert finding.severity == "low"
    assert finding.detail == "过去 1 分钟语速约 140 字/分钟，建议不低于 150。"


def test_one_rushed_sentence_is_flagged_even_when_the_average_is_fine():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    history = facts(analyzer, (0, 10, "快" * 100), (12, 32, "正常" * 30), (35, 55, "正常" * 30))
    report = analyzer.analyze(history, 0, 60, full=True)
    assert report.metrics.rate < 330
    spike = kinds(report)["rate_spike"]
    assert spike.detail == "过去 1 分钟有 1 句语速超过 429 字/分钟，最快约 600。"
    assert spike.evidence[0].start == 0

    rushed = facts(analyzer, *[(i * 6, i * 6 + 6, "快" * 60) for i in range(10)])
    found = kinds(analyzer.analyze(rushed, 0, 60, full=True))
    assert "rate_fast" in found and "rate_spike" not in found  # 整体已经偏快时不再逐句重复


def test_rate_needs_enough_speech():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    report = analyzer.analyze(facts(analyzer, (0, 10, "快" * 100)), 0, 60, full=True)
    assert report.metrics.rate is None
    assert "rate_fast" not in kinds(report)


def test_pauses_ignore_initial_silence_and_respect_ongoing_speech():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    history = facts(analyzer, (30, 35, "第一句"), (50, 55, "第二句"))
    report = analyzer.analyze(history, 0, 60, full=True)
    assert [(p.start, p.end) for p in report.metrics.pauses] == [(35, 50)]

    later = analyzer.analyze(history, 0, 80, full=True)
    assert [(p.start, p.end) for p in later.metrics.pauses] == [(35, 50), (55, 80)]
    assert later.metrics.longest_pause == 25
    assert kinds(later)["long_pause"].severity == "medium"

    speaking = analyzer.analyze(history, 0, 80, full=True, speech_since=60)
    assert [(p.start, p.end) for p in speaking.metrics.pauses] == [(35, 50)]


def test_pause_before_window_is_still_seen_from_history():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    history = facts(analyzer, (0, 5, "开场"), (70, 75, "回来了"))
    report = analyzer.analyze(history, 60, 120, full=True)
    assert [(p.start, p.end) for p in report.metrics.pauses] == [(5, 70), (75, 120)]


def test_speech_ratio_and_boundary_overlap():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    history = facts(analyzer, (0, 6, "你好"), (55, 65, "跨过窗口边界的一句话"))
    report = analyzer.analyze(history, 0, 60, full=True)
    assert report.metrics.speech_seconds == 11
    assert report.metrics.speech_ratio == pytest.approx(11 / 60, abs=1e-3)
    assert kinds(report)["low_speech"].detail == "过去 1 分钟说话时间只占 18%，建议不低于 50%。"


def test_fillers_are_counted_per_minute():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    history = facts(analyzer, *[(i * 6, i * 6 + 5, "嗯就是说嗯这个") for i in range(5)])
    report = analyzer.analyze(history, 0, 60, full=True)
    assert report.metrics.fillers == {"嗯": 10, "就是说": 5}
    assert report.metrics.fillers_per_minute == 15
    assert kinds(report)["fillers"].detail == "过去 1 分钟每分钟约 15.0 次：嗯×10、就是说×5。"


def test_minute_rates_follow_session_minutes():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    history = facts(analyzer, (40, 60, "字" * 60), (60, 80, "字" * 100))
    report = analyzer.analyze(history, 30, 90, full=True)
    rates = [(m.start, m.end, m.speech_seconds, m.rate) for m in report.metrics.minute_rates]
    assert rates == [(30, 60, 20, 180), (60, 90, 20, 300)]


def test_order_check_reports_price_before_product():
    framework = FRAMEWORK.model_copy(update={"check_order": True})
    analyzer = FrameworkAnalyzer(framework)
    history = facts(analyzer, (0, 5, "现在下单"), (6, 10, "杯子是不锈钢"))
    finding = kinds(analyzer.analyze(history, 0, 60, full=True))["order"]
    assert finding.detail == "先讲了「价格优惠」，后讲「产品介绍」；框架要求「产品介绍」在前。"


def test_highlights_mark_points_forbidden_words_and_fillers():
    analyzer = FrameworkAnalyzer(FRAMEWORK)
    (item,) = facts(analyzer, (0, 5, "嗯不锈钢全网最低"))
    marks = [(h.kind, item.segment.text[h.start : h.end], h.label) for h in analyzer.highlights(item)]
    assert marks == [
        ("filler", "嗯", "口头禅"),
        ("point", "不锈钢", "产品介绍·材质"),
        ("forbidden", "全网最低", "绝对化用语"),
    ]


@pytest.mark.parametrize(
    ("seconds", "final", "text"), [(300, False, "过去 5 分钟"), (90, False, "过去 90 秒"), (90, True, "本场")]
)
def test_describe_span(seconds, final, text):
    assert describe_span(seconds, final=final) == text
