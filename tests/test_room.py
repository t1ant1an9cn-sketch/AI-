"""直播间链接：用本机的 Edge/Chrome 打开本地模拟的直播间网页，验证两种取声音的方式、断流重连和出错提示。"""

from __future__ import annotations

import http.server
import threading
import time

import av
import numpy as np
import pytest

from tests.fakes import BlockSource, MemoryStore, ScriptedRecognizer
from tests.test_api import connect, make_services
from tests.test_audio_files import dominant_frequency, write_wav
from voicedetect.asr.base import SAMPLE_RATE
from voicedetect.audio.base import level_db
from voicedetect.audio.browser import find_browser
from voicedetect.audio.files import MediaSource
from voicedetect.audio.room import RoomError, RoomSource, clean_title, extract_url, is_room_page, platform_name
from voicedetect.frameworks import Framework
from voicedetect.monitor.manager import SourceSpec, build_source
from voicedetect.monitor.session import MonitorSession

requires_browser = pytest.mark.skipif(find_browser() is None, reason="本机没有 Edge 或 Chrome")
TONE_DB = 20 * np.log10(0.3 / np.sqrt(2))  # 幅度 0.3 的正弦波

# 模拟抖音：网页播放器用 fetch 拉 flv 直播流
FLV_ROOM = """<!doctype html><title>测试主播的直播间 - 测试直播</title><script>
fetch('/cdn/live.flv').then(r => {
  const reader = r.body.getReader();
  const pump = () => reader.read().then(x => { if (!x.done) pump(); });
  pump();
});
</script>"""
# 模拟虎牙：没有 flv/m3u8 请求，声音由 Web Audio 播放；网页转到后台时播放器自己静音
WEB_AUDIO_ROOM = """<!doctype html><title>Web Audio 直播间</title><script>
const ctx = new AudioContext();
const osc = ctx.createOscillator();
osc.frequency.value = 660;
const gain = ctx.createGain();
gain.gain.value = 0.3;
osc.connect(gain);
gain.connect(ctx.destination);
osc.start();
document.addEventListener('visibilitychange', () => { if (document.hidden) gain.gain.value = 0; });
</script>"""
# 模拟用 MediaSource 的播放器：媒体数据经 blob: 地址交给 <audio>
ELEMENT_ROOM = """<!doctype html><title>媒体元素直播间</title><audio id="a" loop></audio><script>
fetch('/media/tone.wav').then(r => r.blob()).then(b => {
  const a = document.getElementById('a');
  a.src = URL.createObjectURL(b);
  a.play();
});
</script>"""
OFFLINE_ROOM = "<!doctype html><title>还没开播</title><p>主播还没开播</p>"


def write_flv(path, seconds, *, frequency, rate=44100):
    with av.open(str(path), "w", format="flv") as container:
        stream = container.add_stream("aac", rate=rate, layout="mono")
        t = np.arange(int(seconds * rate)) / rate
        tone = (0.3 * np.sin(2 * np.pi * frequency * t)).astype(np.float32)
        for start in range(0, len(tone) - 1024, 1024):  # AAC 每帧固定 1024 个样本
            frame = av.AudioFrame.from_ndarray(tone[None, start : start + 1024], format="fltp", layout="mono")
            frame.sample_rate = rate
            frame.pts = start
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return path.read_bytes()


class Site:
    """本地的模拟直播间网站。/cdn/ 下的流要求 Referer 来自直播间网页，模拟直播 CDN 的防盗链。"""

    def __init__(self, tmp_path):
        self.flv = write_flv(tmp_path / "live.flv", 3.0, frequency=440)
        self.wav = write_wav(tmp_path / "tone.wav", 2.0, rate=16000, channels=1, frequency=880).read_bytes()
        self.live = True
        self.requests: list[tuple[str, dict[str, str]]] = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                site.requests.append((self.path, dict(self.headers)))
                pages = {
                    "/web-audio-room": WEB_AUDIO_ROOM,
                    "/element-room": ELEMENT_ROOM,
                    "/offline-room": OFFLINE_ROOM,
                }
                pages["/flv-room"] = FLV_ROOM if site.live else OFFLINE_ROOM
                if self.path in pages:
                    self._send(200, "text/html; charset=utf-8", pages[self.path].encode())
                elif self.path == "/cdn/live.flv":
                    if not site.live:
                        self._send(404, "text/plain", b"ended")
                    elif "/flv-room" not in self.headers.get("Referer", ""):
                        self._send(403, "text/plain", b"forbidden")
                    else:
                        self._send(200, "video/x-flv", site.flv)
                elif self.path == "/media/tone.wav":
                    self._send(200, "audio/wav", site.wav)
                else:
                    self._send(404, "text/plain", b"")

            def _send(self, status, kind, body):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass  # 浏览器或 FFmpeg 读到一半断开

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def pulls(self):
        """FFmpeg 发来的拉流请求（浏览器的请求带 Sec-Fetch-Mode，FFmpeg 的不带）。"""
        return [
            headers for path, headers in self.requests if path == "/cdn/live.flv" and "Sec-Fetch-Mode" not in headers
        ]


@pytest.fixture
def site(tmp_path):
    site = Site(tmp_path)
    yield site
    site.server.shutdown()


def read_seconds(source, seconds):
    blocks = []
    while sum(map(len, blocks)) < seconds * SAMPLE_RATE:
        block = source.read()
        if block is None:
            break
        blocks.append(block)
    return np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)


# —— 网址识别 ——


def test_links_are_recognised_from_pasted_share_text():
    share = "8- 长按复制此条消息，打开抖音搜索，查看TA的更多作品。 https://v.douyin.com/AbC123/ 复制此链接"
    assert extract_url(share) == "https://v.douyin.com/AbC123/"
    assert extract_url("live.douyin.com/123456") == "https://live.douyin.com/123456"
    assert extract_url("demo.mp3") == "demo.mp3"  # 文件名不当成网址
    assert is_room_page("https://live.douyin.com/123456")
    assert is_room_page(share)
    assert not is_room_page("https://cdn.example.com/live/abc.flv?sign=1")
    assert not is_room_page("rtmp://example.com/live/room")
    assert not is_room_page("D:/录播/第一场.mp4")
    assert [
        platform_name(u) for u in ("https://live.douyin.com/1", "https://www.huya.com/1", "https://live.bilibili.com/1")
    ] == [
        "抖音",
        "虎牙",
        "B站",
    ]
    assert platform_name("https://example.org/room/1") == "example.org"
    assert clean_title("某某官方旗舰店的抖音直播间 - 抖音直播") == "某某官方旗舰店的抖音直播间"
    assert clean_title("某某主播-快手直播") == "某某主播"
    assert clean_title("年度总决赛_赛事直播_虎牙直播") == "年度总决赛"


def test_url_source_picks_room_page_or_direct_stream():
    room = build_source(SourceSpec(kind="url", url="快来看 https://live.douyin.com/123456 ！"))
    assert isinstance(room, RoomSource) and room.url == "https://live.douyin.com/123456"
    assert room.live and room.label == "抖音直播间"
    stream = build_source(SourceSpec(kind="url", url="https://cdn.example.com/live/abc.flv"))
    assert isinstance(stream, MediaSource) and stream.live
    with pytest.raises(ValueError, match="请填写直播间链接"):
        build_source(SourceSpec(kind="url", url="  "))


# —— 开播前打开直播间 ——


class UnopenableRoom(BlockSource):
    def __init__(self):
        super().__init__(1, live=True)
        self.prepared = False

    def prepare(self):
        self.prepared = True
        raise RoomError("没有在这个直播间里找到正在播放的直播")


def test_room_that_cannot_be_opened_fails_before_a_session_is_recorded(tmp_path):
    services = make_services(tmp_path)
    room = UnopenableRoom()
    services.monitor._make_source = lambda spec: room
    try:
        with connect(services) as client:
            request = {"framework_id": "tpl-thermos-cup", "source": {"kind": "url", "url": "https://live.douyin.com/1"}}
            response = client.post("/api/session/start", json=request)
            assert (response.status_code, response.json()["detail"]) == (400, "没有在这个直播间里找到正在播放的直播")
            assert client.get("/api/sessions").json()["total"] == 0
        assert room.prepared and room.closed and not room.opened
    finally:
        services.close()


class NoticingSource(BlockSource):
    """模拟直播间来源：读到第 5 块时报告一次断流重连。"""

    def take_notices(self):
        if self.sent == 5:
            return [("stream_reconnected", "low", "直播流中断后已重新连接", "中断了约 3 秒。", "")]
        return []


def test_source_notices_become_system_alerts():
    store = MemoryStore()
    session = MonitorSession(
        session_id="s1",
        title="测试",
        framework=Framework(name="空框架"),
        source=NoticingSource(1.0, live=True),
        recognizer_factory=lambda: ScriptedRecognizer([]),
        emit=lambda event: None,
        store=store,
    )
    session.start()
    assert session.wait(10)
    [alert] = [alert for alert in store.alerts if alert.kind == "stream_reconnected"]
    assert (alert.source, alert.severity, alert.title) == ("system", "low", "直播流中断后已重新连接")


# —— 真实浏览器 ——


@requires_browser
def test_room_with_a_flv_stream_is_pulled_directly_and_reconnects(site):
    source = RoomSource(site.url("/flv-room"), resolve_timeout=20)
    try:
        source.prepare()
        assert source.mode == "stream"
        assert source.label == "127.0.0.1：测试主播的直播间"
        assert source._browser is None  # 拿到流地址后浏览器就关了
        source.open()
        audio = read_seconds(source, 4.5)  # 模拟的流只有 3 秒：读完后断开，应自动重连接着读
        assert len(audio) >= 4.5 * SAMPLE_RATE
        assert dominant_frequency(audio[4000:20000]) == pytest.approx(440, abs=5)
        assert [notice[0] for notice in source.take_notices()] == ["stream_reconnected"]
    finally:
        source.close()
    pulls = site.pulls()
    assert len(pulls) >= 2  # 试拉一次，断开后重连至少一次
    for headers in pulls:  # 带上了网页播放器的 Referer 和浏览器的 User-Agent，没有被防盗链拦下
        assert headers["Referer"] == site.url("/flv-room")
        assert "Chrome/" in headers["User-Agent"] and "Headless" not in headers["User-Agent"]


@requires_browser
def test_stream_that_ends_is_reported_and_stops_the_source(site):
    source = RoomSource(site.url("/flv-room"), resolve_timeout=4)
    try:
        source.prepare()
        source.open()
        site.live = False  # 下播：流地址 404，网页上也没有直播了
        started = time.monotonic()
        audio = read_seconds(source, 30)
        assert len(audio) < 3.2 * SAMPLE_RATE
        assert time.monotonic() - started < 20
        [notice] = source.take_notices()
        assert notice[:3] == ("stream_ended", "high", "直播流断开了")
    finally:
        source.close()


@requires_browser
def test_page_playing_through_web_audio_is_captured_even_in_background(site):
    source = RoomSource(site.url("/web-audio-room"), page_audio_after=2, resolve_timeout=20)
    try:
        source.prepare()
        assert (source.mode, source.label) == ("page", "127.0.0.1：Web Audio 直播间")
        source.open()
        started = time.monotonic()
        audio = read_seconds(source, 2.0)
        assert time.monotonic() - started >= 1.5  # 按实际时间送来
        assert dominant_frequency(audio[4000:]) == pytest.approx(660, abs=5)
        assert level_db(audio[4000:]) == pytest.approx(TONE_DB, abs=2)
        # 另开一个标签页，把直播间挤到后台：网页里的播放器不应因此静音
        source._browser.call("Target.createTarget", {"url": "about:blank"})
        time.sleep(1.0)
        audio = read_seconds(source, 2.0)
        assert level_db(audio[8000:]) == pytest.approx(TONE_DB, abs=2)
    finally:
        source.close()


@requires_browser
def test_page_playing_through_a_media_element_is_captured(site):
    source = RoomSource(site.url("/element-room"), page_audio_after=2, resolve_timeout=20)
    try:
        source.prepare()
        assert source.mode == "page"
        source.open()
        audio = read_seconds(source, 2.0)
        assert dominant_frequency(audio[4000:]) == pytest.approx(880, abs=5)
        assert level_db(audio[4000:]) == pytest.approx(TONE_DB, abs=2)
    finally:
        source.close()


@requires_browser
def test_room_without_a_live_stream_explains_what_to_do(site):
    source = RoomSource(site.url("/offline-room"), page_audio_after=2, resolve_timeout=4)
    started = time.monotonic()
    with pytest.raises(RoomError, match=r"没有在这个直播间里找到正在播放的直播.*系统声音"):
        source.prepare()
    assert time.monotonic() - started < 12
    assert source._browser is None
