"""直播间链接：粘贴任意平台的直播间网址就能盯播，不用自己找直播流地址。

做法与平台无关：用后台浏览器（browser.py）打开直播间网页，让网页自己的播放器去取流。
1. 网页请求了 flv / m3u8 直播流（抖音、快手、B 站、斗鱼等）：记下地址和请求头，关掉浏览器，由 PyAV 直接拉流。
   最省资源，之后网页弹窗、要求登录也不受影响；断流时先重连原地址，不行再重新打开网页拿新地址。
2. 网页没有这类地址（例如虎牙走 P2P 和 WebRTC）：浏览器继续播放，从网页里取解码后的声音，
   包括 <video>/<audio> 播放的声音和网页用 Web Audio 播放的声音，在网页里统一转成 16 kHz 送回程序。
两种都不行时给出明确提示，用户可以改用“系统声音”。
要登录才能看的直播间：用户先在登录窗口里登录一次（logins.py），打开网页前放入存下的 Cookie。
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import queue
import re
import threading
import time
from typing import Literal
from urllib.parse import urlsplit

import numpy as np

from ..asr.base import SAMPLE_RATE
from .base import level_db
from .browser import BrowserError, HeadlessBrowser
from .files import BLOCK_SAMPLES, MediaSource
from .logins import apply_cookies, load_cookies

log = logging.getLogger(__name__)

BINDING = "__voicedetectAudio"  # 网页里调用它把声音送回程序
STREAM_SUFFIXES = (".flv", ".m3u8")
STREAM_MIMES = {
    "video/x-flv",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
}
MEDIA_SUFFIXES = (
    *STREAM_SUFFIXES,
    ".mp4",
    ".m4a",
    ".mp3",
    ".aac",
    ".ts",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".mkv",
    ".webm",
)
STREAM_SCHEMES = ("rtmp", "rtmps", "rtsp", "srt")
PASSED_HEADERS = {"referer": "Referer", "origin": "Origin", "user-agent": "User-Agent"}
PLATFORMS = {
    "douyin.com": "抖音",
    "kuaishou.com": "快手",
    "taobao.com": "淘宝",
    "tmall.com": "天猫",
    "jd.com": "京东",
    "xiaohongshu.com": "小红书",
    "weixin.qq.com": "视频号",
    "huya.com": "虎牙",
    "douyu.com": "斗鱼",
    "bilibili.com": "B站",
    "weibo.com": "微博",
    "tiktok.com": "TikTok",
    "youtube.com": "YouTube",
    "twitch.tv": "Twitch",
}
NOT_FOUND = (
    "没有在这个直播间里找到正在播放的直播：可能还没开播、需要登录才能观看，或者这个平台的网页播放不了。"
    "要登录才能看的，点“登录平台账号”登录一次再试；也可以先在浏览器里打开直播间，音频来源改选“系统声音”。"
)
PAGE_LAG = 0.6  # 网页声音约每 0.26 秒送来一次；落后超过这么多还没有新数据就补静音
PAGE_SILENT_RELOAD = 20.0  # 这么久没收到网页的声音就刷新网页
Notice = tuple[str, str, str, str, str]  # (类别, 严重程度, 标题, 说明, 建议)，对应会话里的系统提醒


class RoomError(ValueError):
    """打不开直播间。说明文字直接展示给用户。"""


@dataclass
class Candidate:
    url: str
    headers: dict[str, str] = field(default_factory=dict)


def extract_url(text: str) -> str:
    """从粘贴的内容里取出网址：平台的分享口令常常是“一段文字 + 链接”。

    没写协议头、形如“live.douyin.com/123”的按 https 处理；必须带路径，免得把“demo.mp3”这样的文件名当成网址。
    """
    match = re.search(r"(?:https?|rtmps?|rtsp|srt)://[^\s，。！？、；：“”‘’（）《》【】]+", text, re.IGNORECASE)
    if match:
        return match.group(0)
    text = text.strip()
    if re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+/", text, re.IGNORECASE):
        return f"https://{text}"
    return text


def is_room_page(location: str) -> bool:
    """是直播间网页（需要打开网页取声音），还是可以直接拉的直播流或媒体文件地址。"""
    parts = urlsplit(extract_url(location))
    scheme = parts.scheme.lower()
    return scheme in ("http", "https") and not parts.path.lower().endswith(MEDIA_SUFFIXES)


def platform_name(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for domain, name in PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host.removeprefix("www.") or "网页"


def clean_title(title: str) -> str:
    """网页标题去掉平台后缀，例如“某某的抖音直播间 - 抖音直播”→“某某的抖音直播间”。"""
    return re.split(r"\s*[-_|｜—]\s*", title.strip(), maxsplit=1)[0].strip()[:40]


def _looks_like_stream(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and parts.path.lower().endswith(STREAM_SUFFIXES)


class RoomSource:
    """直播间网页来源。prepare() 打开直播间并决定取声音的方式，要在开始盯播前调用（几秒到二十几秒）。"""

    live = True

    def __init__(
        self,
        url: str,
        *,
        resolve_timeout: float = 25.0,
        page_audio_after: float = 10.0,
        browser: Callable[[], HeadlessBrowser] = HeadlessBrowser,
        cookies: Callable[[], list[dict]] = load_cookies,
    ) -> None:
        self.url = extract_url(url)
        self.platform = platform_name(self.url)
        self.label = f"{self.platform}直播间"
        self.title = ""
        self.mode: Literal["stream", "page"] | None = None
        self._resolve_timeout = resolve_timeout
        self._page_audio_after = page_audio_after  # 等这么久还没有直播流地址、网页已经在出声，就改为从网页取声音
        self._new_browser = browser
        self._cookies = cookies  # 用户在登录窗口里登录后存下的 Cookie
        self._browser: HeadlessBrowser | None = None
        self._stream: MediaSource | None = None
        self._candidate: Candidate | None = None
        self._first_block: np.ndarray | None = None  # 试拉时读到的第一块，不浪费
        self._pcm: queue.Queue[np.ndarray] = queue.Queue()
        self._page_buffer = np.zeros(0, dtype=np.float32)
        self._page_heard = 0.0  # 收到的有声网页音频（秒），用来判断网页是否真的在播放
        self._clock = 0.0
        self._produced = 0
        self._last_audio = 0.0
        self._last_reload = 0.0
        self._notices: list[Notice] = []
        self._cancel = threading.Event()

    # —— 来源接口 ——

    def prepare(self) -> None:
        if self.mode is None:
            self._resolve()
            self.label = f"{self.platform}：{self.title}" if self.title else f"{self.platform}直播间"

    def open(self) -> None:
        self.prepare()
        if self.mode == "page":
            self._start_page_clock()

    def read(self) -> np.ndarray | None:
        if self._cancel.is_set():
            return None
        if self.mode == "page":
            return self._read_page()
        return self._read_stream()

    def interrupt(self) -> None:
        """停止盯播时调用：让正在等待重连或等待网页声音的 read() 尽快返回。"""
        self._cancel.set()

    def close(self) -> None:
        self._cancel.set()
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def take_notices(self) -> list[Notice]:
        notices, self._notices = self._notices, []
        return notices

    # —— 打开直播间 ——

    def _resolve(self) -> None:
        """打开网页，优先找直播流地址；找不到但网页在出声时，改为从网页取声音。"""
        try:
            browser = self._new_browser()
        except BrowserError as exc:
            raise RoomError(f"{exc}打不开直播间链接，可以在浏览器里打开直播间，音频来源改选“系统声音”。") from None
        keep = False
        candidates: list[Candidate] = []
        tried: set[str] = set()
        self._page_heard = 0.0
        try:
            if cookies := self._cookies():
                applied = apply_cookies(browser.call, cookies)
                log.info("room %s: applied %d of %d saved login cookies", self.url, applied, len(cookies))
            self._watch(browser, candidates)
            started = time.monotonic()
            result = browser.call("Page.navigate", {"url": self.url}, timeout=30)
            if result.get("errorText"):
                raise RoomError(f"打不开这个网址（{result['errorText']}），请检查链接是否正确、网络是否正常。")
            while not self._cancel.is_set():
                for candidate in [item for item in candidates if item.url not in tried]:
                    tried.add(candidate.url)
                    stream = self._try_stream(candidate)
                    if stream is not None:
                        self._stream, self._candidate, self.mode = stream, candidate, "stream"
                        # 很快就找到流时网页可能还没换上直播间名称，稍等一下
                        settle = min(3.0, max(0.0, 8.0 - (time.monotonic() - started)))
                        self.title = self.title or self._page_title(browser, settle=settle)
                        log.info("room %s: pulling stream %s", self.url, urlsplit(candidate.url).hostname)
                        return
                elapsed = time.monotonic() - started
                # 找到的流地址都拉不动（例如节点连不上）而网页在出声，就不必再等
                if self._page_heard >= 1.0 and (elapsed >= self._page_audio_after or tried):
                    self.mode, keep = "page", True
                    self.title = self.title or self._page_title(browser)
                    log.info("room %s: capturing page audio", self.url)
                    return
                if elapsed >= self._resolve_timeout:
                    raise RoomError(NOT_FOUND)
                if not browser.alive:
                    raise RoomError("后台浏览器意外退出了，请重试。")
                time.sleep(0.2)
            raise RoomError("已取消")
        except BrowserError as exc:
            raise RoomError(f"打开直播间失败：{exc}") from None
        finally:
            if keep:
                self._browser = browser
            else:
                browser.close()
                self._drain_page_audio()

    def _watch(self, browser: HeadlessBrowser, candidates: list[Candidate]) -> None:
        """登记网络请求、网页声音的监听，并让网页在加载前就装好取声音的脚本。"""
        first_requests: dict[tuple[str | None, str], tuple[str, dict]] = {}

        def add(url: str, headers: dict[str, str]) -> None:
            if not url or any(item.url == url for item in candidates):
                return
            passed = {
                PASSED_HEADERS[name.lower()]: value for name, value in headers.items() if name.lower() in PASSED_HEADERS
            }
            passed.setdefault("Referer", self.url)
            passed.setdefault("User-Agent", browser.user_agent)
            candidates.append(Candidate(url, passed))

        def on_request(params: dict, session: str | None) -> None:
            request = params.get("request", {})
            if "redirectResponse" in params:
                return  # 跳转后的地址多半只能用一次，用网页最初请求的那个
            first_requests[(session, params.get("requestId", ""))] = (
                request.get("url", ""),
                request.get("headers", {}),
            )
            if _looks_like_stream(request.get("url", "")):
                add(request["url"], request.get("headers", {}))

        def on_response(params: dict, session: str | None) -> None:
            mime = params.get("response", {}).get("mimeType", "").lower()
            direct = params.get("type") == "Document" and mime.startswith(("audio/", "video/"))  # 填的就是流地址
            if mime in STREAM_MIMES or direct:
                url, headers = first_requests.get((session, params.get("requestId", "")), ("", {}))
                add(url, headers)

        def on_attached(params: dict, _session: str | None) -> None:
            child = params.get("sessionId")
            if child:  # 附加时目标处于暂停状态，开启网络监听后放行
                browser.send("Network.enable", session=child)
                browser.send("Runtime.runIfWaitingForDebugger", session=child)

        def on_binding(params: dict, _session: str | None) -> None:
            if params.get("name") == BINDING:
                self._push_pcm(params.get("payload", ""))

        browser.on("Network.requestWillBeSent", on_request)
        browser.on("Network.responseReceived", on_response)
        browser.on("Target.attachedToTarget", on_attached)
        browser.on("Runtime.bindingCalled", on_binding)
        browser.call("Network.enable")
        browser.call("Page.enable")
        browser.call("Runtime.enable")
        browser.call(
            "Network.setUserAgentOverride", {"userAgent": browser.user_agent, "acceptLanguage": "zh-CN,zh;q=0.9"}
        )
        browser.call("Runtime.addBinding", {"name": BINDING})
        browser.call("Page.addScriptToEvaluateOnNewDocument", {"source": PAGE_AUDIO_JS})
        # 播放器常在 worker 里取流，要附加到 worker 和 iframe 上才看得到这些请求
        browser.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})

    def _try_stream(self, candidate: Candidate) -> MediaSource | None:
        """试拉一下：能打开、读到第一块音频才算可用。同一地址偶尔第一次连不上，再试一次。"""
        for attempt in range(2):
            stream = MediaSource(candidate.url, headers=candidate.headers, label=self.label, timeout=10)
            try:
                stream.open()
                first = stream.read()
            except Exception as exc:
                log.info("stream candidate failed (attempt %d): %s", attempt + 1, exc)
                stream.close()
                continue
            if first is None:
                stream.close()
                continue
            self._first_block = first
            return stream
        return None

    def _page_title(self, browser: HeadlessBrowser, *, settle: float = 0.0) -> str:
        """网页标题。很多直播间网页先显示通用标题，房间信息加载完才换成直播间名称，最多等 settle 秒看它变化。"""

        def current() -> str:
            try:
                result = browser.call(
                    "Runtime.evaluate", {"expression": "document.title", "returnByValue": True}, timeout=5
                )
            except BrowserError:
                return ""
            return str(result.get("result", {}).get("value") or "")

        first = latest = current()
        deadline = time.monotonic() + settle
        while latest == first and time.monotonic() < deadline:
            time.sleep(0.5)
            latest = current()
        return clean_title(latest)

    # —— 直接拉流 ——

    def _read_stream(self) -> np.ndarray | None:
        while not self._cancel.is_set():
            if self._first_block is not None:
                block, self._first_block = self._first_block, None
                return block
            block = None
            try:
                block = self._stream.read() if self._stream is not None else None
            except Exception as exc:  # 网络中断、CDN 断开连接
                log.warning("live stream read failed: %s", exc)
            if block is not None:
                return block
            if not self._reconnect():
                return None
            if self.mode == "page":
                return self._read_page()
        return None

    def _reconnect(self) -> bool:
        """直播流断了：先重连原地址，不行再重新打开网页拿新地址；都不行就认为直播结束了。"""
        lost = time.monotonic()
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        for wait in (1.0, 3.0):
            if self._cancel.wait(wait):
                return False
            stream = self._try_stream(self._candidate) if self._candidate else None
            if stream is not None:
                self._stream = stream
                self._reconnected(lost)
                return True
        self.mode = None
        try:
            self._resolve()
        except RoomError as exc:
            if self._cancel.is_set():
                return False
            log.info("reopening room failed: %s", exc)
            self._notices.append(
                (
                    "stream_ended",
                    "high",
                    "直播流断开了",
                    "重新连接没有成功，直播可能已经结束，盯播到此停止。",
                    "如果直播还在继续，请重新开始盯播。",
                )
            )
            return False
        if self.mode == "page":
            self._start_page_clock()
        self._reconnected(lost)
        return True

    def _reconnected(self, lost: float) -> None:
        gap = time.monotonic() - lost
        self._notices.append(
            (
                "stream_reconnected",
                "low",
                "直播流中断后已重新连接",
                f"中断了约 {gap:.0f} 秒，这段时间的讲解没有收到。",
                "",
            )
        )

    # —— 从网页取声音 ——

    def _push_pcm(self, payload: str) -> None:
        """在浏览器接收线程里调用：网页送来的一段 16 位 PCM（base64）。"""
        try:
            samples = np.frombuffer(base64.b64decode(payload), dtype="<i2").astype(np.float32) / 32768
        except ValueError:
            return
        if level_db(samples) > -60:
            self._page_heard += len(samples) / SAMPLE_RATE
        if self._pcm.qsize() > 1200:  # 约 5 分钟没人读（不应发生），丢掉最旧的，免得占满内存
            self._drain_page_audio()
        self._pcm.put(samples)

    def _drain_page_audio(self) -> None:
        while True:
            try:
                self._pcm.get_nowait()
            except queue.Empty:
                return

    def _start_page_clock(self) -> None:
        """从现在起计时；开始盯播前积下的网页声音丢掉，时间轴从开始盯播算起。"""
        self._drain_page_audio()
        self._page_buffer = np.zeros(0, dtype=np.float32)
        self._clock = self._last_audio = time.monotonic()
        self._produced = 0

    def _read_page(self) -> np.ndarray | None:
        """网页的声音按实际时间送来；一时没有（缓冲、弹窗暂停）就补静音，“没有收到声音”的提醒才能起作用。"""
        while len(self._page_buffer) < BLOCK_SAMPLES:
            if self._cancel.is_set():
                return None
            behind = (time.monotonic() - self._clock) * SAMPLE_RATE - self._produced
            if behind > PAGE_LAG * SAMPLE_RATE:
                padding = np.zeros(BLOCK_SAMPLES - len(self._page_buffer), dtype=np.float32)
                self._page_buffer = np.concatenate([self._page_buffer, padding])
                self._watch_page()
                break
            try:
                chunk = self._pcm.get(timeout=0.05)
            except queue.Empty:
                continue
            self._page_buffer = np.concatenate([self._page_buffer, chunk])
            self._last_audio = time.monotonic()
        block, self._page_buffer = self._page_buffer[:BLOCK_SAMPLES], self._page_buffer[BLOCK_SAMPLES:]
        self._produced += len(block)
        return block

    def _watch_page(self) -> None:
        browser = self._browser
        if browser is None or not browser.alive:
            raise RoomError("后台浏览器意外退出了，直播间的声音中断，请重新开始盯播。")
        silent = time.monotonic() - self._last_audio
        if silent > PAGE_SILENT_RELOAD and time.monotonic() - self._last_reload > 60:
            self._last_reload = time.monotonic()
            browser.send("Page.reload", {"ignoreCache": True})
            self._notices.append(
                (
                    "page_reloaded",
                    "low",
                    "直播间网页没有声音，已自动刷新",
                    f"已经 {silent:.0f} 秒没有收到网页的声音。",
                    "",
                )
            )


# 在网页加载前注入：把网页播放的声音转成 16 kHz 单声道，约每 0.26 秒调用一次 BINDING 送回程序。
PAGE_AUDIO_JS = r"""
(() => {
  // 只在顶层页面取声音；各个 iframe 各取一份会把几路声音交错送回。
  if (window.top !== window || window.__voicedetectTap) return;
  window.__voicedetectTap = true;
  const stats = (window.__voicedetectStats = { chunks: 0, contexts: 0, elements: 0, state: '' });  // 排查用

  // 后台浏览器里的网页有时被判为“不可见”，有的播放器（例如虎牙）会因此静音：让网页始终以为自己在前台。
  try {
    const visible = { get: () => 'visible', configurable: true };
    Object.defineProperty(Document.prototype, 'visibilityState', visible);
    Object.defineProperty(Document.prototype, 'webkitVisibilityState', visible);
    Object.defineProperty(Document.prototype, 'hidden', { get: () => false, configurable: true });
    const swallow = (event) => event.stopImmediatePropagation();
    window.addEventListener('visibilitychange', swallow, true);
    document.addEventListener('visibilitychange', swallow, true);
  } catch (e) {}

  const nativeConnect = AudioNode.prototype.connect;
  let ctx = null;
  let mixer = null;

  function ensure() {
    if (ctx) return true;
    try { ctx = new AudioContext({ sampleRate: 16000 }); } catch (e) { return false; }
    mixer = ctx.createGain();
    const processor = ctx.createScriptProcessor(4096, 1, 1);
    const sink = ctx.createGain();
    sink.gain.value = 0;  // 处理节点要接到输出才会运行；增益为 0，不出声
    nativeConnect.call(mixer, processor);
    nativeConnect.call(processor, sink);
    nativeConnect.call(sink, ctx.destination);
    processor.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      const pcm = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        const x = Math.max(-1, Math.min(1, input[i]));
        pcm[i] = x < 0 ? x * 32768 : x * 32767;
      }
      const bytes = new Uint8Array(pcm.buffer);
      let text = '';
      for (let i = 0; i < bytes.length; i += 0x8000) {
        text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      }
      try { window.__voicedetectAudio(btoa(text)); } catch (e) {}
      stats.chunks++;
    };
    ctx.onstatechange = () => { stats.state = ctx.state; };
    ctx.resume();
    return true;
  }

  // 网页用 Web Audio 播放的声音（例如虎牙）：凡是接到扬声器的节点，同时接一份给我们。
  const taps = new WeakMap();
  AudioNode.prototype.connect = function (target, ...rest) {
    const result = nativeConnect.call(this, target, ...rest);
    try {
      if (target instanceof AudioDestinationNode && this.context !== ctx && ensure()) {
        let tap = taps.get(this.context);
        if (!tap) {
          tap = this.context.createMediaStreamDestination();
          taps.set(this.context, tap);
          nativeConnect.call(ctx.createMediaStreamSource(tap.stream), mixer);
          stats.contexts++;
        }
        nativeConnect.call(this, tap);
      }
    } catch (e) {}
    return result;
  };

  // <video>/<audio> 播放的声音（例如抖音）：只取 MediaStream 或 MediaSource（blob:）来源；
  // 跨域的普通文件（广告、封面视频）浏览器不允许取，也不是直播。
  const tapped = new WeakSet();
  function tapElements() {
    for (const el of document.querySelectorAll('video, audio')) {
      if (tapped.has(el) || el.paused || el.readyState < 2) continue;
      const own = el.srcObject instanceof MediaStream ? el.srcObject : null;
      if (!own && !(el.currentSrc || '').startsWith('blob:')) continue;
      let stream;
      try { stream = own || el.captureStream(); } catch (e) { continue; }
      if (!stream.getAudioTracks().length) continue;  // 只有画面的流，声音另走 Web Audio
      if (!ensure()) return;
      tapped.add(el);
      el.muted = false;  // 浏览器整体静音，取消元素静音才取得到声音，扬声器也不会出声
      nativeConnect.call(ctx.createMediaStreamSource(stream), mixer);
      stats.elements++;
    }
  }
  setInterval(tapElements, 1000);
})();
"""
