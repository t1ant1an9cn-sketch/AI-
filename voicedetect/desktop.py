"""桌面窗口：后台线程启动本地服务，用系统自带的网页引擎（Windows 为 WebView2，macOS 为 WKWebView）显示界面；
没有可用的网页引擎时改用默认浏览器打开。

访问令牌放在网址的 # 之后：这部分不会发给服务器、也不会写进日志，界面读取后保存在本页会话里。
"""

from __future__ import annotations

from importlib.resources import files
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import secrets
import socket
import threading
import time
import webbrowser

import uvicorn

from .api import create_app
from .app import Services
from .config import data_dir

log = logging.getLogger(__name__)

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
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
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
    _prepare_audio()
    services = Services.create()
    token = secrets.token_urlsafe(24)
    server = LocalServer(create_app(services, token=token, web_dir=web_dir()), port or free_port())
    server.start()
    url = f"http://127.0.0.1:{server.port}/#token={token}"
    try:
        if browser or not _open_window(url):
            _open_browser(url)
    finally:
        services.close()
        server.stop()
    return 0


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
        webview.start(private_mode=False, storage_path=str(storage))
    except Exception:
        log.exception("desktop window failed, falling back to browser")
        return False
    return True


def _open_browser(url: str) -> None:
    print("没有可用的桌面窗口组件，已在浏览器中打开界面。关闭这个窗口或按 Ctrl+C 退出。")
    print(url)
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


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
