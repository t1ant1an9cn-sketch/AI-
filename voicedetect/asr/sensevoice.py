"""本地识别：Silero VAD 切句 + SenseVoice 识别（sherpa-onnx，只用 CPU）。

VAD 判断哪里有人说话；一句话说完（停顿约 0.4 秒）就交给 SenseVoice 识别成定稿文字，
自带标点，数字写成阿拉伯数字。说话过程中隔一小段把“这句话到目前为止”的音频识别一次，
作为 partial 显示，实现边说边出字；间隔随识别耗时自动放宽，句子越长刷新越慢，避免占满 CPU。
"""

from __future__ import annotations

from pathlib import Path
import threading
import time

import numpy as np

from ..analysis.text import comparable
from .base import SAMPLE_RATE, AsrEvent, RecognizerError
from .models import SENSEVOICE, ModelStore

EMOTIONS = {
    "HAPPY": "开心",
    "SAD": "低落",
    "ANGRY": "生气",
    "NEUTRAL": "平静",
    "FEARFUL": "紧张",
    "DISGUSTED": "厌烦",
    "SURPRISED": "惊讶",
}


class SenseVoiceEngine:
    """加载好的识别模型。加载约 1 秒、占用约 300 MB 内存，可在多场会话之间复用。"""

    def __init__(self, model_dir: Path, *, threads: int = 2, language: str = "zh") -> None:
        import sherpa_onnx

        self._sherpa = sherpa_onnx
        self.model_dir = Path(model_dir)
        self.vad_model = str(self.model_dir / "silero_vad_v5.onnx")
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(self.model_dir / "model.int8.onnx"),
            tokens=str(self.model_dir / "tokens.txt"),
            num_threads=threads,
            use_itn=True,  # 输出标点，数字写成阿拉伯数字
            language="" if language == "auto" else language,
        )
        self._lock = threading.Lock()  # 同一个模型同时只解码一段

    def transcribe(self, samples: np.ndarray) -> tuple[str, str | None]:
        with self._lock:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            self.recognizer.decode_stream(stream)
            result = stream.result
        emotion = EMOTIONS.get(result.emotion.strip("<|>"))
        return result.text.strip(), emotion

    def create(self, *, partials: bool = True) -> SenseVoiceRecognizer:
        return SenseVoiceRecognizer(self, partials=partials)


_engines: dict[tuple[str, int, str], SenseVoiceEngine] = {}
_engines_lock = threading.Lock()


def load_engine(models_root: Path, *, threads: int = 2, language: str = "zh") -> SenseVoiceEngine:
    """按需加载并缓存模型；模型未下载时给出可操作的提示。"""
    store = ModelStore(models_root, SENSEVOICE)
    if not store.installed():
        raise RecognizerError("还没有下载本地识别模型，请到“设置 → 语音识别”里下载（约 230 MB）。")
    key = (str(store.path), threads, language)
    with _engines_lock:
        if key not in _engines:
            _engines.clear()  # 只保留一份，换设置时释放旧模型
            _engines[key] = SenseVoiceEngine(store.path, threads=threads, language=language)
        return _engines[key]


class SenseVoiceRecognizer:
    name = "SenseVoice（本地）"

    def __init__(
        self,
        engine: SenseVoiceEngine,
        *,
        partials: bool = True,
        min_silence: float = 0.4,
        max_speech: float = 15.0,
    ) -> None:
        sherpa = engine._sherpa
        config = sherpa.VadModelConfig()
        config.silero_vad.model = engine.vad_model
        config.silero_vad.threshold = 0.5
        config.silero_vad.min_silence_duration = min_silence
        config.silero_vad.min_speech_duration = 0.25
        config.silero_vad.max_speech_duration = max_speech  # 一口气说太久时强制断句，保证定稿及时
        config.sample_rate = SAMPLE_RATE
        config.num_threads = 1
        self._engine = engine
        self._vad = sherpa.VoiceActivityDetector(config, buffer_size_in_seconds=60)
        self._window = config.silero_vad.window_size
        self._partials = partials
        self._pending = np.zeros(0, dtype=np.float32)
        self._fed = 0  # 已送入 VAD 的样本数
        self._speech_start: float | None = None
        self._next_partial = 0  # 下一次刷新 partial 的样本位置
        self._partial_gap = int(0.5 * SAMPLE_RATE)

    @property
    def speech_since(self) -> float | None:
        return self._speech_start

    def accept(self, samples: np.ndarray) -> list[AsrEvent]:
        buffer = np.concatenate([self._pending, np.asarray(samples, dtype=np.float32)])
        usable = len(buffer) - len(buffer) % self._window
        for offset in range(0, usable, self._window):
            self._vad.accept_waveform(buffer[offset : offset + self._window])
        self._fed += usable
        self._pending = buffer[usable:]
        events = self._drain()
        self._track_speech(bool(events))
        if self._partials and self._speech_start is not None and self._fed >= self._next_partial:
            events.extend(self._partial())
        return events

    def flush(self) -> list[AsrEvent]:
        self._vad.flush()
        events = self._drain()
        self._speech_start = None
        return events

    def close(self) -> None:
        self._vad.reset()

    def _drain(self) -> list[AsrEvent]:
        events = []
        while not self._vad.empty():
            segment = self._vad.front
            samples = np.asarray(segment.samples, dtype=np.float32)
            start = segment.start / SAMPLE_RATE
            self._vad.pop()
            text, emotion = self._engine.transcribe(samples)
            if not comparable(text):  # 有人说话但没认出字（语气词、噪声），仍报告这段时间用于计算停顿
                text, emotion = "", None
            events.append(AsrEvent("final", text, start, start + len(samples) / SAMPLE_RATE, emotion))
        return events

    def _track_speech(self, finished: bool) -> None:
        if not self._vad.is_speech_detected():
            self._speech_start = None
            return
        if self._speech_start is None or finished:  # 新的一句开始了
            current = self._vad.current_segment
            self._speech_start = current.start / SAMPLE_RATE if current is not None else self._fed / SAMPLE_RATE
            self._next_partial = self._fed + self._partial_gap

    def _partial(self) -> list[AsrEvent]:
        current = self._vad.current_segment
        if current is None or not len(current.samples):
            return []
        started = time.perf_counter()
        text, _ = self._engine.transcribe(np.asarray(current.samples, dtype=np.float32))
        spent = time.perf_counter() - started
        # 解码越慢，刷新越稀：间隔约为解码耗时的 4 倍，限制在 0.5–2 秒之间。
        self._partial_gap = int(min(2.0, max(0.5, spent * 4)) * SAMPLE_RATE)
        self._next_partial = self._fed + self._partial_gap
        if not comparable(text):
            return []
        start = current.start / SAMPLE_RATE
        return [AsrEvent("partial", text, start, start + len(current.samples) / SAMPLE_RATE)]
