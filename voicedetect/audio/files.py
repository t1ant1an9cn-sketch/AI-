"""音频文件和直播流：用 PyAV（内置 FFmpeg）解码 wav/mp3/m4a/flac/视频文件或 http(s)/rtmp/m3u8 地址。

文件默认尽快处理（用于复盘录播）；勾选“按实际速度播放”时按真实时间送入，效果与盯直播一致。
直播流地址天然是实时的，按实时来源处理（识别跟不上时丢弃最旧的音频）。
"""

from __future__ import annotations

from pathlib import Path
import time
from urllib.parse import urlsplit

import numpy as np

from ..asr.base import SAMPLE_RATE

BLOCK_SAMPLES = SAMPLE_RATE // 10
STREAM_PREFIXES = ("http://", "https://", "rtmp://", "rtmps://", "rtsp://", "srt://")


def is_stream_url(location: str) -> bool:
    return location.lower().startswith(STREAM_PREFIXES)


def system_proxy(url: str) -> str | None:
    """这个地址按系统设置该走的 HTTP 代理（没有或在例外名单里时返回 None）。

    浏览器默认走系统代理，拉流也走同一条路：开着代理软件时，有的 CDN 节点只有经代理才连得上。
    FFmpeg 只支持 http:// 代理，https 地址会经它建立隧道。
    """
    import urllib.request

    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return None
    proxies = urllib.request.getproxies()  # 先看环境变量，Windows 上再看系统代理设置
    proxy = proxies.get(parts.scheme.lower()) or proxies.get("http")
    if not proxy or not proxy.lower().startswith("http://"):
        return None
    if urllib.request.proxy_bypass(parts.hostname or ""):
        return None
    return proxy


class MediaSource:
    """解码文件或流的第一条音轨，输出 16 kHz 单声道 float32。"""

    def __init__(
        self,
        location: str | Path,
        *,
        realtime: bool = False,
        label: str = "",
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.location = str(location)
        self.stream = is_stream_url(self.location)
        self.live = self.stream
        self.realtime = realtime and not self.stream  # 流本身就按实时到达，不必再等
        self.label = label or (f"直播流：{self.location}" if self.stream else f"音频文件：{Path(self.location).name}")
        self.headers = headers or {}  # 拉流时带上的请求头；不少直播 CDN 要求和网页播放器一致的 Referer、User-Agent
        self.timeout = timeout
        self.duration: float | None = None
        self._container = None
        self._frames = None
        self._resampler = None
        self._buffer = np.zeros(0, dtype=np.float32)
        self._produced = 0
        self._started = 0.0
        self._ended = False

    def open(self) -> None:
        import av

        options = {}
        if self.stream:
            options = {"rw_timeout": str(int(self.timeout * 1_000_000)), "reconnect": "1", "reconnect_streamed": "1"}
            if proxy := system_proxy(self.location):
                options["http_proxy"] = proxy
            headers = dict(self.headers)
            if agent := headers.pop("User-Agent", ""):
                options["user_agent"] = agent  # User-Agent 用 FFmpeg 自己的选项，其余放进自定义请求头
            if headers:
                options["headers"] = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        try:
            self._container = av.open(self.location, options=options, timeout=(self.timeout, self.timeout))
        except FileNotFoundError:
            raise FileNotFoundError(f"找不到文件：{self.location}") from None
        except av.FFmpegError as exc:
            raise RuntimeError(f"无法打开{'直播流' if self.stream else '文件'}：{exc}") from None
        if not self._container.streams.audio:
            self._container.close()
            raise RuntimeError("这个文件里没有音轨")
        audio = self._container.streams.audio[0]
        if self._container.duration:
            self.duration = self._container.duration / 1_000_000
        self._frames = self._container.decode(audio)
        # 保留原声道重采样，再自己取平均：FFmpeg 默认的下混会把相同的左右声道叠加高 3 dB。
        self._resampler = av.AudioResampler(format="fltp", rate=SAMPLE_RATE)
        self._started = time.monotonic()

    def read(self) -> np.ndarray | None:
        while len(self._buffer) < BLOCK_SAMPLES and not self._ended:
            self._decode_more()
        if not len(self._buffer):
            return None
        block, self._buffer = self._buffer[:BLOCK_SAMPLES], self._buffer[BLOCK_SAMPLES:]
        self._produced += len(block)
        if self.realtime:
            wait = self._produced / SAMPLE_RATE - (time.monotonic() - self._started)
            if wait > 0:
                time.sleep(wait)
        return block

    def _decode_more(self) -> None:
        try:
            frame = next(self._frames)
        except StopIteration:
            self._ended = True
            resampled = self._resampler.resample(None)  # 取出重采样器里剩余的样本
        else:
            frame.pts = None  # 部分直播流时间戳不连续，交给重采样器按样本计数
            resampled = self._resampler.resample(frame)
        for out in resampled:
            mono = out.to_ndarray().mean(axis=0, dtype=np.float32)
            self._buffer = np.concatenate([self._buffer, mono])

    def close(self) -> None:
        if self._container is not None:
            self._container.close()
            self._container = None
