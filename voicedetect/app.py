"""把各部分组装起来：存储、设置、事件总线、会话管理、模型下载和直播平台登录窗口。桌面窗口、命令行和接口共用一份。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import threading
from typing import Any

from . import instance
from .asr.models import SENSEVOICE, DownloadCancelled, ModelStore
from .audio.logins import LoginWindow
from .config import AppSettings, database_path, models_dir
from .frameworks import builtin_templates
from .monitor.bus import EventBus
from .monitor.manager import SessionManager
from .storage import Storage

log = logging.getLogger(__name__)


class ModelDownloads:
    """在后台线程下载模型，进度通过事件总线推送给界面。"""

    def __init__(self, bus: EventBus, settings) -> None:
        self._bus = bus
        self._settings = settings
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.state: dict[str, Any] = {"downloading": False, "done": 0, "total": SENSEVOICE.size, "error": ""}

    @property
    def store(self) -> ModelStore:
        return ModelStore(models_dir(), SENSEVOICE)

    def status(self) -> dict[str, Any]:
        store = self.store
        return {
            "id": SENSEVOICE.id,
            "name": SENSEVOICE.name,
            "size": SENSEVOICE.size,
            "installed": store.installed(),
            "path": str(store.path),
            **self.state,
        }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._cancel.clear()
                self.state.update(downloading=True, done=0, error="")
                self._thread = threading.Thread(target=self._run, name="model-download", daemon=True)
                self._thread.start()
        return self.status()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        last = [0]

        def progress(done: int, total: int) -> None:
            self.state.update(done=done, total=total)
            if done - last[0] >= 2_000_000 or done == total:  # 每 2 MB 推送一次
                last[0] = done
                self._publish()

        try:
            self.store.download(source=self._settings().asr.model_source, progress=progress, cancel=self._cancel)
        except DownloadCancelled:
            self.state["error"] = "已取消"
        except Exception as exc:
            log.exception("model download failed")
            self.state["error"] = str(exc)
        finally:
            self.state["downloading"] = False
            self._publish()

    def _publish(self) -> None:
        self._bus.publish({"type": "model", **self.status()})


def load_settings(storage: Storage) -> AppSettings:
    return AppSettings.model_validate(storage.get_setting("app") or {})


@dataclass
class Services:
    storage: Storage
    bus: EventBus
    monitor: SessionManager
    downloads: ModelDownloads
    logins: LoginWindow
    primary: bool = True  # 是不是这个数据库唯一在用的程序；不是时不去改别人正在进行的场次

    @classmethod
    def create(cls, db_path: Path | None = None) -> Services:
        path = db_path or database_path()
        storage = Storage(path)
        primary = instance.acquire(path.parent)
        if primary:
            storage.mark_interrupted()  # 只有没有别的程序在用这个数据库时，进行中的场次才是上次异常退出留下的
        storage.seed_templates(builtin_templates())
        bus = EventBus()

        def settings() -> AppSettings:
            return load_settings(storage)

        return cls(
            storage=storage,
            bus=bus,
            monitor=SessionManager(storage=storage, bus=bus, settings=settings),
            downloads=ModelDownloads(bus, settings),
            logins=LoginWindow(on_change=lambda status: bus.publish({"type": "logins", **status})),
            primary=primary,
        )

    def settings(self) -> AppSettings:
        return load_settings(self.storage)

    def save_settings(self, settings: AppSettings) -> AppSettings:
        self.storage.set_setting("app", settings.model_dump())
        return settings

    def close(self) -> None:
        self.monitor.shutdown()
        self.downloads.cancel()
        self.logins.close()
        self.storage.close()
