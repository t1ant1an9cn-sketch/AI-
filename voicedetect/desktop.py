"""桌面窗口：后台线程启动本地服务，用系统自带的网页引擎（Windows 为 WebView2，macOS 为 WKWebView）显示界面；
没有可用的网页引擎时改用默认浏览器打开。同一时间只开一个程序（instance.py），再打开时提示已经在运行。

访问令牌放在网址的 # 之后：这部分不会发给服务器、也不会写进日志，界面读取后保存在本页会话里。
"""

from __future__ import annotations

from importlib.resources import files
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from . import instance
from .api import create_app
from .app import Services
from .config import data_dir, database_path

log = logging.getLogger(__name__)

BROWSER_IDLE_EXIT = 60.0  # 在浏览器里打开时，网页关掉这么久（又没有在盯播）程序就自己退出
ALREADY_RUNNING = "AI 盯播助手已经打开了，请在任务栏里找到它。"

AUDIO_TYPES = (
    "音频或视频 (*.wav;*.mp3;*.m4a;*.aac;*.flac;*.ogg;*.opus;*.wma;*.mp4;*.mov;*.mkv;*.flv;*.ts;*.webm)",
    "所有文件 (*.*)",
)
LOCALIZATION = {
    "global.quitConfirmation": "确定要退出吗？正在进行的盯播会结束并保存。",
    "global.ok": "确定",
    "global.quit": "退出",
    "global.cancel": "取消",
}


def web_dir() -> Path:
    return Path(str(files("voicedetect") / "web"))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LocalServer:
    """在后台线程运行 uvicorn。"""

    def __init__(self, app, port: int) -> None:
        self.port = port
        # 不让 uvicorn 自己配置日志：它的格式化器要看命令行窗口支不支持颜色，打包后双击打开时没有命令行窗口，
        # 会直接出错。警告和错误照常写进程序的日志文件。
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", log_config=None, access_log=False
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="local-server", daemon=True)

    def start(self, timeout: float = 15.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"本地服务启动失败（端口 {self.port}）")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(5)


class DesktopApi:
    """暴露给界面的 window.pywebview.api，用来打开系统文件对话框。"""

    def __init__(self) -> None:
        self._window = None  # 下划线开头，pywebview 不会把它暴露给网页

    def pick_audio_file(self) -> str | None:
        return self._pick(self._dialogs().OPEN, file_types=AUDIO_TYPES)

    def pick_folder(self) -> str | None:
        return self._pick(self._dialogs().FOLDER)

    def save_text(self, filename: str, content: str) -> str | None:
        """让用户选保存位置后写入文本（导出复盘报告、话术框架）；取消时返回 None。"""
        path = self._pick(self._dialogs().SAVE, save_filename=filename)
        if path:
            Path(path).write_text(content, encoding="utf-8")
        return path

    def _pick(self, kind, **options) -> str | None:
        if self._window is None:
            return None
        chosen = self._window.create_file_dialog(kind, **options)
        if not chosen:
            return None
        return str(chosen if isinstance(chosen, str) else chosen[0])

    @staticmethod
    def _dialogs():
        import webview

        return webview.FileDialog


def run_app(*, browser: bool = False, port: int = 0) -> int:
    _setup_file_logging()
    if not instance.acquire(database_path().parent):
        _tell(ALREADY_RUNNING)
        return 0
    try:
        return _run(browser=browser, port=port)
    except Exception as exc:  # 双击打开的程序没有命令行窗口，出错要弹出来，不能悄悄退出
        log.exception("desktop app failed")
        _tell(f"AI 盯播助手出错了：{exc}\n\n详细信息在 {data_dir() / 'logs' / 'voicedetect.log'}")
        return 1


def _run(*, browser: bool, port: int) -> int:
    _prepare_audio()
    services = Services.create()
    token = secrets.token_urlsafe(24)
    server = LocalServer(create_app(services, token=token, web_dir=web_dir()), port or free_port())
    server.start()
    url = f"http://127.0.0.1:{server.port}/#token={token}"
    try:
        if browser or not webview2_available() or not _open_window(url):
            _open_browser(url, services)
    finally:
        services.close()
        server.stop()
    return 0


def selfcheck(*, timeout: float = 60.0) -> int:
    """打开桌面窗口，等界面显示出来就关掉，打印检查结果。检查打包后的程序用：本地服务、网页引擎和界面都要能用。"""
    import webview

    _prepare_audio()
    services = Services.create()
    token = secrets.token_urlsafe(24)
    server = LocalServer(create_app(services, token=token, web_dir=web_dir()), free_port())
    server.start()
    result = {"window": False, "title": "", "version": ""}
    try:
        window = webview.create_window(
            "AI 盯播助手（自检）", f"http://127.0.0.1:{server.port}/#token={token}", width=1200, height=800
        )

        def probe() -> None:
            deadline = time.monotonic() + timeout
            # 界面画出来了才有开播页；侧栏里的版本号是从本地服务读到的，有它说明界面连得上服务
            version = "document.querySelector('.version')?.textContent ?? ''"
            rendered = f"document.querySelector('.setup, .live') && ({version}) ? document.title : ''"
            while time.monotonic() < deadline:
                try:
                    title = window.evaluate_js(rendered)
                    if title:
                        result.update(window=True, title=title, version=window.evaluate_js(version))
                        break
                except Exception:
                    log.debug("page not ready yet", exc_info=True)
                time.sleep(0.5)
            window.destroy()

        webview.start(probe, private_mode=True)
    finally:
        services.close()
        server.stop()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["window"] else 1


def serve_forever(*, port: int, token: str) -> int:
    """只运行本地服务（前端开发时配合 Vite 使用），Ctrl+C 结束。"""
    _prepare_audio()
    services = Services.create()
    print(f"本地服务：http://127.0.0.1:{port}/#token={token}")
    try:
        uvicorn.run(create_app(services, token=token, web_dir=web_dir()), host="127.0.0.1", port=port, log_level="info")
    finally:
        services.close()
    return 0


def _open_window(url: str) -> bool:
    try:
        import webview
    except Exception:
        log.exception("pywebview unavailable")
        return False
    api = DesktopApi()
    try:
        api._window = webview.create_window(
            "AI 盯播助手",
            url,
            js_api=api,
            width=1440,
            height=920,
            min_size=(1100, 700),
            text_select=True,
            confirm_close=True,
            localization=LOCALIZATION,
            background_color="#F6F7F9",
        )
        storage = data_dir() / "webview"
        if port := os.environ.get("VOICEDETECT_DEVTOOLS_PORT"):
            # 只给自动化检查用：经这个端口操作窗口里的界面，检查打包后的程序。平时不设置，不开调试端口。
            webview.settings["REMOTE_DEBUGGING_PORT"] = int(port)
        webview.start(private_mode=False, storage_path=str(storage))
    except Exception:
        log.exception("desktop window failed, falling back to browser")
        return False
    return True


def webview2_available() -> bool:
    """Windows 上桌面窗口要用 WebView2（Windows 11 自带，Windows 10 一般随新版 Edge 装好）。
    没有时 pywebview 会退回 IE 内核，界面显示不出来，不如直接用浏览器打开。"""
    if sys.platform != "win32":
        return True
    import winreg

    client = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\{client}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\{client}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\{client}"),
    ):
        try:
            with winreg.OpenKey(root, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return True
    log.warning("WebView2 runtime not found; opening the interface in the browser")
    return False


def _open_browser(url: str, services: Services, *, idle_exit: float = BROWSER_IDLE_EXIT) -> None:
    """用默认浏览器打开界面。打包后的程序没有命令行窗口可以按 Ctrl+C，所以网页关掉一会儿、
    又没有在盯播时，程序自己退出。"""
    print("已在浏览器中打开界面。关掉网页一分钟后程序自动退出，也可以按 Ctrl+C 退出。")
    print(url)
    webbrowser.open(url)
    idle_since = time.monotonic()
    try:
        while time.monotonic() - idle_since < idle_exit:
            time.sleep(1)
            session = services.monitor.current
            if services.bus.listeners or (session is not None and not session.finished):
                idle_since = time.monotonic()
    except KeyboardInterrupt:
        pass


def _tell(message: str) -> None:
    """给用户看一句话：打包后的程序没有命令行窗口，Windows 上弹一个提示框。"""
    print(message)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "AI 盯播助手", 0x40)  # 0x40：信息图标


def _prepare_audio() -> None:
    """在主线程先加载 soundcard。

    soundcard 在 Windows 上导入时会把当前线程加入 COM 多线程套间；放在主线程做，套间在程序运行期间
    一直存在，采集线程用起来才稳定。桌面窗口在 pywebview 自己的 STA 线程里，互不影响。
    """
    try:
        import soundcard  # noqa: F401
    except Exception:
        log.warning("soundcard unavailable; device capture disabled", exc_info=True)


def _setup_file_logging() -> None:
    folder = data_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "voicedetect.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
