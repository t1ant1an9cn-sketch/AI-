"""直播平台登录：有的直播间要登录才能看（例如淘宝直播）。

用户在程序打开的浏览器窗口里登录一次（一般是用手机 App 扫码），程序把这个窗口里的 Cookie 存到本机；
之后打开直播间时，后台浏览器先放入这些 Cookie 再打开网页。

- 登录窗口用固定的资料目录（数据目录下的 logins/profile），下次打开时网页里的其他状态还在。
- 不少网站的登录 Cookie 是“会话 Cookie”，浏览器一关就没了，所以窗口开着时每 2 秒存一次；
  再次打开登录窗口时，也先把存下的 Cookie 放回去，再打开网页。
- 后台浏览器仍然每次用新的临时资料目录，只带上存下的 Cookie，登录窗口开着也互不影响。
- Cookie 文件在 Windows 上用 DPAPI 加密，只有这台电脑上的当前用户能解开。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from ..config import data_dir
from .browser import COMMON_FLAGS, BrowserError, find_browser, http_json, launch, remove_profile, stop_process

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0
COOKIE_FIELDS = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "partitionKey")


def logins_dir() -> Path:
    return data_dir() / "logins"


def cookies_path() -> Path:
    return logins_dir() / "cookies.bin"


def saved_at() -> float | None:
    path = cookies_path()
    return path.stat().st_mtime if path.is_file() else None


def load_cookies() -> list[dict[str, Any]]:
    """存下的 Cookie（可以直接交给浏览器设置）；没有存过或解不开时返回空列表。"""
    path = cookies_path()
    if not path.is_file():
        return []
    try:
        return json.loads(_unprotect(path.read_bytes()))["cookies"]
    except Exception:
        log.warning("saved login cookies are unreadable; ignoring them", exc_info=True)
        return []


def save_cookies(cookies: list[dict[str, Any]]) -> None:
    path = cookies_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(_protect(json.dumps({"cookies": cookies}, ensure_ascii=False).encode()))
    if sys.platform != "win32":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)  # 写完再换上，中途退出不会留下半个文件


def to_params(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """浏览器读出的 Cookie → 设置 Cookie 用的参数。会话 Cookie 不带过期时间，放回去后同样随浏览器关闭失效。"""
    params = []
    for cookie in cookies:
        item = {key: cookie[key] for key in COOKIE_FIELDS if key in cookie}
        if not cookie.get("session") and cookie.get("expires", -1) > 0:
            item["expires"] = cookie["expires"]
        params.append(item)
    return params


def apply_cookies(call: Callable[..., Any], cookies: list[dict[str, Any]]) -> int:
    """把 Cookie 放进浏览器，返回放进去的个数。整批被拒时逐个放，跳过放不进去的那几个。"""
    if not cookies:
        return 0
    try:
        call("Storage.setCookies", {"cookies": cookies})
        return len(cookies)
    except BrowserError as exc:
        log.info("setting %d cookies at once failed (%s); trying one by one", len(cookies), exc)
    count = 0
    for cookie in cookies:
        try:
            call("Storage.setCookies", {"cookies": [cookie]})
            count += 1
        except BrowserError:
            log.debug("cookie %s for %s was rejected", cookie.get("name"), cookie.get("domain"))
    return count


class LoginWindow:
    """程序打开的登录窗口：一个普通的浏览器窗口，用户在里面登录直播平台。同一时间最多开一个。

    窗口开着时，后台线程每隔 POLL_SECONDS 秒读一次窗口里的全部 Cookie，有变化就存下来；
    用户关掉窗口后线程结束。on_change 在状态变化时调用（参数同 status()），可能在后台线程里执行。
    """

    def __init__(
        self,
        *,
        on_change: Callable[[dict[str, Any]], None] | None = None,
        headless: bool = False,
        executable: Path | None = None,
    ) -> None:
        self._on_change = on_change
        self._headless = headless  # 测试时不弹出窗口
        self._executable = executable
        self._lock = threading.Lock()  # 打开、关闭窗口
        self._send_lock = threading.Lock()  # 调试命令一问一答，同一时间只发一条
        self._connection = ExitStack()
        self._socket = None
        self._process: subprocess.Popen | None = None
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        self._open = False
        self._next_id = 0
        self._saved = ""  # 上次存下的内容，没变就不重写文件

    @property
    def is_open(self) -> bool:
        return self._open

    def status(self) -> dict[str, Any]:
        return {"open": self._open, "saved_at": saved_at()}

    def open(self, url: str) -> dict[str, Any]:
        """在登录窗口里打开网址；窗口已经开着时新开一个标签页。"""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("请先粘贴直播间的网页链接")
        with self._lock:
            if self._open:
                try:
                    self._show(url)
                except BrowserError:  # 窗口刚被关掉，等后台线程收尾后重新打开
                    if self._watcher is not None:
                        self._watcher.join(15)
            if not self._open:
                self._start(url)
        self._changed()
        return self.status()

    def close(self) -> None:
        """关掉登录窗口，关之前再存一次 Cookie。程序退出时也会调用。"""
        with self._lock:
            if not self._open:
                return
            try:
                self._snapshot()
                self._call("Browser.close", timeout=5)
            except BrowserError:
                pass
            self._stop.set()
            watcher = self._watcher
        if watcher is not None:
            watcher.join(15)

    def clear(self) -> dict[str, Any]:
        """关掉登录窗口，删除存下的 Cookie 和登录窗口的资料目录。"""
        self.close()
        cookies_path().unlink(missing_ok=True)
        remove_profile(logins_dir())
        self._saved = ""
        self._changed()
        return self.status()

    # —— 窗口 ——

    def _start(self, url: str) -> None:
        profile = logins_dir() / "profile"
        profile.mkdir(parents=True, exist_ok=True)
        port = _port_in_use(profile)  # 程序上次没有正常退出，留下的登录窗口还开着：接着用它
        fresh = port is None
        if port is None:
            program = self._executable or find_browser()
            if program is None:
                raise BrowserError("没有找到 Edge 或 Chrome 浏览器。")
            flags = [*COMMON_FLAGS, "--new-window", "about:blank"]
            if self._headless:
                flags.insert(0, "--headless=new")
            self._process, port = launch(program, profile, flags, timeout=15, name="登录窗口")
        try:
            from websockets.sync.client import connect

            version = http_json(port, "/json/version")
            self._connection = ExitStack()
            self._socket = self._connection.enter_context(
                connect(version["webSocketDebuggerUrl"], max_size=None, open_timeout=10, close_timeout=2)
            )
            if fresh:
                apply_cookies(self._call, load_cookies())  # 先放回存下的，打开网页时才是登录状态
                pages = [item for item in self._call("Target.getTargets")["targetInfos"] if item["type"] == "page"]
                self._call("Target.createTarget", {"url": url})
                for page in pages:  # 启动时自带的空白页
                    self._call("Target.closeTarget", {"targetId": page["targetId"]})
            else:
                self._show(url)
        except Exception as exc:
            self._disconnect(wait=False)
            raise BrowserError(f"打开登录窗口失败：{exc}") from None
        self._open = True
        self._stop.clear()
        self._watcher = threading.Thread(target=self._watch, name="login-window", daemon=True)
        self._watcher.start()

    def _show(self, url: str) -> None:
        target = self._call("Target.createTarget", {"url": url})["targetId"]
        self._call("Target.activateTarget", {"targetId": target})
        window = self._call("Browser.getWindowForTarget", {"targetId": target})
        if window.get("bounds", {}).get("windowState") == "minimized":
            self._call("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": "normal"}})

    def _watch(self) -> None:
        try:
            while True:
                self._snapshot()
                if self._stop.wait(POLL_SECONDS):
                    break
        except BrowserError as exc:  # 用户关掉了窗口
            log.info("login window closed: %s", exc)
        except Exception:
            log.exception("login window watcher failed")
        finally:
            self._disconnect(wait=True)
            self._changed()

    def _snapshot(self) -> None:
        cookies = to_params(self._call("Storage.getCookies")["cookies"])
        text = json.dumps(cookies, ensure_ascii=False, sort_keys=True)
        if text == self._saved:
            return
        if not cookies and saved_at() is not None:
            return  # 浏览器正在退出时可能读到空列表，不能拿它盖掉已经存下的登录信息
        save_cookies(cookies)
        self._saved = text
        self._changed()

    def _disconnect(self, *, wait: bool) -> None:
        self._socket = None
        try:
            self._connection.close()
        except Exception:
            pass
        process, self._process = self._process, None
        if process is not None:
            try:
                process.wait(10 if wait else 0)  # 用户关掉窗口后浏览器自己会退出
            except subprocess.TimeoutExpired:
                stop_process(process)
        self._open = False

    def _call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 10) -> dict[str, Any]:
        """向浏览器发一条调试命令并等它回复。"""
        socket = self._socket
        if socket is None:
            raise BrowserError("登录窗口已关闭")
        with self._send_lock:
            self._next_id += 1
            ident = self._next_id
            deadline = time.monotonic() + timeout
            try:
                socket.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
                while True:
                    message = json.loads(socket.recv(timeout=max(0.1, deadline - time.monotonic())))
                    if message.get("id") == ident:
                        break
            except TimeoutError:
                raise BrowserError(f"登录窗口没有响应（{method}）") from None
            except Exception as exc:
                raise BrowserError(f"登录窗口已关闭：{exc}") from None
        if "error" in message:
            raise BrowserError(message["error"].get("message", "浏览器命令失败"))
        return message.get("result", {})

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change(self.status())
            except Exception:
                log.exception("login status listener failed")


def _port_in_use(profile: Path) -> int | None:
    """资料目录里记着的调试端口还能连上，说明用这个资料目录的浏览器还开着。"""
    try:
        port = int((profile / "DevToolsActivePort").read_text(encoding="ascii").split()[0])
        version = http_json(port, "/json/version", timeout=2)
    except Exception:
        return None
    return port if isinstance(version, dict) and "webSocketDebuggerUrl" in version else None


# —— Windows DPAPI ——


def _protect(data: bytes) -> bytes:
    return _dpapi("CryptProtectData", data) if sys.platform == "win32" else data


def _unprotect(data: bytes) -> bytes:
    return _dpapi("CryptUnprotectData", data) if sys.platform == "win32" else data


def _dpapi(function: str, data: bytes) -> bytes:
    """用当前 Windows 用户的密钥加密或解密：换一个用户或换一台电脑都解不开。"""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    call = getattr(crypt32, function)
    blob = ctypes.POINTER(Blob)
    call.argtypes = [blob, ctypes.c_void_p, blob, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob]
    call.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    if not call(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)):  # 0x1：不弹任何提示
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))
