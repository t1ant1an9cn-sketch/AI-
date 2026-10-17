"""本地接口：界面通过 HTTP 读写数据，通过 WebSocket（/api/events）接收实时事件。

只监听 127.0.0.1。每个请求都要带启动时生成的令牌（请求头 X-Token；WebSocket 和录音地址用 ?token=，
因为浏览器的 WebSocket 和 <audio> 元素都没法加请求头），并检查 Host 头，防止网页借 DNS 重绑定
访问本地接口。界面使用 hash 路由，静态文件不需要回退规则。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
import secrets
import sys
import time
from typing import Any
import uuid

import anyio
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .analysis.llm import LlmError, LlmReviewer
from .app import Services
from .asr.base import RecognizerError
from .asr.models import ModelError
from .audio.browser import BrowserError
from .audio.recording import recording_seconds
from .audio.room import extract_url
from .config import SECRET_MASK, AppSettings, LlmSettings, data_dir, recording_path, uploads_dir
from .export import session_markdown
from .frameworks import Framework, new_id
from .monitor.manager import SessionBusy, StartRequest

log = logging.getLogger(__name__)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
UNSAFE_NAME = re.compile(r"[^\w.\-()]+")  # \w 包含中文
AUDIO_PATH = re.compile(r"/api/sessions/[^/]+/audio")


class FolderRequest(BaseModel):
    folder: str


class UrlRequest(BaseModel):
    url: str


def create_app(services: Services, *, token: str, web_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="AI 盯播助手", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not _local_host(request.headers.get("host", "")):
            return JSONResponse({"detail": "只允许本机访问"}, status_code=403)
        if request.url.path.startswith("/api/") and not _authorized(request, token):
            return JSONResponse({"detail": "缺少或错误的访问令牌"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": _validation_message(exc.errors())}, status_code=422)

    for error, status in (
        (ValueError, 400),
        (RecognizerError, 400),
        (ModelError, 400),
        (LlmError, 400),
        (BrowserError, 400),
        (SessionBusy, 409),
    ):
        app.add_exception_handler(error, _handler(status))

    # —— 基本信息与设备 ——

    @app.get("/api/info")
    def info() -> dict[str, Any]:
        from .audio import apps
        from .audio.devices import loopback_supported

        return {
            "version": __version__,
            "platform": sys.platform,
            "loopback_supported": loopback_supported(),
            "app_capture_supported": apps.supported(),
            "data_dir": str(data_dir()),
        }

    @app.get("/api/devices")
    def devices() -> list[dict[str, Any]]:
        from .audio.devices import list_devices

        try:
            return list_devices()
        except Exception as exc:
            raise HTTPException(500, f"读取音频设备失败：{exc}") from None

    @app.get("/api/apps")
    def apps() -> list[dict[str, Any]]:
        """用着声音的程序，可以只听其中一个（Windows 11）。"""
        from .audio.apps import list_apps

        try:
            return list_apps()
        except Exception as exc:
            raise HTTPException(500, f"读取正在出声的程序失败：{exc}") from None

    # —— 话术框架 ——

    @app.get("/api/frameworks")
    def list_frameworks() -> list[dict[str, Any]]:
        return services.storage.list_frameworks()

    @app.get("/api/frameworks/{framework_id}")
    def get_framework(framework_id: str) -> Framework:
        return _framework_or_404(services, framework_id)

    @app.post("/api/frameworks", status_code=201)
    def create_framework(framework: Framework) -> Framework:
        """新建或导入；总是分配新 ID，导入同一个文件两次不会互相覆盖。"""
        return services.storage.save_framework(framework.model_copy(update={"id": new_id()}))

    @app.put("/api/frameworks/{framework_id}")
    def update_framework(framework_id: str, framework: Framework) -> Framework:
        _framework_or_404(services, framework_id)
        return services.storage.save_framework(framework.model_copy(update={"id": framework_id}))

    @app.delete("/api/frameworks/{framework_id}", status_code=204)
    def delete_framework(framework_id: str) -> None:
        if not services.storage.delete_framework(framework_id):
            raise HTTPException(404, "找不到这个话术框架")

    # —— 设置 ——

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return services.settings().masked()

    @app.put("/api/settings")
    def put_settings(update: AppSettings) -> dict[str, Any]:
        return services.save_settings(services.settings().merged(update)).masked()

    @app.post("/api/llm/test")
    def test_llm(llm: LlmSettings) -> dict[str, Any]:
        api_key = services.settings().llm.api_key if llm.api_key == SECRET_MASK else llm.api_key
        started = time.monotonic()
        reply = LlmReviewer(base_url=llm.base_url, api_key=api_key, model=llm.model, timeout=30).ping()
        return {"reply": reply.strip(), "seconds": round(time.monotonic() - started, 1)}

    # —— 识别模型 ——

    @app.get("/api/models")
    def model_status() -> dict[str, Any]:
        return services.downloads.status()

    @app.post("/api/models/download")
    def download_model() -> dict[str, Any]:
        return services.downloads.start()

    @app.post("/api/models/cancel")
    def cancel_download() -> dict[str, Any]:
        services.downloads.cancel()
        return services.downloads.status()

    @app.post("/api/models/import")
    def import_model(request: FolderRequest) -> dict[str, Any]:
        services.downloads.store.import_from(Path(request.folder))
        return services.downloads.status()

    # —— 直播平台登录 ——

    @app.get("/api/logins")
    def login_status() -> dict[str, Any]:
        return services.logins.status()

    @app.post("/api/logins/open")
    def open_login(request: UrlRequest) -> dict[str, Any]:
        """弹出登录窗口并打开这个直播间，用户在窗口里登录（分享口令里的链接也能认出来）。"""
        return services.logins.open(extract_url(request.url))

    @app.delete("/api/logins")
    def clear_logins() -> dict[str, Any]:
        return services.logins.clear()

    # —— 当前盯播 ——

    @app.get("/api/session")
    def current_session() -> dict[str, Any] | None:
        session = services.monitor.current
        return session.snapshot() if session else None

    @app.post("/api/session/start")
    def start_session(request: StartRequest) -> dict[str, Any]:
        return services.monitor.start(request).snapshot()

    @app.post("/api/session/stop")
    def stop_session() -> dict[str, Any] | None:
        session = services.monitor.stop()
        return session.snapshot() if session else None

    # —— 历史记录 ——

    @app.get("/api/sessions")
    def list_sessions(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        items, total = services.storage.list_sessions(limit=min(max(limit, 1), 200), offset=max(offset, 0))
        return {"items": items, "total": total}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        detail = _session_or_404(services, session_id)
        return {**detail, "recording": _recording_info(session_id)}

    @app.get("/api/sessions/{session_id}/report.md")
    def session_report(session_id: str) -> PlainTextResponse:
        detail = _session_or_404(services, session_id)
        return PlainTextResponse(session_markdown(detail), media_type="text/markdown; charset=utf-8")

    @app.get("/api/sessions/{session_id}/audio")
    def session_audio(session_id: str) -> FileResponse:
        """本场录音。支持 Range 请求，播放器可以直接拖动到任意位置。"""
        path = recording_path(session_id)
        if not path.is_file():
            raise HTTPException(404, "这一场没有录音")
        return FileResponse(path, media_type="audio/wav")

    @app.delete("/api/sessions/{session_id}", status_code=204)
    def delete_session(session_id: str) -> None:
        current = services.monitor.current
        if current is not None and current.session_id == session_id and not current.finished:
            raise HTTPException(409, "这一场还在进行，请先停止")
        if not services.storage.delete_session(session_id):
            raise HTTPException(404, "找不到这一场记录")
        _remove_recording(session_id)

    # —— 上传音频（浏览器模式下没有系统文件对话框） ——

    @app.put("/api/uploads")
    async def upload(request: Request, name: str) -> dict[str, str]:
        safe = UNSAFE_NAME.sub("_", Path(name).name)[-80:] or "audio"
        target = uploads_dir() / f"{uuid.uuid4().hex[:8]}-{safe}"
        with target.open("wb") as handle:
            async for chunk in request.stream():
                handle.write(chunk)
        return {"path": str(target), "name": Path(name).name}

    # —— 实时事件 ——

    @app.websocket("/api/events")
    async def events(socket: WebSocket) -> None:
        if not _local_host(socket.headers.get("host", "")) or not _same(socket.query_params.get("token", ""), token):
            await socket.close(code=4401)
            return
        await socket.accept()
        queue = services.bus.subscribe()
        try:
            session = services.monitor.current
            hello = {"type": "hello", "session": session.snapshot() if session else None}
            await socket.send_text(json.dumps(hello, ensure_ascii=False))
            await _pump(socket, queue)
        finally:
            services.bus.unsubscribe(queue)

    if web_dir is not None and (web_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    else:

        @app.get("/", response_class=HTMLResponse)
        def missing_frontend() -> str:
            command = "cd frontend &amp;&amp; npm install &amp;&amp; npm run build"
            return (
                "<meta charset='utf-8'><h2>界面还没有构建</h2>"
                f"<p>在项目根目录运行 <code>{command}</code>，然后重新启动。</p>"
            )

    return app


async def _pump(socket: WebSocket, queue: asyncio.Queue) -> None:
    """一边转发事件，一边等待对方断开；任何一边结束就一起退出。"""
    async with anyio.create_task_group() as group:

        async def forward() -> None:
            try:
                while True:
                    event = await queue.get()
                    await socket.send_text(json.dumps(event, ensure_ascii=False))
            except (WebSocketDisconnect, RuntimeError):  # 对方已断开
                pass
            group.cancel_scope.cancel()

        async def watch() -> None:
            try:
                while True:
                    await socket.receive_text()
            except WebSocketDisconnect:
                pass
            group.cancel_scope.cancel()

        group.start_soon(forward)
        group.start_soon(watch)


def _same(value: str, token: str) -> bool:
    # 按字节比较：compare_digest 遇到非 ASCII 的字符串会抛异常，而请求参数可能是任意文字。
    return secrets.compare_digest(value.encode(), token.encode())


def _authorized(request: Request, token: str) -> bool:
    if _same(request.headers.get("x-token", ""), token):
        return True
    if request.method in ("GET", "HEAD") and AUDIO_PATH.fullmatch(request.url.path):
        return _same(request.query_params.get("token", ""), token)
    return False


def _remove_recording(session_id: str) -> None:
    path = recording_path(session_id)
    for _ in range(20):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:  # Windows：播放器刚断开时文件可能还没释放，稍等再试
            time.sleep(0.1)
    log.warning("could not delete recording %s; the session record is already gone", path)


def _recording_info(session_id: str) -> dict[str, Any] | None:
    path = recording_path(session_id)
    if not path.is_file():
        return None
    return {"path": str(path), "bytes": path.stat().st_size, "seconds": round(recording_seconds(path), 1)}


def _local_host(host: str) -> bool:
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in LOCAL_HOSTS


def _handler(status: int):
    async def handle(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status)

    return handle


def _framework_or_404(services: Services, framework_id: str) -> Framework:
    framework = services.storage.get_framework(framework_id)
    if framework is None:
        raise HTTPException(404, "找不到这个话术框架")
    return framework


def _session_or_404(services: Services, session_id: str) -> dict[str, Any]:
    detail = services.storage.get_session(session_id)
    if detail is None:
        raise HTTPException(404, "找不到这一场记录")
    return detail


_MESSAGES = {
    "missing": "必须填写",
    "string_too_short": "不能为空",
    "too_short": "至少需要一项",
    "greater_than_equal": "不能小于 {ge}",
    "less_than_equal": "不能大于 {le}",
    "string_too_long": "太长了，最多 {max_length} 个字",
    "literal_error": "取值不在可选范围内",
}


_FIELDS = {
    "stages": "环节",
    "points": "要点",
    "forbidden": "违规表达",
    "name": "名称",
    "keywords": "关键词",
    "description": "说明",
    "min_mentions": "至少提到次数",
    "severity": "严重程度",
    "suggestion": "整改建议",
    "fillers": "口头禅",
    "pace": "节奏标准",
    "min_rate": "语速下限",
    "max_rate": "语速上限",
    "max_pause_seconds": "冷场秒数",
    "min_speech_ratio": "说话时间占比下限",
    "max_fillers_per_minute": "每分钟口头禅上限",
    "llm_instructions": "给大模型的额外要求",
    "window_seconds": "分析窗口",
    "interval_seconds": "分析间隔",
    "threads": "识别线程数",
    "review_every": "审核频率",
    "timeout_seconds": "超时时间",
}


def _location(parts: tuple[Any, ...]) -> str:
    """("stages", 0, "points", 1, "name") → “第 1 个环节 › 第 2 个要点 › 名称”。"""
    names: list[str] = []
    index = 0
    items = [item for item in parts if item != "body"]
    while index < len(items):
        item = items[index]
        label = _FIELDS.get(str(item), str(item))
        if index + 1 < len(items) and isinstance(items[index + 1], int):
            names.append(f"第 {items[index + 1] + 1} 个{label}")
            index += 2
        else:
            if not isinstance(item, int) and item not in ("asr", "llm", "monitor", "notify"):
                names.append(label)
            index += 1
    return " › ".join(names)


def _validation_message(errors: list[dict[str, Any]]) -> str:
    """把校验错误整理成一句中文，例如“第 1 个环节 › 名称：不能为空”。"""
    parts = []
    for error in errors[:3]:
        location = _location(tuple(error.get("loc", ())))
        template = _MESSAGES.get(error.get("type", ""))
        message = template.format(**error.get("ctx", {})) if template else str(error.get("msg", ""))
        message = message.removeprefix("Value error, ")
        parts.append(f"{location}：{message}" if location else message)
    return "；".join(parts)
