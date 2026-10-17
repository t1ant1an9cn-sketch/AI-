"""测试替身：按脚本“识别”的引擎、按块输出的音频来源，以及记录写入的存储。"""

from __future__ import annotations

import threading
import time

import numpy as np

from voicedetect.asr.base import SAMPLE_RATE, AsrEvent


class ScriptedRecognizer:
    """音频时间走到某句话的结束时刻时，把这句话作为定稿输出；说到一半时输出 partial。"""

    name = "脚本"

    def __init__(self, lines, *, delay: float = 0.0):
        self.lines = sorted(lines)
        self.delay = delay  # 每块额外耗时，模拟识别慢
        self.samples = 0
        self.done = 0
        self.closed = False

    @property
    def time(self):
        return self.samples / SAMPLE_RATE

    def accept(self, samples):
        if self.delay:
            time.sleep(self.delay)
        self.samples += len(samples)
        events = []
        while self.done < len(self.lines) and self.lines[self.done][1] <= self.time:
            start, end, text = self.lines[self.done]
            events.append(AsrEvent("final", text, start, end))
            self.done += 1
        current = self._current()
        if current:
            start, end, text = current
            shown = max(1, int(len(text) * (self.time - start) / (end - start)))
            events.append(AsrEvent("partial", text[:shown], start, self.time))
        return events

    def _current(self):
        if self.done < len(self.lines) and self.lines[self.done][0] <= self.time:
            return self.lines[self.done]
        return None

    @property
    def speech_since(self):
        current = self._current()
        return current[0] if current else None

    def flush(self):
        events = []
        while self.done < len(self.lines) and self.lines[self.done][0] < self.time:
            start, _, text = self.lines[self.done]
            events.append(AsrEvent("final", text, start, self.time))
            self.done += 1
        return events

    def close(self):
        self.closed = True


class BlockSource:
    """输出 seconds 秒音频（默认是能触发“有信号”的低噪声），可模拟实时节奏或读取失败。"""

    def __init__(self, seconds, *, live=False, pace=0.0, level=0.01, fail_at=None, block=0.1):
        self.label = "测试音频"
        self.live = live
        self.total = int(seconds / block)
        self.block = int(block * SAMPLE_RATE)
        self.pace = pace  # 每块之间等待的秒数
        self.level = level
        self.fail_at = fail_at
        self.sent = 0
        self.opened = self.closed = False
        self.release = threading.Event()
        self.release.set()

    def open(self):
        self.opened = True

    def read(self):
        self.release.wait()
        if self.fail_at is not None and self.sent >= self.fail_at:
            raise OSError("设备被拔出")
        if self.sent >= self.total:
            return None
        if self.pace:
            time.sleep(self.pace)
        self.sent += 1
        return np.full(self.block, self.level, dtype=np.float32)

    def close(self):
        self.closed = True


class MemoryStore:
    def __init__(self):
        self.segments, self.reports, self.alerts, self.llm = [], [], [], {}
        self.finished = None

    def add_segment(self, session_id, segment):
        self.segments.append(segment)

    def add_report(self, report):
        self.reports.append(report)
        return len(self.reports)

    def set_report_llm(self, report_id, review):
        self.llm[report_id] = review

    def add_alert(self, alert):
        self.alerts.append(alert)
        return len(self.alerts)

    def finish_session(self, session_id, **fields):
        self.finished = fields


class Events(list):
    """收集会话发出的事件。"""

    def __call__(self, event):
        self.append(event)

    def of(self, kind):
        return [event for event in self if event["type"] == kind]
