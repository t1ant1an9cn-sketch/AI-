"""把一场实时盯播的声音存成 WAV（16 kHz 单声道 16 位，约每小时 115 MB）。

录音在采集线程里写，内容是设备送来的全部声音，包括识别跟不上时被丢弃的部分；
时间轴与盯播时间一致，复盘时转写、提醒上的时间可以直接对到录音里。
每次写入后都会更新文件头并写到磁盘，程序异常退出时已录下的部分仍能播放。
"""

from __future__ import annotations

from pathlib import Path
import wave

import numpy as np

from ..asr.base import SAMPLE_RATE

HEADER_BYTES = 44  # wave 模块写出的 PCM 文件头长度
BYTES_PER_SECOND = SAMPLE_RATE * 2


class WavRecorder:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("wb")
        self._wave = wave.open(self._file, "wb")
        self._wave.setnchannels(1)
        self._wave.setsampwidth(2)
        self._wave.setframerate(SAMPLE_RATE)
        self.frames = 0

    def write(self, samples: np.ndarray) -> None:
        pcm = np.rint(np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")  # 四舍五入，直接转换会向零截断
        self._wave.writeframes(pcm.tobytes())  # 写完会顺带改好文件头里的长度
        self._file.flush()
        self.frames += len(pcm)

    def close(self) -> None:
        try:
            self._wave.close()
        finally:
            self._file.close()


def recording_seconds(path: Path) -> float:
    """按文件大小估算录音时长；录音还在进行时也能用。"""
    return max(0, path.stat().st_size - HEADER_BYTES) / BYTES_PER_SECOND
