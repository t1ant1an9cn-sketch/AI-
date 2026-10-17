"""大模型语义审核：把一个窗口的转写和话术框架交给大模型，找出关键词匹配发现不了的问题，
例如价格说错、卖点讲得含糊、夸大宣传换了说法。

使用兼容 OpenAI 的 /chat/completions 接口，可以接通义千问（DashScope 兼容模式）、DeepSeek、
Kimi、智谱、OpenAI，或本机运行的 Ollama。模型给出的引文会回到转写里逐字核对，对不上的标为未核实。

百炼上的千问 3 系列默认开启深度思考，实测一次审核从约 13 秒变成约 70 秒，赶不上每分钟一次的分析，
所以对百炼地址固定关闭深度思考。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from ..frameworks import Framework
from .report import LlmIssue, LlmPoint, LlmReview, Segment, WindowReport
from .text import comparable

SYSTEM_PROMPT = """你是直播带货的话术质检员。根据话术框架检查主播最近一段讲解，找出需要改进的地方。
要求：
1. 只依据给出的转写判断，不要编造。转写由语音识别生成，常有同音字、错别字、漏字；
   疑似识别错误的地方不是主播的问题，不要当成主播说错（实在要提，category 写“识别错误”）。
2. 关注关键词规则发现不了的问题：信息说错（价格、规格、赠品与框架不符）、卖点讲得含糊、
   换了说法的夸大或违规承诺、讲解混乱、长时间偏离产品。
3. “规则已经发现的问题”里列出的，不要在 issues 里重复。
4. quote 必须是转写里逐字出现过的原话；没有对应原话时留空。
只输出一个 JSON 对象，格式如下：
{"summary": "一两句话概括这段讲解",
 "points": [{"point_id": "要点 ID", "status": "covered 或 partial 或 missing", "comment": "简短说明"}],
 "issues": [{"category": "信息错误/违规风险/表达/节奏/识别错误/其他", "severity": "high 或 medium 或 low",
             "title": "15 字以内的标题", "quote": "原话", "problem": "问题是什么", "suggestion": "怎么改"}]}
points 覆盖框架里的每个要点；没有问题时 issues 为空数组。"""


# “识别可能存在误差”“识别出错”“疑似识别……”这类说法讲的是语音识别，不是主播。
_ASR_NOTE = re.compile(r"识别.{0,6}(错|误|偏差|不准)|疑似(识别|转写)|转写(错误|有误)")


class LlmError(RuntimeError):
    pass


def build_prompt(framework: Framework, segments: list[Segment], report: WindowReport) -> str:
    lines = [f"## 话术框架：{framework.name}"]
    if framework.description:
        lines.append(framework.description)
    lines.append("### 环节与要点")
    for stage, point in framework.points():
        detail = point.description or "（无描述）"
        keywords = f"（关键词：{'、'.join(point.keywords)}）" if point.keywords else ""
        required = "" if point.required else "（选讲）"
        lines.append(f"- [{point.id}] {stage.name}／{point.name}{required}：{detail}{keywords}")
    if framework.forbidden:
        lines.append("### 不能说的话")
        lines += [f"- {rule.name}：{'、'.join(rule.keywords)}" for rule in framework.forbidden]
    if framework.llm_instructions:
        lines += ["### 额外要求", framework.llm_instructions]
    if report.findings:
        lines.append("## 规则已经发现的问题（不要重复）")
        lines += [f"- {finding.title}" for finding in report.findings]
    lines.append(f"## 主播讲解（第 {_clock(report.start)} 到 {_clock(report.end)}）")
    lines += [f"[{_clock(segment.start)}] {segment.text}" for segment in segments if segment.text]
    return "\n".join(lines)


class LlmReviewer:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("请填写大模型接口地址")
        if not model.strip():
            raise ValueError("请填写模型名称")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        host = urlparse(self.url).hostname or ""
        # 本机服务（如 Ollama）不走系统代理：Windows 上 httpx 会读取系统代理设置，连 127.0.0.1 也转发过去。
        local = host in ("localhost", "127.0.0.1", "::1")
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), trust_env=not local)
        # 可选参数：服务不认识时返回 400，自动去掉后重试，以后也不再发送。
        self._optional: dict[str, Any] = {"response_format": {"type": "json_object"}}
        if host.endswith("dashscope.aliyuncs.com"):
            self._optional["enable_thinking"] = False

    def review(self, framework: Framework, segments: list[Segment], report: WindowReport) -> LlmReview:
        started = time.monotonic()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(framework, segments, report)},
        ]
        content = self._complete(messages, temperature=0.2)
        review = parse_review(content, framework, segments)
        review.model = self.model
        review.seconds = round(time.monotonic() - started, 1)
        return review

    def ping(self) -> str:
        """连通性测试：发一条很短的请求，返回模型的回复。"""
        return self._complete([{"role": "user", "content": "请只回复“可以连接”四个字。"}], max_tokens=20, json=False)

    def _complete(self, messages: list[dict[str, str]], *, json: bool = True, **options: Any) -> str:
        optional = {key: value for key, value in self._optional.items() if json or key != "response_format"}
        payload: dict[str, Any] = {"model": self.model, "messages": messages, **optional, **options}
        try:
            response = self._client.post(self.url, json=payload, headers=self._headers)
            rejected = [key for key in optional if response.status_code == 400 and key in response.text]
            if rejected:
                for key in rejected:
                    payload.pop(key)
                    self._optional.pop(key)
                response = self._client.post(self.url, json=payload, headers=self._headers)
        except httpx.TimeoutException:
            raise LlmError("大模型响应超时") from None
        except httpx.TransportError as exc:
            raise LlmError(f"连不上大模型接口：{exc}") from None
        if response.status_code >= 400:
            raise LlmError(_http_error(response))
        try:
            return response.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise LlmError("大模型返回的内容格式不对") from None


def parse_review(content: str, framework: Framework, segments: list[Segment]) -> LlmReview:
    """解析模型输出；字段不全或取值不规范时尽量保留能用的部分。"""
    data = _extract_json(content)
    if data is None:
        return LlmReview(status="failed", error="大模型没有按要求返回 JSON")
    transcript = comparable("".join(segment.text for segment in segments))
    point_ids = {point.id for _, point in framework.points()}
    points = []
    for item in _list(data.get("points")):
        try:
            point = LlmPoint.model_validate(item)
        except ValidationError:
            continue
        if point.point_id in point_ids:
            points.append(point)
    issues = []
    for item in _list(data.get("issues")):
        if not isinstance(item, dict) or not item.get("problem"):
            continue
        category = str(item.get("category") or "其他")
        problem = str(item["problem"])
        # 模型即使被要求，有时仍会把识别出的错字当成问题；这不是主播的问题，丢掉。
        if "识别" in category or _ASR_NOTE.search(problem):
            continue
        quote = str(item.get("quote") or "").strip()
        issues.append(
            LlmIssue(
                category=category,
                severity=item.get("severity") if item.get("severity") in ("high", "medium", "low") else "medium",
                title=str(item.get("title") or "").strip(),
                quote=quote,
                problem=problem,
                suggestion=str(item.get("suggestion") or ""),
                verified=bool(quote) and comparable(quote) in transcript,
            )
        )
    return LlmReview(status="done", summary=str(data.get("summary") or ""), points=points, issues=issues)


def _extract_json(content: str) -> dict[str, Any] | None:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _http_error(response: httpx.Response) -> str:
    """把错误状态码换成中文说明，并附上服务端给的原因（例如百炼免费额度用完时返回 403）。"""
    status = response.status_code
    reason = _service_message(response)
    if status == 403 and "quota" in reason.lower():
        summary = "额度已用完：请到服务商控制台充值，或关闭“仅使用免费额度”"
    elif status in (401, 403):
        summary = "API Key 无效或没有权限"
    elif status == 404:
        summary = "接口地址或模型名称不对（404）"
    elif status == 429:
        summary = "请求太频繁或额度不足（429）"
    else:
        summary = f"大模型接口返回 {status}"
    return f"{summary}。服务端原因：{reason}" if reason else summary


def _service_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text.strip()[:200]
    error = data.get("error") if isinstance(data, dict) else None
    message = (
        error.get("message") if isinstance(error, dict) else (data.get("message") if isinstance(data, dict) else "")
    )
    return str(message or "").strip()[:200]


def _clock(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"
