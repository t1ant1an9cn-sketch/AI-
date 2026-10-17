import time

import pytest

from tests.fakes import BlockSource, Events, MemoryStore, ScriptedRecognizer
from voicedetect.analysis.report import LlmIssue, LlmReview
from voicedetect.frameworks import ForbiddenRule, Framework, PaceRules, Point, Stage
from voicedetect.monitor.session import MonitorSession, SessionOptions

FRAMEWORK = Framework(
    id="fw",
    name="测试框架",
    stages=[
        Stage(
            id="product",
            name="产品介绍",
            points=[
                Point(id="material", name="材质", keywords=["不锈钢"]),
                Point(id="capacity", name="容量", keywords=["500毫升"]),
            ],
        )
    ],
    forbidden=[ForbiddenRule(id="absolute", name="绝对化用语", keywords=["全网最低"])],
    pace=PaceRules(max_pause_seconds=8),
)


def run(lines=(), seconds=10.0, *, source=None, recognizer=None, options=None, reviewer=None, factory=None):
    events, store = Events(), MemoryStore()
    session = MonitorSession(
        session_id="s1",
        title="测试场次",
        framework=FRAMEWORK,
        source=source or BlockSource(seconds),
        recognizer_factory=factory or (lambda: recognizer or ScriptedRecognizer(lines)),
        emit=events,
        options=options or SessionOptions(window_seconds=120, interval_seconds=60),
        store=store,
        reviewer=reviewer,
    )
    session.start()
    assert session.wait(20), "会话没有按时结束"
    return session, events, store


def test_windows_follow_audio_time_and_end_with_tail_report_and_summary():
    lines = [(1, 5, "这个杯子是不锈钢的"), (10, 14, "容量是五百毫升"), (70, 74, "再说一遍不锈钢")]
    session, events, store = run(lines, 135)

    assert session.state == "stopped" and session.audio_time == 135
    assert [(r.start, r.end, r.full) for r in store.reports] == [(0, 60, False), (0, 120, True), (15, 135, True)]
    assert [s.text for s in store.segments] == ["这个杯子是不锈钢的", "容量是五百毫升", "再说一遍不锈钢"]
    assert [e["state"] for e in events.of("state")] == ["starting", "running", "stopped"]
    summary = session.summary
    assert (summary.final, summary.start, summary.end) == (True, 0, 135)
    assert {c.point_id: c.mentions for c in summary.coverage} == {"material": 2, "capacity": 1}
    assert store.finished == {"status": "stopped", "duration": 135, "summary": summary, "error": ""}


def test_segment_events_carry_highlights_for_the_transcript():
    _, events, _ = run([(1, 5, "不锈钢全网最低")], 8)
    (segment,) = events.of("segment")
    assert segment["segment"]["text"] == "不锈钢全网最低"
    assert [(h["kind"], h["label"]) for h in segment["highlights"]] == [
        ("point", "产品介绍·材质"),
        ("forbidden", "绝对化用语"),
    ]
    assert events.of("partial"), "说话过程中应推送正在识别的文字"


def test_forbidden_words_alert_immediately_and_only_once():
    _, _, store = run([(2, 6, "今天全网最低价")], 20)
    alerts = [a for a in store.alerts if a.kind == "forbidden"]
    assert [(a.source, a.time, a.evidence[0].text) for a in alerts] == [("realtime", 2, "全网最低")]
    assert any(f.key == "forbidden:absolute" for f in store.reports[-1].findings)  # 报告里仍然有


def test_pause_alerts_once_per_silence_and_resets_when_speech_resumes():
    _, _, store = run([(0, 4, "第一句"), (20, 24, "第二句")], 40)
    pauses = [a for a in store.alerts if a.title == "冷场了"]
    assert [(a.time, a.source) for a in pauses] == [(4, "realtime"), (24, "realtime")]


def test_speech_without_recognized_text_still_resets_the_pause_timer():
    # 中间那句只说了“嗯，好”之类没被识别出文字的话，冷场应从它结束时算起。
    _, _, store = run([(0, 2, "第一句"), (10, 12, "")], 25)
    assert [a.time for a in store.alerts if a.title == "冷场了"] == [12]


def test_window_findings_alert_only_when_they_first_appear():
    options = SessionOptions(window_seconds=60, interval_seconds=30)
    _, _, store = run([(1, 5, "不锈钢")], 125, options=options)
    assert [(r.start, r.end) for r in store.reports] == [(0, 30), (0, 60), (30, 90), (60, 120)]
    missing = [(a.title, a.time) for a in store.alerts if a.kind == "missing_point"]
    assert missing == [("没讲「容量」", 60), ("没讲「材质」", 90)]


def test_live_source_drops_backlog_but_keeps_the_timeline():
    source = BlockSource(6, live=True, pace=0.001)
    options = SessionOptions(window_seconds=60, interval_seconds=30, max_backlog_seconds=1)
    session, _, store = run(source=source, recognizer=ScriptedRecognizer([], delay=0.02), options=options)
    assert session.audio_time == pytest.approx(6.0)
    assert session.health()["dropped_seconds"] > 0
    assert any(a.kind == "dropped_audio" and a.source == "system" for a in store.alerts)


def test_no_signal_is_reported_for_live_sources():
    source = BlockSource(12, live=True, level=0.0)
    options = SessionOptions(no_signal_seconds=5)
    _, _, store = run(source=source, options=options)
    assert [a.title for a in store.alerts if a.kind == "no_signal"] == ["没有收到声音"]


def test_recognizer_failure_is_reported_and_recorded():
    def broken():
        raise FileNotFoundError("模型文件不存在")

    session, events, store = run(factory=broken)
    assert session.state == "failed"
    assert session.error == "识别失败：模型文件不存在"
    assert events.of("state")[-1] == {
        "type": "state",
        "session_id": "s1",
        "state": "failed",
        "error": session.error,
        "engine": "",
    }
    assert store.finished["status"] == "failed"


def test_capture_failure_stops_the_session_and_closes_the_device():
    source = BlockSource(30, fail_at=5)
    session, _, _ = run([(0, 0.3, "你好")], source=source)
    assert session.state == "failed" and session.error == "音频采集失败：设备被拔出"
    assert source.closed


def test_stop_finishes_recognition_and_summarises():
    source = BlockSource(100, live=True, pace=0.002)
    events, store = Events(), MemoryStore()
    recognizer = ScriptedRecognizer([(0.1, 0.4, "开场了")])
    session = MonitorSession(
        session_id="s1",
        title="测试",
        framework=FRAMEWORK,
        source=source,
        recognizer_factory=lambda: recognizer,
        emit=events,
        store=store,
    )
    session.start()
    time.sleep(0.3)
    session.stop()
    assert session.wait(10)
    assert session.state == "stopped" and 0 < session.audio_time < 100
    assert recognizer.closed and source.closed
    assert store.finished["status"] == "stopped"
    assert [s.text for s in store.segments] == ["开场了"]


class FakeReviewer:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = []

    def review(self, framework, segments, report):
        self.calls.append((report.start, report.end, [s.text for s in segments]))
        time.sleep(self.delay)
        return LlmReview(
            status="done",
            model="fake",
            summary="讲得还行",
            issues=[LlmIssue(category="价格", severity="high", quote="九十九", problem="价格说错了", verified=True)],
        )


def test_llm_review_is_attached_to_reports_and_raises_alerts():
    reviewer = FakeReviewer()
    session, events, store = run([(1, 5, "只要九十九")], 75, reviewer=reviewer)
    assert reviewer.calls == [(0, 60, ["只要九十九"]), (0, 75, ["只要九十九"])]
    assert store.llm[1].summary == "讲得还行"
    assert events.of("llm")[0]["review"]["status"] == "done"
    # 两轮审核都指出同一句原话，只提醒一次，并带上原话所在的句子和时间
    (alert,) = [a for a in store.alerts if a.source == "llm"]
    assert (alert.severity, alert.detail, alert.time) == ("high", "「九十九」价格说错了", 1)
    assert (alert.evidence[0].sentence, alert.evidence[0].start) == ("只要九十九", 1)
    # 断线重连后从快照恢复时，审核结果也在
    assert [r["llm"]["summary"] for r in session.snapshot()["reports"]] == ["讲得还行", "讲得还行"]


def test_slow_llm_review_skips_for_live_sources_instead_of_piling_up():
    reviewer = FakeReviewer(delay=0.5)
    options = SessionOptions(window_seconds=10, interval_seconds=5, max_backlog_seconds=60)
    _, events, _ = run([(1, 3, "你好")], source=BlockSource(21, live=True), reviewer=reviewer, options=options)
    statuses = [e["review"]["status"] for e in events.of("llm")]
    assert "skipped" in statuses and "done" in statuses


class QuotingReviewer:
    """按给定的问题列表“审核”，用来检查哪些问题会变成提醒。"""

    def __init__(self, issues):
        self.issues = issues

    def review(self, framework, segments, report):
        return LlmReview(status="done", issues=self.issues)


def test_llm_does_not_repeat_rule_hits_and_skips_unverified_quotes():
    issues = [
        LlmIssue(category="违规风险", severity="high", quote="今天全网最低价", problem="绝对化用语", verified=True),
        LlmIssue(category="信息错误", severity="high", quote="只要八十九", problem="价格说错了", verified=True),
        LlmIssue(category="信息错误", severity="high", quote="没说过的话", problem="编的", verified=False),
    ]
    _, _, store = run([(1, 5, "今天全网最低价"), (6, 9, "只要八十九")], 62, reviewer=QuotingReviewer(issues))
    # 违规词已由规则即时提醒，大模型重复指出的不再提醒；引文对不上转写的也不提醒
    assert [(a.title, a.time) for a in store.alerts if a.source == "llm"] == [("价格说错了", 6)]
    assert [a.evidence[0].text for a in store.alerts if a.kind == "forbidden"] == ["全网最低"]


def test_llm_alerts_skip_points_the_rules_flagged_and_get_short_titles():
    issues = [
        # “容量”是规则已判定没讲的必讲要点，模型再说一遍就不提醒
        LlmIssue(category="其他", severity="medium", problem="全程没提到容量是多少"),
        LlmIssue(category="表达", severity="medium", title="卖点讲得太笼统", problem="只说了好用，没有具体说明"),
        LlmIssue(
            category="节奏",
            severity="medium",
            problem="主播说到价格时语气很犹豫，前后两次报价不一致，容易让观众怀疑价格的真实性",
        ),
    ]
    _, _, store = run([(1, 5, "这个杯子是不锈钢的，真的很好用")], 62, reviewer=QuotingReviewer(issues))
    assert [a.title for a in store.alerts if a.source == "llm"] == [
        "卖点讲得太笼统",
        "主播说到价格时语气很犹豫，前后两次报价不一致",  # 没给标题时在标点处截短
    ]


def test_file_analysis_queues_every_llm_review():
    reviewer = FakeReviewer(delay=0.2)
    options = SessionOptions(window_seconds=10, interval_seconds=5)
    # 第 1 句只在前两个窗口里；第 2 句没认出字（文字为空），不交给大模型
    _, events, store = run([(1, 3, "你好"), (16, 18, "")], 21, reviewer=reviewer, options=options)
    assert [e["review"]["status"] for e in events.of("llm")] == ["done", "done"]
    assert [(start, end) for start, end, _ in reviewer.calls] == [(0, 5), (0, 10)]
    assert sorted(store.llm) == [1, 2]


def test_snapshot_restores_the_view_after_reconnect():
    session, _, _ = run([(1, 5, "不锈钢全网最低")], 75)
    snapshot = session.snapshot()
    assert snapshot["state"] == "stopped"
    assert snapshot["engine"] == "脚本"  # 识别引擎加载后报上来的名字
    assert snapshot["paced"] is False  # 快速分析文件，界面不弹窗
    assert snapshot["framework"] == {"id": "fw", "name": "测试框架", "pace": FRAMEWORK.pace.model_dump()}
    assert [s["segment"]["text"] for s in snapshot["segments"]] == ["不锈钢全网最低"]
    assert snapshot["alerts"][0]["kind"] == "forbidden"
    assert [r["end"] for r in snapshot["reports"]] == [60, 75]
    assert snapshot["minute_rates"][0]["minute"] == 0
    assert snapshot["summary"]["final"] is True
