"""讯飞实时转写：用本地 WebSocket 服务器模拟官方协议（未连接真实服务）。"""

import json
import threading
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
from websockets.sync.server import serve

from voicedetect.asr.base import SAMPLE_RATE, RecognizerError
from voicedetect.asr.xfyun import XfyunRecognizer, parse_result, signature

APP_ID, API_KEY = "app123", "key456"


def result(text, bg, ed, final):
    words = [{"cw": [{"w": char, "wp": "n"}], "wb": 0, "we": 0} for char in text]
    data = {"cn": {"st": {"bg": str(bg), "ed": str(ed), "rt": [{"ws": words}], "type": "0" if final else "1"}}}
    return json.dumps(
        {"action": "result", "code": "0", "data": json.dumps(data, ensure_ascii=False), "desc": "success"}
    )


class FakeRtasr:
    """收到 1 秒音频发中间结果，2 秒发定稿，收到结束标志后再发最后一句并关闭。"""

    def __init__(self, *, fail_after=None):
        self.frames, self.queries = [], []
        self.fail_after = fail_after
        self.server = serve(self.handle, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}/v1/ws"

    def handle(self, ws):
        query = {k: v[0] for k, v in parse_qs(urlparse(ws.request.path).query).items()}
        self.queries.append(query)
        if query["signa"] != signature(APP_ID, API_KEY, query["ts"]):
            ws.send(json.dumps({"action": "error", "code": "10105", "desc": "illegal access"}))
            return
        ws.send(json.dumps({"action": "started", "code": "0", "data": "", "desc": "success"}))
        received, sent_partial, sent_final = 0, False, False
        for message in ws:
            if message == b'{"end": true}':
                ws.send(result("马上下单", 2000, 2900, True))
                return
            self.frames.append(len(message))
            received += len(message)
            if self.fail_after is not None and received >= self.fail_after:
                ws.send(json.dumps({"action": "error", "code": "10700", "desc": "engine error"}))
                return
            if received >= SAMPLE_RATE * 2 and not sent_partial:
                ws.send(result("今天", 0, 0, False))
                sent_partial = True
            if received >= SAMPLE_RATE * 4 and not sent_final:
                ws.send(result("今天到手价99元", 0, 1900, True))
                sent_final = True

    def close(self):
        self.server.shutdown()


@pytest.fixture
def rtasr():
    server = FakeRtasr()
    yield server
    server.close()


def feed(recognizer, seconds):
    events = []
    for _ in range(int(seconds * 10)):
        events += recognizer.accept(np.full(SAMPLE_RATE // 10, 0.1, dtype=np.float32))
    return events + recognizer.flush()


def test_signature_matches_the_documented_example():
    assert signature("595f23df", "d9f4aa7ea6d94faca62cd88a28fd5234", "1512041814") == "IrrzsJeOFk1NGfJHW6SkHUoN9CU="


def test_result_parsing():
    event = parse_result(json.loads(result("全网最低", 1500, 3200, True))["data"])
    assert (event.kind, event.text, event.start, event.end) == ("final", "全网最低", 1.5, 3.2)
    partial = parse_result(json.loads(result("全网", 1500, 0, False))["data"])
    assert (partial.kind, partial.start) == ("partial", 1.5)


def test_leading_punctuation_from_the_previous_sentence_is_removed():
    # 真实服务返回的样子：上一句的句号落在这一句开头
    event = parse_result(json.loads(result("。欢迎刚进直播间的宝宝们", 1650, 4310, True))["data"])
    assert event.text == "欢迎刚进直播间的宝宝们"
    assert parse_result(json.loads(result("，", 4310, 4400, True))["data"]).text == ""


class FakeClock:
    """sleep 只推进假时间，测试不必真的等 3 秒。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_streams_audio_in_40ms_frames_and_returns_timed_sentences(rtasr):
    clock = FakeClock()
    recognizer = XfyunRecognizer(app_id=APP_ID, api_key=API_KEY, url=rtasr.url, clock=clock, sleep=clock.sleep)
    events = feed(recognizer, 3)
    recognizer.close()

    assert rtasr.queries[0]["appid"] == APP_ID and rtasr.queries[0]["lang"] == "cn"
    assert set(rtasr.frames) == {1280}
    assert sum(rtasr.frames) == 3 * SAMPLE_RATE * 2
    finals = [(e.text, e.start, e.end) for e in events if e.kind == "final"]
    assert finals == [("今天到手价99元", 0, 1.9), ("马上下单", 2.0, 2.9)]
    assert any(e.kind == "partial" and e.text == "今天" for e in events)
    assert recognizer.speech_since is None
    # 文件一下子送进 3 秒音频，也按每帧 40 毫秒的节奏发完，正好用掉 3 秒
    assert clock.now == pytest.approx(3.0)


def test_wrong_key_is_reported_as_auth_failure(rtasr):
    recognizer = XfyunRecognizer(app_id=APP_ID, api_key="wrong", url=rtasr.url)
    with pytest.raises(RecognizerError, match="鉴权失败"):
        recognizer.accept(np.zeros(1600, dtype=np.float32))


def test_server_error_stops_recognition():
    server = FakeRtasr(fail_after=6400)
    try:
        recognizer = XfyunRecognizer(app_id=APP_ID, api_key=API_KEY, url=server.url, sleep=lambda s: None)
        with pytest.raises(RecognizerError, match="识别引擎出错"):
            feed(recognizer, 3)
    finally:
        server.close()


def test_missing_credentials_fail_early():
    with pytest.raises(RecognizerError, match="APPID"):
        XfyunRecognizer(app_id="", api_key="")
