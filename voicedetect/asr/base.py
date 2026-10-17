"""语音识别引擎的统一接口。

会话只依赖这里的 Recognizer：送入 16 kHz 单声道 float32 音频，取回两类事件——
partial（正在说的那句话，随后会被更新）和 final（定稿的一句话）。
有人说了话但没认出字时，final 的文字为空，只用来计算说话时间和停顿。
事件时间按已送入的音频样本计算（秒），与墙上时钟无关，所以回放文件和实时采集用同一套逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np

SAMPLE_RATE = 16000


@dataclass(frozen=True, slots=True)
class AsrEvent:
    kind: Literal["partial", "final"]
    text: str
    start: float
    end: float
    emotion: str | None = None


class RecognizerError(RuntimeError):
    """识别引擎无法继续工作（模型缺失、鉴权失败、网络中断等），消息直接展示给用户。"""


class Recognizer(Protocol):
    name: str

    def accept(self, samples: np.ndarray) -> list[AsrEvent]:
        """送入一段音频，返回这段时间里产生的事件。"""
        ...

    def flush(self) -> list[AsrEvent]:
        """输入结束：把尚未定稿的语音定稿并返回。"""
        ...

    @property
    def speech_since(self) -> float | None:
        """正在说的那句话从何时开始；None 表示此刻没人说话。"""
        ...

    def close(self) -> None: ...
