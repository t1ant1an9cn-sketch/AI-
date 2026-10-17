"""分析结果的数据结构：转写句子、窗口指标、要点覆盖、违规命中、问题和大模型审核。

时间字段都是会话音频时间（秒，从开始监控算起），界面再换算成钟点。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..frameworks import Severity


def now_text() -> str:
    """当前本地时间（带时区），用于各条记录的 created_at。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Segment(BaseModel):
    """一句定稿的转写。"""

    id: int
    start: float
    end: float
    text: str
    emotion: str | None = None  # 识别引擎给出的情绪标签（如有）


class Highlight(BaseModel):
    """转写里需要标出的一段文字，供界面高亮。"""

    start: int
    end: int
    kind: Literal["point", "forbidden", "filler"]
    ref: str  # 要点 ID、违规规则 ID 或口头禅本身
    label: str
    exact: bool = True


class Evidence(BaseModel):
    """原文证据：命中的那几个字，以及它所在句子的时间。"""

    segment_id: int
    text: str
    sentence: str
    start: float
    end: float
    exact: bool = True  # False 表示按同音命中，可能是识别把字认错了


class PointCoverage(BaseModel):
    stage_id: str
    stage_name: str
    point_id: str
    point_name: str
    required: bool
    min_mentions: int
    mentions: int
    status: Literal["covered", "insufficient", "missing", "unchecked"]
    evidence: list[Evidence] = Field(default_factory=list)
    first_at: float | None = None
    last_at: float | None = None


class ForbiddenHit(BaseModel):
    rule_id: str
    rule_name: str
    severity: Severity
    keyword: str
    evidence: Evidence


class Pause(BaseModel):
    start: float
    end: float
    seconds: float


class MinuteRate(BaseModel):
    """按会话整分钟统计的语速，用于画趋势。"""

    start: float
    end: float
    speech_seconds: float
    rate: float | None  # 说话不足时为空


class Metrics(BaseModel):
    window_seconds: float
    speech_seconds: float
    speech_ratio: float
    syllables: int
    rate: float | None  # 字/分钟，只按说话时间算；说话太少时为空
    segment_count: int
    pauses: list[Pause]  # 超过阈值的停顿
    longest_pause: float
    fillers: dict[str, int]
    fillers_per_minute: float
    minute_rates: list[MinuteRate]


FindingKind = Literal[
    "missing_point",
    "few_mentions",
    "forbidden",
    "rate_fast",
    "rate_slow",
    "rate_spike",
    "long_pause",
    "low_speech",
    "fillers",
    "order",
    "llm",
]


class Finding(BaseModel):
    """一个需要提醒主播的问题。"""

    key: str  # 同一问题在相邻窗口里的标识，用来避免重复提醒
    kind: FindingKind
    severity: Severity
    title: str
    detail: str
    suggestion: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


AlertSource = Literal["realtime", "window", "llm", "system"]


class Alert(BaseModel):
    """推送给主播或运营的一条提醒。"""

    id: int | None = None
    session_id: str = ""
    time: float  # 会话音频时间
    created_at: str = ""
    source: AlertSource  # 实时检查、窗口分析、大模型审核或系统状态
    kind: str
    severity: Severity
    title: str
    detail: str
    suggestion: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    report_id: int | None = None


class LlmPoint(BaseModel):
    point_id: str
    status: Literal["covered", "partial", "missing"]
    comment: str = ""


class LlmIssue(BaseModel):
    category: str
    severity: Severity
    title: str = ""  # 十几个字的短标题，用作提醒标题
    quote: str = ""
    problem: str
    suggestion: str = ""
    verified: bool = False  # 引文确实出现在转写里


class LlmReview(BaseModel):
    """大模型对一个窗口的语义审核结果。"""

    status: Literal["done", "failed", "skipped"]
    model: str = ""
    summary: str = ""
    points: list[LlmPoint] = Field(default_factory=list)
    issues: list[LlmIssue] = Field(default_factory=list)
    error: str = ""
    seconds: float = 0.0


class WindowReport(BaseModel):
    """一次窗口分析的完整结果。"""

    id: int | None = None
    session_id: str = ""
    start: float
    end: float
    full: bool  # 窗口已达到设定长度；未满时不报“没讲到”
    final: bool = False  # 会话结束时对全程的总结
    created_at: str = ""
    metrics: Metrics
    coverage: list[PointCoverage]
    forbidden: list[ForbiddenHit]
    findings: list[Finding]
    llm: LlmReview | None = None
