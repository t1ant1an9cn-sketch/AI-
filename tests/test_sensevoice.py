"""真实模型测试：用随程序附带的演示音频跑 SenseVoice 和整条盯播流程（未下载模型时跳过）。"""

from tests.conftest import REAL_MODELS, requires_model
from tests.fakes import Events, MemoryStore
from voicedetect.asr.sensevoice import load_engine
from voicedetect.audio.files import MediaSource
from voicedetect.frameworks import builtin_templates
from voicedetect.monitor.manager import demo_audio_path
from voicedetect.monitor.session import MonitorSession, SessionOptions

pytestmark = requires_model


def test_sensevoice_streams_partials_and_timed_sentences():
    recognizer = load_engine(REAL_MODELS).create(partials=True)
    source = MediaSource(demo_audio_path())
    source.open()
    events = []
    try:
        for _ in range(300):  # 前 30 秒
            events += recognizer.accept(source.read())
    finally:
        source.close()
    events += recognizer.flush()
    finals = [e for e in events if e.kind == "final"]
    text = "".join(e.text for e in finals)
    for phrase in ("欢迎", "保温杯", "316不锈钢"):
        assert phrase in text
    assert any(e.kind == "partial" for e in events)
    starts = [e.start for e in finals]
    assert starts == sorted(starts) and 0 < starts[0] < 3 and finals[-1].end <= 30.5
    assert all(e.end > e.start for e in finals)


def test_demo_audio_end_to_end_finds_the_planted_problems():
    framework = next(t for t in builtin_templates() if t.id == "tpl-thermos-cup")
    store = MemoryStore()
    session = MonitorSession(
        session_id="demo",
        title="演示",
        framework=framework,
        source=MediaSource(demo_audio_path()),
        recognizer_factory=lambda: load_engine(REAL_MODELS).create(partials=False),
        emit=Events(),
        options=SessionOptions(window_seconds=120, interval_seconds=30),
        store=store,
    )
    session.start()
    assert session.wait(120)
    assert session.state == "stopped"

    forbidden = {a.evidence[0].text for a in store.alerts if a.kind == "forbidden"}
    assert {"全网最低", "加我微信", "最好"} <= forbidden
    assert any(a.title == "冷场了" for a in store.alerts)

    summary = session.summary
    missing = {c.point_name for c in summary.coverage if c.status == "missing"}
    assert missing == {"赠品", "售后承诺"}
    # 按能量实测这段静音约 13.7 秒；VAD 没把其后很短的“嗯，好”算作人声，所以测得略长。
    assert summary.metrics.pauses and 12 <= summary.metrics.longest_pause <= 16
    assert summary.metrics.rate and 200 < summary.metrics.rate < 330
    assert any(f.kind == "rate_spike" for r in store.reports for f in r.findings)
