"""音频来源的统一接口：设备采集和文件回放都输出 16 kHz 单声道 float32 音频块。"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class AudioSource(Protocol):
    label: str  # 展示给用户的来源名称
    live: bool  # 实时来源：识别跟不上时丢弃最旧的音频，而不是让采集等待

    def open(self) -> None: ...

    def read(self) -> np.ndarray | None:
        """读取下一块音频；返回 None 表示来源已结束。"""
        ...

    def close(self) -> None: ...


def level_db(samples: np.ndarray) -> float:
    """音频块的均方根电平（dBFS），完全无声时返回 -120。"""
    if samples.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return max(-120.0, 20 * np.log10(rms)) if rms > 0 else -120.0
