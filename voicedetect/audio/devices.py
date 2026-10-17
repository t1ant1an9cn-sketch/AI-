"""麦克风与系统声音采集（soundcard：Windows WASAPI、macOS Core Audio、Linux PulseAudio）。

- 麦克风：主播在自己的电脑上使用时，直接采集主播的声音。
- 系统声音（回环）：采集播放设备正在输出的声音，适合运营在电脑上打开直播间旁听。
  Windows 和 Linux 直接支持；macOS 需要安装 BlackHole 这类虚拟声卡，它会作为一个麦克风出现在列表里。

设备直接以 16 kHz 单声道打开，由系统音频引擎完成重采样和声道混合。
"""

from __future__ import annotations

import sys
from typing import Any
import warnings

import numpy as np

from ..asr.base import SAMPLE_RATE

BLOCK_FRAMES = SAMPLE_RATE // 10  # 每次读 0.1 秒


def loopback_supported() -> bool:
    return sys.platform != "darwin"


def list_devices() -> list[dict[str, Any]]:
    """可选的输入设备。kind 为 microphone（麦克风）或 loopback（系统声音）。"""
    import soundcard as sc

    default_mic = _safe(sc.default_microphone)
    default_speaker = _safe(sc.default_speaker)
    devices = []
    for mic in sc.all_microphones(include_loopback=loopback_supported()):
        if mic.isloopback:
            is_default = default_speaker is not None and mic.name == default_speaker.name
            devices.append({"id": mic.id, "name": mic.name, "kind": "loopback", "default": is_default})
        else:
            is_default = default_mic is not None and mic.id == default_mic.id
            devices.append({"id": mic.id, "name": mic.name, "kind": "microphone", "default": is_default})
    return sorted(devices, key=lambda d: (d["kind"] != "loopback", not d["default"], d["name"]))


def _safe(getter):
    try:
        return getter()
    except Exception:
        return None


class DeviceSource:
    """从一个音频设备持续采集。"""

    live = True

    def __init__(self, device_id: str | None, *, loopback: bool, name: str = "") -> None:
        self.device_id = device_id
        self.loopback = loopback
        kind = "系统声音" if loopback else "麦克风"
        self.label = f"{kind}：{name}" if name else f"{kind}（默认设备）"
        self.discontinuities = 0  # 声卡报告的数据不连续次数
        self._context = None
        self._recorder = None

    def open(self) -> None:
        import soundcard as sc

        if self.device_id:
            mic = sc.get_microphone(self.device_id, include_loopback=self.loopback)
        elif self.loopback:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(speaker.id, include_loopback=True)
        else:
            mic = sc.default_microphone()
        if not self.label.endswith(mic.name):
            self.label = f"{'系统声音' if self.loopback else '麦克风'}：{mic.name}"
        self._context = mic.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=BLOCK_FRAMES)
        self._recorder = self._context.__enter__()

    def read(self) -> np.ndarray | None:
        if self._recorder is None:
            raise RuntimeError("设备尚未打开")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            data = self._recorder.record(numframes=BLOCK_FRAMES)
        self.discontinuities += sum("discontinuity" in str(item.message) for item in caught)
        return np.ascontiguousarray(data[:, 0], dtype=np.float32)

    def close(self) -> None:
        context, self._context, self._recorder = self._context, None, None
        if context is not None:
            context.__exit__(None, None, None)
