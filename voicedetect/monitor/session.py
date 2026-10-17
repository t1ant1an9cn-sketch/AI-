"""一场直播的实时盯播会话：采集 → 识别 → 即时检查 → 定时窗口分析 → 提醒。

两个后台线程：
- 采集线程只读音频，放进有界队列。实时来源（声卡、直播流）在识别跟不上时丢弃最旧的音频，
  并补上等长静音保持时间轴不变，同时提醒用户；文件来源则等待识别，不丢数据。
- 处理线程加载识别引擎、逐块识别，并按“音频时间”调度窗口分析：每 interval 秒分析过去 window 秒。
  违规表达和冷场在每句话、每块音频上即时检查，不等窗口。

大模型审核在单独的线程里进行，上一轮没完成时跳过本轮，不阻塞识别。
"""

from __future__ import annotations

from bisect import insort
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging
import queue
import threading
import time
from typing import Any, Literal, Protocol

import numpy as np

from ..analysis.report import Alert, Evidence, Finding, LlmReview, Segment, WindowReport, now_text
from ..analysis.text import comparable
from ..analysis.window import FrameworkAnalyzer, SegmentFacts
from ..asr.base import SAMPLE_RATE, AsrEvent, Recognizer
from ..audio.base import AudioSource, level_db
from ..frameworks import Framework

log = logging.getLogger(__name__)

State = Literal["starting", "running", "stopping", "stopped", "failed"]
Emit = Callable[[dict[str, Any]], None]

NO_SIGNAL_DB = -70.0  # 低于此电平视为没有音频信号（静音的播放器或拔掉的麦克风）
BLOCK_SECONDS = 0.1  # 各音频来源大致的块长，用于估算积压
_EOF = object()


@dataclass
class SessionOptions:
    window_seconds: float = 300.0  # 每次分析过去多少秒
    interval_seconds: float = 60.0  # 每隔多少秒分析一次
    max_backlog_seconds: float = 15.0  # 实时来源最多积压多少秒待识别音频
    no_signal_seconds: float = 15.0  # 连续多久没有信号就提醒
    review_every: int = 1  # 每几个窗口做一次大模型审核


class SessionStore(Protocol):
    def add_segment(self, session_id: str, segment: Segment) -> None: ...
    def add_report(self, report: WindowReport) -> int: ...
    def set_report_llm(self, report_id: int, review: LlmReview) -> None: ...
    def add_alert(self, alert: Alert) -> int: ...
    def finish_session(
        self, session_id: str, *, status: str, duration: float, summary: WindowReport | None, error: str
    ) -> None: ...


class Reviewer(Protocol):
    def review(self, framework: Framework, segments: list[Segment], report: WindowReport) -> LlmReview: ...


class MonitorSession:
    def __init__(
        self,
        *,
        session_id: str,
        title: str,
        framework: Framework,
        source: AudioSource,
        recognizer_factory: Callable[[], Recognizer],
        emit: Emit,
        options: SessionOptions | None = None,
        store: SessionStore | None = None,
        reviewer: Reviewer | None = None,
        engine: str = "",
    ) -> None:
        self.session_id = session_id
        self.title = title
        self.framework = framework
        self.source = source
        self.options = options or SessionOptions()
        self._recognizer_factory = recognizer_factory
        self._emit_event = emit
        self._store = store
        self._reviewer = reviewer
        self._analyzer = FrameworkAnalyzer(framework)

        self.state: State = "starting"
        self.error = ""
        self.started_at = ""
        self.engine = engine  # 识别引擎名称；引擎加载好后以它自报的为准
        self._samples = 0  # 已处理的样本数；用整数计时，避免浮点累加误差
        self.summary: WindowReport | None = None

        live = source.live
        # 音频是否按真实时间到达（实时来源，或按实际速度播放的文件）；快速分析文件时界面不弹窗。
        self.paced = live or bool(getattr(source, "realtime", False))
        self._queue: queue.Queue = queue.Queue(maxsize=max(10, int(self.options.max_backlog_seconds / BLOCK_SECONDS)))
        self._stop = threading.Event()
        self._ready = threading.Event()  # 识别引擎就绪后才开始采集，避免加载模型期间积压
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._live = live

        self._history: list[SegmentFacts] = []
        self._next_segment_id = 1
        self._next_due = self.options.interval_seconds
        self._last_report_end = 0.0
        self._active_keys: set[str] = set()
        self._speech_since: float | None = None
        self._last_speech_end: float | None = None
        self._pause_alerted = False
        self._silent_for = 0.0
        self._no_signal = False
        self._gap_samples = 0  # 采集线程丢弃、尚未补静音的样本数
        self._dropped_samples = 0
        self._partial = ""
        self._throttle: dict[str, float] = {}
        self._level_peak = -120.0
        self._store_failed = False

        self._segments: deque[dict[str, Any]] = deque(maxlen=400)
        self._alerts: deque[dict[str, Any]] = deque(maxlen=200)
        self._reports: deque[dict[str, Any]] = deque(maxlen=20)
        self._minute_rates: dict[int, float | None] = {}
        self._reviewer_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-review") if reviewer else None
        self._review_future: Future | None = None
        self._window_count = 0
        self._llm_seen: set[str] = set()  # 已经提醒过的大模型问题

    # —— 生命周期 ——

    def start(self) -> None:
        self.started_at = now_text()
        self._set_state("starting")
        for target, name in ((self._process, "monitor-process"), (self._capture, "monitor-capture")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        """请求停止。已采集的音频会识别完，再做收尾分析。"""
        with self._lock:
            if self.state in ("starting", "running"):
                self._set_state("stopping")
        self._stop.set()
        self._ready.set()

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in self._threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        if self._review_future is not None:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                self._review_future.result(remaining)
            except Exception:
                pass
        return not any(thread.is_alive() for thread in self._threads)

    @property
    def finished(self) -> bool:
        return self.state in ("stopped", "failed")

    @property
    def audio_time(self) -> float:
        """会话音频时间（秒）。"""
        return self._samples / SAMPLE_RATE

    # —— 采集线程 ——

    def _capture(self) -> None:
        try:
            self._ready.wait()
            if self._stop.is_set():
                return
            self.source.open()
            while not self._stop.is_set():
                block = self.source.read()
                if block is None:
                    break
                if block.size:
                    self._enqueue(block)
        except Exception as exc:  # 设备被拔出、文件损坏、网络中断等
            log.exception("audio capture failed")
            self._fail(f"音频采集失败：{exc}")
        finally:
            try:
                self.source.close()
            except Exception:
                log.exception("closing audio source failed")
            self._enqueue_eof()

    def _enqueue(self, block: np.ndarray) -> None:
        if not self._live:
            while not self._stop.is_set():
                try:
                    self._queue.put(block, timeout=0.5)
                    return
                except queue.Full:
                    continue
            return
        while True:
            try:
                self._queue.put_nowait(block)
                return
            except queue.Full:
                try:
                    dropped = self._queue.get_nowait()
                except queue.Empty:
                    continue
                with self._lock:
                    self._gap_samples += len(dropped)

    def _enqueue_eof(self) -> None:
        while True:
            try:
                self._queue.put(_EOF, timeout=0.5)
                return
            except queue.Full:
                if self.state == "failed":
                    return  # 处理线程已退出，没人再读队列

    # —— 处理线程 ——

    def _process(self) -> None:
        recognizer: Recognizer | None = None
        try:
            recognizer = self._recognizer_factory()
            self.engine = recognizer.name
            with self._lock:
                if self.state == "starting":
                    self._set_state("running")
            self._ready.set()
            while True:
                item = self._queue.get()
                if item is _EOF:
                    break
                self._step(recognizer, item)
            if self.state != "failed":
                self._handle(recognizer.flush())
        except Exception as exc:
            log.exception("recognition failed")
            self._fail(f"识别失败：{exc}")
        finally:
            self._ready.set()
            self._stop.set()
            if recognizer is not None:
                try:
                    recognizer.close()
                except Exception:
                    log.exception("closing recognizer failed")
            self._finish()

    def _step(self, recognizer: Recognizer, block: np.ndarray) -> None:
        with self._lock:
            gap, self._gap_samples = self._gap_samples, 0
        if gap:
            self._dropped_samples += gap
            self._feed(recognizer, np.zeros(gap, dtype=np.float32))
            self._system_alert(
                "dropped_audio",
                "medium",
                "识别跟不上，丢弃了部分音频",
                f"有 {gap / SAMPLE_RATE:.1f} 秒音频来不及识别被跳过，这段时间的讲解没有分析。",
                "关闭其他占用 CPU 的程序，或在设置里换用更快的识别方式。",
            )
        self._feed(recognizer, block)

    def _feed(self, recognizer: Recognizer, samples: np.ndarray) -> None:
        self._samples += len(samples)
        self._watch_signal(samples, len(samples) / SAMPLE_RATE)
        speaking_before = self._speech_since is not None
        self._handle(recognizer.accept(samples))
        self._speech_since = recognizer.speech_since
        if self._speech_since is not None:
            self._pause_alerted = False
        elif speaking_before:
            # 有人说了话但没识别出文字（例如只说了“嗯，好”），冷场也从这里重新计时。
            self._last_speech_end = max(self._last_speech_end or 0.0, self.audio_time)
        self._watch_pause()
        self._run_due_windows()
        if self._due("health", 2.0):
            self._emit("health", **self.health())

    def _handle(self, events: list[AsrEvent]) -> None:
        for event in events:
            if event.kind == "partial":
                if event.text != self._partial and self._due("partial", 0.15):
                    self._partial = event.text
                    self._emit("partial", text=event.text, start=round(event.start, 2), end=round(event.end, 2))
                continue
            text = event.text.strip()
            if text:
                self._add_segment(text, event)
            else:
                self._add_unrecognized(event)

    def _add_unrecognized(self, event: AsrEvent) -> None:
        """没认出字的说话：不进转写，但计入说话时间并结束冷场计时。"""
        segment = Segment(id=0, start=round(event.start, 2), end=round(max(event.end, event.start), 2), text="")
        insort(self._history, self._analyzer.inspect(segment), key=lambda item: item.segment.start)
        self._last_speech_end = max(self._last_speech_end or 0.0, segment.end)
        self._pause_alerted = False

    def _add_segment(self, text: str, event: AsrEvent) -> None:
        segment = Segment(
            id=self._next_segment_id,
            start=round(event.start, 2),
            end=round(max(event.end, event.start), 2),
            text=text,
            emotion=event.emotion,
        )
        self._next_segment_id += 1
        facts = self._analyzer.inspect(segment)
        insort(self._history, facts, key=lambda item: item.segment.start)
        self._partial = ""
        self._last_speech_end = max(self._last_speech_end or 0.0, segment.end)
        self._pause_alerted = False
        payload = {
            "segment": segment.model_dump(),
            "highlights": [item.model_dump() for item in self._analyzer.highlights(facts)],
        }
        with self._lock:
            self._segments.append(payload)
        self._save(lambda store: store.add_segment(self.session_id, segment))
        self._emit("segment", **payload)
        for finding in self._analyzer.forbidden_findings([facts], "刚才"):
            self._alert(finding, time=segment.start, source="realtime")

    # —— 即时检查 ——

    def _watch_signal(self, samples: np.ndarray, duration: float) -> None:
        level = level_db(samples)
        self._level_peak = max(self._level_peak, level)
        if self._due("level", 0.2):
            self._emit("level", db=round(self._level_peak, 1), time=round(self.audio_time, 2))
            self._level_peak = -120.0
        if not self._live:
            return
        if level >= NO_SIGNAL_DB:
            self._silent_for = 0.0
            self._no_signal = False
            return
        self._silent_for += duration
        if not self._no_signal and self._silent_for >= self.options.no_signal_seconds:
            self._no_signal = True
            self._system_alert(
                "no_signal",
                "high",
                "没有收到声音",
                f"已经 {self._silent_for:.0f} 秒没有任何音频信号。",
                "检查直播间是否在播放、播放器是否静音，或者是否选错了音频设备。",
            )

    def _watch_pause(self) -> None:
        """主播停下来超过阈值时立即提醒一次，再开口后重新计时。"""
        if self._last_speech_end is None or self._speech_since is not None or self._pause_alerted or self._no_signal:
            return
        silence = self.audio_time - self._last_speech_end
        if silence >= self.framework.pace.max_pause_seconds:
            self._pause_alerted = True
            self._alert(
                Finding(
                    key="pause_now",
                    kind="long_pause",
                    severity="medium",
                    title="冷场了",
                    detail=f"已经 {silence:.0f} 秒没有人说话。",
                    suggestion="接着讲产品细节，或者和观众互动",
                ),
                time=self._last_speech_end,
                source="realtime",
            )

    # —— 窗口分析 ——

    def _run_due_windows(self) -> None:
        window, interval = self.options.window_seconds, self.options.interval_seconds
        while self.audio_time >= self._next_due:
            end = self._next_due
            self._next_due += interval
            self._report(max(0.0, end - window), end, full=end >= window - 1e-6)

    def _report(self, start: float, end: float, *, full: bool) -> WindowReport:
        report = self._analyzer.analyze(self._history, start, end, full=full, speech_since=self._speech_since)
        report.session_id = self.session_id
        report.created_at = now_text()
        report.id = self._save(lambda store: store.add_report(report))
        self._last_report_end = end
        for minute in report.metrics.minute_rates:
            self._minute_rates[int(minute.start // 60)] = minute.rate
        payload = report.model_dump()
        with self._lock:
            self._reports.append(payload)
        self._emit("report", report=payload)
        self._notify(report)
        self._maybe_review(report)
        return report

    def _notify(self, report: WindowReport) -> None:
        """只提醒新出现的问题；违规和冷场已经即时提醒过，这里不重复。"""
        keys = {finding.key for finding in report.findings}
        for finding in report.findings:
            if finding.kind in ("forbidden", "long_pause") or finding.key in self._active_keys:
                continue
            self._alert(finding, time=report.end, source="window", report_id=report.id)
        self._active_keys = keys

    def _maybe_review(self, report: WindowReport) -> None:
        if self._reviewer_pool is None:
            return
        self._window_count += 1
        if (self._window_count - 1) % max(1, self.options.review_every):
            return
        # 实时来源跟不上就跳过本轮，免得审核越积越旧；分析文件时不赶时间，排队逐个审核。
        if self._live and self._review_future is not None and not self._review_future.done():
            self._emit(
                "llm", report_id=report.id, review=LlmReview(status="skipped", error="上一轮审核还没完成").model_dump()
            )
            return
        segments = [
            item.segment
            for item in self._history
            if item.segment.text and item.segment.end > report.start and item.segment.start < report.end
        ]
        if segments:
            self._review_future = self._reviewer_pool.submit(self._review, report, segments)

    def _review(self, report: WindowReport, segments: list[Segment]) -> None:
        assert self._reviewer is not None
        try:
            review = self._reviewer.review(self.framework, segments, report)
        except Exception as exc:
            log.exception("llm review failed")
            review = LlmReview(status="failed", error=str(exc) or type(exc).__name__)
        report.llm = review
        if report.id is not None:
            self._save(lambda store: store.set_report_llm(report.id, review))
        payload = review.model_dump()
        with self._lock:  # 界面重连时从快照恢复，审核结果也要在里面
            for item in self._reports:
                if item["id"] == report.id and item["end"] == report.end:
                    item["llm"] = payload
        self._emit("llm", report_id=report.id, review=payload)
        current: set[str] = set()
        for issue in review.issues:
            # 引了原话却在转写里找不到，多半是模型编的，只留在报告里不提醒。
            if issue.severity == "low" or (issue.quote and not issue.verified):
                continue
            segment = _quote_segment(issue.quote, segments)
            if segment is not None and self._repeats_rule_hit(issue.quote, segment):
                continue  # 原话里的违规词规则已经即时提醒过
            if segment is None and _mentions_flagged_point(issue.problem, report, self.framework):
                continue  # 规则已经报了这个要点没讲或讲得少，模型又说了一遍
            # 同一句原话会留在之后几个窗口里被反复审到，只提醒一次；
            # 没有原话的问题按类别提醒，连续几轮都有时不重复，消失后再出现才再提醒。
            key = f"quote:{segment.id}:{comparable(issue.quote)}" if segment else f"category:{issue.category}"
            current.add(key)
            if key in self._llm_seen:
                continue
            evidence = []
            if segment is not None:
                evidence.append(
                    Evidence(
                        segment_id=segment.id,
                        text=issue.quote,
                        sentence=segment.text,
                        start=segment.start,
                        end=segment.end,
                    )
                )
            self._alert(
                Finding(
                    key=key,
                    kind="llm",
                    severity=issue.severity,
                    title=_shorten(issue.title or issue.problem, 24),
                    detail=f"「{issue.quote}」{issue.problem}" if issue.quote else issue.problem,
                    suggestion=issue.suggestion,
                    evidence=evidence,
                ),
                time=segment.start if segment else report.end,
                source="llm",
                report_id=report.id,
            )
        self._llm_seen = {key for key in self._llm_seen if key.startswith("quote:")} | current

    def _repeats_rule_hit(self, quote: str, segment: Segment) -> bool:
        """大模型引用的原话里含有规则已经抓到的违规表达。"""
        target = comparable(quote)
        hits = self._analyzer.inspect(segment).forbidden_hits
        return any(comparable(segment.text[hit.start : hit.end]) in target for hit in hits)

    def _finish(self) -> None:
        """收尾：补一份最后不满一个周期的窗口报告，再对全场做总结。"""
        status = "failed" if self.state == "failed" else "stopped"
        try:
            tail = self.audio_time - self._last_report_end
            if self._history and tail >= min(10.0, self.options.interval_seconds):
                end = self.audio_time
                self._report(max(0.0, end - self.options.window_seconds), end, full=end >= self.options.window_seconds)
            if self._history:
                summary = self._analyzer.analyze(self._history, 0.0, self.audio_time, full=True, final=True)
                summary.session_id = self.session_id
                summary.created_at = now_text()
                self.summary = summary
                self._emit("summary", report=summary.model_dump())
        except Exception as exc:
            log.exception("final analysis failed")
            self.error = self.error or f"收尾分析失败：{exc}"
            status = "failed"
        if self._reviewer_pool is not None:
            # 最后一轮审核在后台跑完，结果晚到时照常保存和推送；wait() 会等它。
            self._reviewer_pool.shutdown(wait=False)
        self._save(
            lambda store: store.finish_session(
                self.session_id, status=status, duration=self.audio_time, summary=self.summary, error=self.error
            )
        )
        with self._lock:
            self._set_state(status)

    # —— 提醒与事件 ——

    def _alert(self, finding: Finding, *, time: float, source: str, report_id: int | None = None) -> None:
        self._publish_alert(
            Alert(
                time=time,
                source=source,
                kind=finding.kind,
                severity=finding.severity,
                title=finding.title,
                detail=finding.detail,
                suggestion=finding.suggestion,
                evidence=finding.evidence,
                report_id=report_id,
            )
        )

    def _system_alert(self, kind: str, severity: str, title: str, detail: str, suggestion: str) -> None:
        self._publish_alert(
            Alert(
                time=self.audio_time,
                source="system",
                kind=kind,
                severity=severity,
                title=title,
                detail=detail,
                suggestion=suggestion,
            )
        )

    def _publish_alert(self, alert: Alert, *, save: bool = True) -> None:
        alert.session_id = self.session_id
        alert.time = round(alert.time, 2)
        alert.created_at = now_text()
        with self._lock:
            if save:
                alert.id = self._save(lambda store: store.add_alert(alert))
            payload = alert.model_dump()
            self._alerts.append(payload)
        self._emit("alert", alert=payload)

    def _fail(self, message: str) -> None:
        with self._lock:
            if self.state == "failed":
                return
            self.error = message
            self._set_state("failed")
        self._stop.set()
        self._ready.set()

    def _set_state(self, state: State) -> None:
        self.state = state
        self._emit("state", state=state, error=self.error, engine=self.engine)

    def _emit(self, kind: str, **payload: Any) -> None:
        try:
            self._emit_event({"type": kind, "session_id": self.session_id, **payload})
        except Exception:
            log.exception("emitting event failed")

    def _due(self, name: str, seconds: float) -> bool:
        """界面类事件按墙上时间限流，文件高速回放时不至于刷屏。"""
        now = time.monotonic()
        if now - self._throttle.get(name, -1e9) < seconds:
            return False
        self._throttle[name] = now
        return True

    def _save(self, action: Callable[[SessionStore], Any]) -> Any:
        if self._store is None:
            return None
        try:
            return action(self._store)
        except Exception:
            log.exception("saving session data failed")
            if not self._store_failed:
                self._store_failed = True
                alert = Alert(
                    time=self.audio_time,
                    source="system",
                    kind="storage",
                    severity="high",
                    title="记录保存失败",
                    detail="本场数据可能没有完整保存到历史记录，实时提醒不受影响。",
                )
                self._publish_alert(alert, save=False)
            return None

    # —— 查询 ——

    def health(self) -> dict[str, Any]:
        return {
            "audio_time": round(self.audio_time, 2),
            "backlog_seconds": round(self._queue.qsize() * BLOCK_SECONDS, 1),
            "dropped_seconds": round(self._dropped_samples / SAMPLE_RATE, 1),
            "signal": not self._no_signal,
        }

    def snapshot(self) -> dict[str, Any]:
        """界面刷新或重连时恢复现场所需的全部状态。"""
        with self._lock:
            return {
                "session_id": self.session_id,
                "title": self.title,
                "paced": self.paced,
                "state": self.state,
                "error": self.error,
                "started_at": self.started_at,
                "framework": {
                    "id": self.framework.id,
                    "name": self.framework.name,
                    "pace": self.framework.pace.model_dump(),
                },
                "source": self.source.label,
                "engine": self.engine,
                "window_seconds": self.options.window_seconds,
                "interval_seconds": self.options.interval_seconds,
                "next_report_at": self._next_due,
                "partial": self._partial,
                "segments": list(self._segments),
                "alerts": list(self._alerts),
                "reports": list(self._reports),
                "minute_rates": [
                    {"minute": minute, "rate": rate} for minute, rate in sorted(self._minute_rates.items())
                ],
                "summary": self.summary.model_dump() if self.summary else None,
                "health": self.health(),
            }


def _shorten(text: str, limit: int) -> str:
    """提醒标题：太长时尽量在标点处截断，否则硬截并加省略号。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = max(text.rfind(mark, 0, limit) for mark in "，。；：,;")
    return text[:cut] if cut >= limit // 2 else text[: limit - 1] + "…"


def _mentions_flagged_point(problem: str, report: WindowReport, framework: Framework) -> bool:
    """问题描述提到了规则已判定“没讲 / 讲得少”的必讲要点（要点名或任一关键词）。"""
    flagged = {
        item.point_id for item in report.coverage if item.required and item.status in ("missing", "insufficient")
    }
    if not flagged:
        return False
    text = comparable(problem)
    for _, point in framework.points():
        if point.id in flagged and any(comparable(word) in text for word in [point.name, *point.keywords] if word):
            return True
    return False


def _quote_segment(quote: str, segments: list[Segment]) -> Segment | None:
    """大模型引用的原话出自哪一句；跨句或找不到时返回 None。"""
    target = comparable(quote)
    if not target:
        return None
    return next((segment for segment in segments if target in comparable(segment.text)), None)
