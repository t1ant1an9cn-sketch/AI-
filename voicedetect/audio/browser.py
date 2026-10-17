"""后台浏览器：用本机的 Edge 或 Chrome（无界面模式）打开网页，通过 DevTools 协议（CDP）观察和控制页面。

直播间链接靠它工作：网页里的播放器会去请求直播流，从网络请求里就能拿到地址；拿不到时让网页继续播放，
从网页里取解码后的声音。Windows 10/11 自带 Edge，打包成 exe 后也不用另外安装浏览器。

每次使用都新建一个临时的浏览器资料目录，用完删除，不碰用户自己的浏览器、登录状态和历史记录。
"""

from __future__ import annotations

import atexit
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.request

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any], str | None], None]  # (事件参数, 子会话 ID)

_running: set[HeadlessBrowser] = set()
_running_lock = threading.Lock()


class BrowserError(RuntimeError):
    pass


def find_browser() -> Path | None:
    """按顺序找 Edge、Chrome；环境变量 VOICEDETECT_BROWSER 可以指定浏览器程序。"""
    override = os.environ.get("VOICEDETECT_BROWSER")
    if override:
        return Path(override) if Path(override).is_file() else None
    candidates: list[Path] = []
    if sys.platform == "win32":
        for base in (
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"))
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    else:
        for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            if found := shutil.which(name):
                candidates.append(Path(found))
    return next((path for path in candidates if path.is_file()), None)


class HeadlessBrowser:
    """一个无界面浏览器进程和其中的一个标签页。用完必须 close()：结束进程并删除临时资料目录。

    命令在任意线程发送；回复和事件由后台线程接收，事件按名称分发给 on() 登记的处理函数。
    处理函数在接收线程里执行，应当很快返回。
    """

    def __init__(self, executable: Path | None = None, *, start_timeout: float = 15.0) -> None:
        from websockets.sync.client import connect

        program = executable or find_browser()
        if program is None:
            raise BrowserError("没有找到 Edge 或 Chrome 浏览器。")
        self._profile = Path(tempfile.mkdtemp(prefix="voicedetect-browser-"))
        self._connection = ExitStack()
        self._handlers: dict[str, list[Handler]] = {}
        self._pending: dict[int, Future] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._closed = False
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._process = subprocess.Popen(
            [
                str(program),
                "--headless=new",
                "--remote-debugging-port=0",
                f"--user-data-dir={self._profile}",
                "--mute-audio",  # 不从扬声器出声；网页里照常解码，取声音不受影响
                "--autoplay-policy=no-user-gesture-required",
                "--no-first-run",
                "--no-default-browser-check",
                # 新资料目录会弹出 Edge 的同步确认页并抢到前台，直播间网页随之变成“不可见”，有的播放器会静音
                "--disable-sync",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-component-update",
                "--window-size=1280,800",
                "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        with _running_lock:
            _running.add(self)
        try:
            port = self._wait_for_port(start_timeout)
            version = _http_json(port, "/json/version")
            # 无界面模式的 User-Agent 带 HeadlessChrome，有的平台会因此不给播放
            self.user_agent: str = version.get("User-Agent", "").replace("HeadlessChrome", "Chrome")
            # 用启动时自带的那个标签页；另开一个的话，自带的那个可能跑到前台，把直播间挤到后台
            pages = [item for item in _http_json(port, "/json/list") if item.get("type") == "page"]
            target = pages[0] if pages else _http_json(port, "/json/new?about:blank", method="PUT")
            self._socket = self._connection.enter_context(
                connect(target["webSocketDebuggerUrl"], max_size=None, open_timeout=10, close_timeout=2)
            )
        except Exception as exc:
            self.close()
            if isinstance(exc, BrowserError):
                raise
            raise BrowserError(f"后台浏览器启动失败：{exc}") from None
        self._reader = threading.Thread(target=self._receive, name="browser-cdp", daemon=True)
        self._reader.start()

    # —— 命令与事件 ——

    def on(self, method: str, handler: Handler) -> None:
        self._handlers.setdefault(method, []).append(handler)

    def send(self, method: str, params: dict[str, Any] | None = None, *, session: str | None = None) -> Future:
        future: Future = Future()
        with self._lock:
            self._next_id += 1
            message_id = self._next_id
            self._pending[message_id] = future
        message: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        try:
            with self._send_lock:
                self._socket.send(json.dumps(message))
        except Exception as exc:
            with self._lock:
                self._pending.pop(message_id, None)
            future.set_exception(BrowserError(f"后台浏览器已断开：{exc}"))
        return future

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0) -> dict[str, Any]:
        try:
            return self.send(method, params).result(timeout)
        except TimeoutError:
            raise BrowserError(f"后台浏览器没有响应（{method}）") from None

    @property
    def alive(self) -> bool:
        return not self._closed and self._process.poll() is None and self._reader.is_alive()

    def _receive(self) -> None:
        try:
            for raw in self._socket:
                message = json.loads(raw)
                if "id" in message:
                    with self._lock:
                        future = self._pending.pop(message["id"], None)
                    if future is not None:
                        if "error" in message:
                            future.set_exception(BrowserError(message["error"].get("message", "浏览器命令失败")))
                        else:
                            future.set_result(message.get("result", {}))
                    continue
                for handler in self._handlers.get(message.get("method", ""), []):
                    try:
                        handler(message.get("params", {}), message.get("sessionId"))
                    except Exception:
                        log.exception("browser event handler failed")
        except Exception:
            if not self._closed:
                log.warning("browser connection lost", exc_info=True)
        finally:
            with self._lock:
                pending, self._pending = self._pending, {}
            for future in pending.values():
                if not future.done():
                    future.set_exception(BrowserError("后台浏览器已关闭"))

    # —— 进程 ——

    def _wait_for_port(self, timeout: float) -> int:
        """浏览器用随机端口时，会把端口写进资料目录下的 DevToolsActivePort。"""
        port_file = self._profile / "DevToolsActivePort"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise BrowserError(f"后台浏览器启动后立即退出了（退出码 {self._process.returncode}）")
            try:
                lines = port_file.read_text(encoding="ascii").split()
                if lines:
                    return int(lines[0])
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        raise BrowserError("后台浏览器启动超时")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.close()
        except Exception:
            pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(5)
        with _running_lock:
            _running.discard(self)
        # 浏览器的子进程退出得稍晚，资料目录里的文件可能还被占用一会儿
        for _ in range(20):
            shutil.rmtree(self._profile, ignore_errors=True)
            if not self._profile.exists():
                break
            time.sleep(0.1)


def _http_json(port: int, path: str, *, method: str = "GET") -> Any:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


@atexit.register
def _close_all() -> None:
    """程序退出时收掉还开着的后台浏览器，不留孤儿进程。"""
    with _running_lock:
        browsers = list(_running)
    for browser in browsers:
        browser.close()
