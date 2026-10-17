"""数据目录与应用设置。

数据默认放在系统的用户数据目录（Windows 为 %LOCALAPPDATA%\\voicedetect，macOS 为
~/Library/Application Support/voicedetect），可用环境变量 VOICEDETECT_HOME 改到别处；
识别模型默认在数据目录的 models 子目录，可用 VOICEDETECT_MODELS 单独指定。
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Literal

import platformdirs
from pydantic import BaseModel, Field, model_validator

APP_NAME = "voicedetect"
SECRET_MASK = "********"  # 接口返回设置时用它代替已保存的密钥


def data_dir() -> Path:
    override = os.environ.get("VOICEDETECT_HOME")
    path = Path(override) if override else Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    """识别模型目录；VOICEDETECT_MODELS 可单独指定（例如多个数据目录共用一份模型）。"""
    override = os.environ.get("VOICEDETECT_MODELS")
    return Path(override) if override else data_dir() / "models"


def uploads_dir() -> Path:
    path = data_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def recording_path(session_id: str) -> Path:
    """一场盯播的录音文件位置（不论是否存在）。场次 ID 来自网址，先校验，防止拼出目录外的路径。"""
    if not _SESSION_ID.fullmatch(session_id):
        raise ValueError("场次 ID 无效")
    return data_dir() / "recordings" / f"{session_id}.wav"


def database_path() -> Path:
    return data_dir() / "voicedetect.db"


class AsrSettings(BaseModel):
    engine: Literal["sensevoice", "xfyun"] = "sensevoice"
    # 自动检测在很短的句子上可能误判成日语或韩语，普通话直播固定为 zh 更稳。
    language: Literal["zh", "yue", "en", "auto"] = "zh"
    threads: int = Field(default=2, ge=1, le=16, description="本地识别使用的 CPU 线程数")
    partial_results: bool = Field(default=True, description="边说边显示正在识别的文字")
    model_source: Literal["auto", "hf-mirror", "huggingface"] = "auto"
    xfyun_app_id: str = ""
    xfyun_api_key: str = ""


class LlmSettings(BaseModel):
    enabled: bool = False
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    model: str = "qwen-plus"
    review_every: int = Field(default=1, ge=1, le=10, description="每几个分析窗口审核一次")
    timeout_seconds: float = Field(default=60, ge=5, le=300)


class MonitorSettings(BaseModel):
    window_seconds: float = Field(default=300, ge=30, le=3600, description="每次分析过去多少秒")
    interval_seconds: float = Field(default=60, ge=10, le=1800, description="每隔多少秒分析一次")
    # 只对实时来源（系统声音、麦克风、直播流）生效；文件本来就在磁盘上，不重复保存。
    save_audio: bool = Field(default=False, description="实时盯播时把声音存到本机")

    @model_validator(mode="after")
    def _check(self) -> MonitorSettings:
        if self.interval_seconds > self.window_seconds:
            raise ValueError("分析间隔不能大于分析窗口")
        return self


class NotifySettings(BaseModel):
    sound: bool = True
    min_severity: Literal["high", "medium", "low"] = "medium"


class AppSettings(BaseModel):
    asr: AsrSettings = Field(default_factory=AsrSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)

    def masked(self) -> dict[str, Any]:
        data = self.model_dump()
        for section, key in _SECRETS:
            if data[section][key]:
                data[section][key] = SECRET_MASK
        return data

    def merged(self, update: AppSettings) -> AppSettings:
        """接收界面提交的设置；密钥仍是遮罩时保留原值。"""
        data = update.model_dump()
        for section, key in _SECRETS:
            if data[section][key] == SECRET_MASK:
                data[section][key] = getattr(getattr(self, section), key)
        return AppSettings.model_validate(data)


_SECRETS = (("asr", "xfyun_api_key"), ("llm", "api_key"))
