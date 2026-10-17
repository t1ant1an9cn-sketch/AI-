"""讯飞「实时语音转写」（RTASR）云端识别。

按讯飞公开协议实现：WebSocket 连接 wss://rtasr.xfyun.cn/v1/ws，签名为
Base64(HmacSHA1(APIKey, MD5(APPID + 时间戳)))；每 40 毫秒发送 1280 字节 16 kHz 16 位单声道 PCM，
结束时发送 {"end": true}。返回结果中 type=0 为定稿，type=1 为中间结果，bg/ed 为毫秒时间。

需要在讯飞开放平台开通实时语音转写，按时长计费。2026-10-17 用真实账号送入演示音频前 30 秒验证过：
12 句定稿，每句说完后约 1 秒返回。自动化测试使用本地模拟服务器。
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from contextlib import ExitStack
import hashlib
import hmac
import json
import queue
import threading
import time
from typing import Any
import unicodedata
from urllib.parse import urlencode

import numpy as np

from .base import SAMPLE_RATE, AsrEvent, RecognizerError

URL = "wss://rtasr.xfyun.cn/v1/ws"
FRAME_BYTES = 1280  # 40 毫秒
BYTES_PER_SECOND = SAMPLE_RATE * 2

ERRORS = {
    "10105": "鉴权失败：请检查 APPID 和 APIKey，并确认已在讯飞控制台开通实时语音转写",
    "10106": "请求参数无效",
    "10110": "授权或签名无效（也可能是没有可用的转写时长），请核对 APPID、APIKey，并确认已开通服务",
    "10700": "讯飞识别引擎出错，请稍后重试",
    "10800": "超过讯飞账号允许的最大连接数",
}


def signature(app_id: str, api_key: str, ts: str) -> str:
    digest = hashlib.md5((app_id + ts).encode()).hexdigest()
    mac = hmac.new(api_key.encode(), digest.encode(), hashlib.sha1).digest()
    return base64.b64encode(mac).decode()


def describe_error(message: dict[str, Any]) -> str:
    code = str(message.get("code", ""))
    return ERRORS.get(code, f"讯飞返回错误 {code}：{message.get('desc', '')}")


def parse_result(data: str) -> AsrEvent | None:
    """把 action=result 的 data 字段解析成事件。"""
    payload = json.loads(data)
    sentence = payload["cn"]["st"]
    text = "".join(cw["w"] for rt in sentence.get("rt", []) for ws in rt.get("ws", []) for cw in ws.get("cw", []))
    # 讯飞把上一句的结束标点放在下一句开头（“，新来的宝宝……”），去掉。
    while text and (text[0].isspace() or unicodedata.category(text[0]).startswith("P")):
        text = text[1:]
    start = int(sentence.get("bg", 0)) / 1000
    if sentence.get("type") == "0":
        return AsrEvent("final", text, start, max(start, int(sentence.get("ed", 0)) / 1000))
    return AsrEvent("partial", text, start, start) if text else None


class XfyunRecognizer:
    name = "讯飞实时转写"

    def __init__(
        self,
        *,
        app_id: str,
        api_key: str,
        url: str = URL,
        connect: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not (app_id and api_key):
            raise RecognizerError("请先填写讯飞的 APPID 和 APIKey")
        self._app_id, self._api_key, self._url = app_id, api_key, url
        self._connect = connect
        self._clock, self._sleep = clock, sleep
        self._connection = ExitStack()  # 连接要跨多次 accept 保持，用 ExitStack 代替 with
        self._socket = None
        self._reader: threading.Thread | None = None
        self._events: queue.Queue[AsrEvent] = queue.Queue()
        self._error: RecognizerError | None = None
        self._buffer = bytearray()
        self._sent = 0
        self._started = 0.0
        self._speech_start: float | None = None

    @property
    def speech_since(self) -> float | None:
        return self._speech_start

    def _open(self) -> None:
        connect = self._connect
        if connect is None:
            from websockets.sync.client import connect
        ts = str(int(time.time()))
        query = urlencode(
            {"appid": self._app_id, "ts": ts, "signa": signature(self._app_id, self._api_key, ts), "lang": "cn"}
        )
        try:
            self._socket = self._connection.enter_context(
                connect(f"{self._url}?{query}", open_timeout=10, close_timeout=5)
            )
            first = json.loads(self._socket.recv(timeout=10))
        except RecognizerError:
            raise
        except Exception as exc:
            raise RecognizerError(f"连不上讯飞实时转写：{exc}") from None
        if first.get("action") != "started":
            raise RecognizerError(describe_error(first))
        self._started = self._clock()
        self._reader = threading.Thread(target=self._read, name="xfyun-reader", daemon=True)
        self._reader.start()

    def accept(self, samples: np.ndarray) -> list[AsrEvent]:
        if self._socket is None:
            self._open()
        self._check()
        self._buffer += (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        while len(self._buffer) >= FRAME_BYTES:
            self._send(bytes(self._buffer[:FRAME_BYTES]))
            del self._buffer[:FRAME_BYTES]
        return self._drain()

    def _send(self, frame: bytes) -> None:
        # 文档建议每 40 毫秒发送一帧，发得太快服务端会报错，所以按音频时长等待，不领先实时。
        ahead = (self._sent + len(frame)) / BYTES_PER_SECOND - (self._clock() - self._started)
        if ahead > 0:
            self._sleep(ahead)
        try:
            self._socket.send(frame)
        except Exception as exc:
            self._check()
            raise RecognizerError(f"讯飞连接中断：{exc}") from None
        self._sent += len(frame)

    def flush(self) -> list[AsrEvent]:
        if self._socket is None:
            return []
        if self._buffer:
            self._send(bytes(self._buffer))
            self._buffer.clear()
        try:
            self._socket.send(json.dumps({"end": True}).encode())
        except Exception:
            pass  # 连接已断开时，读线程会记录原因
        if self._reader is not None:
            self._reader.join(10)  # 服务端发完最后的结果后会关闭连接
        self._check()
        events = self._drain()
        self._speech_start = None
        return events

    def close(self) -> None:
        try:
            self._connection.close()
        except Exception:
            pass  # 连接可能已被服务端关闭

    def _read(self) -> None:
        try:
            for message in self._socket:
                data = json.loads(message)
                if data.get("action") == "result" and data.get("data"):
                    event = parse_result(data["data"])
                    if event is not None:
                        self._events.put(event)
                elif data.get("action") == "error":
                    self._error = RecognizerError(describe_error(data))
                    return
        except Exception as exc:
            if self._error is None and not _closed_normally(exc):
                self._error = RecognizerError(f"讯飞连接中断：{exc}")

    def _check(self) -> None:
        if self._error is not None:
            raise self._error

    def _drain(self) -> list[AsrEvent]:
        events = []
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if event.kind == "partial":
                self._speech_start = event.start
                event = AsrEvent("partial", event.text, event.start, max(event.start, self._sent / BYTES_PER_SECOND))
            else:
                self._speech_start = None
            events.append(event)
        return events


def _closed_normally(exc: Exception) -> bool:
    try:
        from websockets.exceptions import ConnectionClosedOK
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, ConnectionClosedOK)
