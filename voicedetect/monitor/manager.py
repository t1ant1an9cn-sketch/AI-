"""会话管理：同一时间只运行一场盯播；按设置创建音频来源、识别引擎和大模型审核。"""

from __future__ import annotations

from collections.abc import Callable
from importlib.resources import files
import threading
from typing import Any, Literal, Protocol
import uuid

from pydantic import BaseModel, Field

from ..asr.base import Recognizer, RecognizerError
from ..audio.base import AudioSource
from ..config import AppSettings, models_dir
from ..frameworks import Framework
from .bus import EventBus
from .session import MonitorSession, Reviewer, SessionOptions, SessionStore

DEMO_LABEL = "演示音频：保温杯讲解"


def demo_audio_path() -> str:
    return str(files("voicedetect") / "assets" / "demo" / "thermos-demo.ogg")


class SourceSpec(BaseModel):
    kind: Literal["device", "file", "url", "demo"]
    device_id: str | None = None
    device_name: str = ""
    loopback: bool = True  # 设备是否为系统声音（回环）
    path: str = ""
    url: str = ""
    realtime: bool = True  # 文件是否按实际速度播放


class StartRequest(BaseModel):
    framework_id: str
    title: str = Field(default="", max_length=100)
    source: SourceSpec
    window_seconds: float | None = Field(default=None, ge=30, le=3600)
    interval_seconds: float | None = Field(default=None, ge=10, le=1800)


class SessionBusy(RuntimeError):
    pass


def build_source(spec: SourceSpec) -> AudioSource:
    if spec.kind == "device":
        from ..audio.devices import DeviceSource

        return DeviceSource(spec.device_id, loopback=spec.loopback, name=spec.device_name)
    from ..audio.files import MediaSource

    if spec.kind == "file":
        if not spec.path:
            raise ValueError("请选择要分析的音频文件")
        return MediaSource(spec.path, realtime=spec.realtime)
    if spec.kind == "url":
        if not spec.url:
            raise ValueError("请填写直播流地址")
        return MediaSource(spec.url)
    return MediaSource(demo_audio_path(), realtime=spec.realtime, label=DEMO_LABEL)


def engine_label(settings: AppSettings) -> str:
    return "讯飞实时转写" if settings.asr.engine == "xfyun" else "SenseVoice（本地）"


def recognizer_factory(settings: AppSettings, *, partials: bool) -> Callable[[], Recognizer]:
    """检查引擎是否可用并返回创建函数；缺模型或缺账号时在开播前就报错。"""
    asr = settings.asr
    if asr.engine == "xfyun":
        if not (asr.xfyun_app_id and asr.xfyun_api_key):
            raise RecognizerError("使用讯飞实时转写需要先在“设置 → 语音识别”里填写 APPID 和 APIKey。")
        from ..asr.xfyun import XfyunRecognizer

        return lambda: XfyunRecognizer(app_id=asr.xfyun_app_id, api_key=asr.xfyun_api_key)
    from ..asr.models import SENSEVOICE, ModelStore
    from ..asr.sensevoice import load_engine

    if not ModelStore(models_dir(), SENSEVOICE).installed():
        raise RecognizerError("还没有下载本地识别模型，请到“设置 → 语音识别”里下载（约 230 MB）。")
    return lambda: load_engine(models_dir(), threads=asr.threads, language=asr.language).create(partials=partials)


def build_reviewer(settings: AppSettings) -> Reviewer | None:
    llm = settings.llm
    if not llm.enabled:
        return None
    from ..analysis.llm import LlmReviewer

    return LlmReviewer(base_url=llm.base_url, api_key=llm.api_key, model=llm.model, timeout=llm.timeout_seconds)


class Store(SessionStore, Protocol):
    """会话需要的存储接口，再加上开场登记和读取框架。"""

    def create_session(self, **fields: Any) -> None: ...
    def get_framework(self, framework_id: str) -> Framework | None: ...


class SessionManager:
    def __init__(
        self,
        *,
        storage: Store,
        bus: EventBus,
        settings: Callable[[], AppSettings],
        make_source: Callable[[SourceSpec], AudioSource] = build_source,
        make_recognizer: Callable[..., Callable[[], Recognizer]] = recognizer_factory,
        make_reviewer: Callable[[AppSettings], Reviewer | None] = build_reviewer,
    ) -> None:
        self._storage = storage
        self._bus = bus
        self._settings = settings
        self._make_source = make_source
        self._make_recognizer = make_recognizer
        self._make_reviewer = make_reviewer
        self._lock = threading.Lock()
        self.current: MonitorSession | None = None

    def start(
        self, request: StartRequest, *, listener: Callable[[dict[str, Any]], None] | None = None
    ) -> MonitorSession:
        framework = self._storage.get_framework(request.framework_id)
        if framework is None:
            raise ValueError("找不到这个话术框架，可能已被删除")
        settings = self._settings()
        window = request.window_seconds or settings.monitor.window_seconds
        interval = request.interval_seconds or settings.monitor.interval_seconds
        if interval > window:
            raise ValueError("分析间隔不能大于分析窗口")
        source = self._make_source(request.source)
        title = request.title.strip() or source.label
        return self._launch(
            session_id=uuid.uuid4().hex[:12],
            title=title,
            framework=framework,
            source=source,
            settings=settings,
            options=SessionOptions(
                window_seconds=window, interval_seconds=interval, review_every=settings.llm.review_every
            ),
            listener=listener,
        )

    def start_file(
        self,
        *,
        session_id: str,
        title: str,
        framework: Framework,
        location: str,
        realtime: bool,
        options: SessionOptions,
        listener: Callable[[dict[str, Any]], None] | None = None,
    ) -> MonitorSession:
        """命令行分析文件或直播流时使用，框架可以不在数据库里。"""
        from ..audio.files import MediaSource, is_stream_url

        source = MediaSource(location, realtime=realtime)
        settings = self._settings()
        options.review_every = settings.llm.review_every
        return self._launch(
            session_id=session_id,
            title=title,
            framework=framework,
            source=source,
            settings=settings,
            options=options,
            listener=listener,
            partials=is_stream_url(location) or realtime,
        )

    def _launch(
        self,
        *,
        session_id: str,
        title: str,
        framework: Framework,
        source: AudioSource,
        settings: AppSettings,
        options: SessionOptions,
        listener: Callable[[dict[str, Any]], None] | None,
        partials: bool | None = None,
    ) -> MonitorSession:
        with self._lock:
            if self.current is not None and not self.current.finished:
                raise SessionBusy("已经有一场盯播在进行，请先停止")
            if partials is None:
                partials = source.live or getattr(source, "realtime", False)
            factory = self._make_recognizer(settings, partials=partials and settings.asr.partial_results)
            reviewer = self._make_reviewer(settings)

            def emit(event: dict[str, Any]) -> None:
                self._bus.publish(event)
                if listener is not None:
                    listener(event)

            self._storage.create_session(
                session_id=session_id,
                title=title,
                framework=framework,
                source=source.label,
                engine=engine_label(settings),
                window_seconds=options.window_seconds,
                interval_seconds=options.interval_seconds,
            )
            session = MonitorSession(
                session_id=session_id,
                title=title,
                framework=framework,
                source=source,
                recognizer_factory=factory,
                emit=emit,
                options=options,
                store=self._storage,
                reviewer=reviewer,
                engine=engine_label(settings),
            )
            self.current = session
            session.start()
            return session

    def stop(self) -> MonitorSession | None:
        session = self.current
        if session is not None:
            session.stop()
        return session

    def shutdown(self, timeout: float = 10.0) -> None:
        session = self.stop()
        if session is not None:
            session.wait(timeout)
