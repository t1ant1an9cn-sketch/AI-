import json

import httpx
import pytest

from voicedetect.analysis.llm import LlmError, LlmReviewer, build_prompt, parse_review
from voicedetect.analysis.report import Finding, Metrics, Segment, WindowReport
from voicedetect.frameworks import builtin_templates

FRAMEWORK = next(t for t in builtin_templates() if t.id == "tpl-thermos-cup")
SEGMENTS = [
    Segment(id=1, start=3, end=8, text="今天到手价只要89元，"),
    Segment(id=2, start=65, end=70, text="内胆是316不锈钢。"),
]


def report(start=0.0, end=90.0):
    metrics = Metrics(
        window_seconds=end - start,
        speech_seconds=10,
        speech_ratio=0.1,
        syllables=20,
        rate=None,
        segment_count=2,
        pauses=[],
        longest_pause=0,
        fillers={},
        fillers_per_minute=0,
        minute_rates=[],
    )
    return WindowReport(start=start, end=end, full=True, metrics=metrics, coverage=[], forbidden=[], findings=[])


def reviewer(handler, api_key="sk-test"):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return LlmReviewer(base_url="https://llm.test/v1/", api_key=api_key, model="qwen-plus", client=client)


def answer(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_prompt_contains_framework_rules_and_timed_transcript():
    prompt = build_prompt(FRAMEWORK, SEGMENTS, report())
    assert "[quote] 价格优惠／到手价：明确说出到手价 99 元" in prompt
    assert "- 绝对化用语：最好、最佳" in prompt
    assert "价格必须是 99 元" in prompt  # 框架里的额外要求
    assert "[00:03] 今天到手价只要89元，" in prompt and "[01:05] 内胆是316不锈钢。" in prompt
    assert "规则已经发现的问题" not in prompt


def test_prompt_lists_rule_findings_so_the_model_does_not_repeat_them():
    finding = Finding(key="missing:gift", kind="missing_point", severity="medium", title="没讲「赠品」", detail="")
    prompt = build_prompt(FRAMEWORK, SEGMENTS, report().model_copy(update={"findings": [finding]}))
    assert "## 规则已经发现的问题（不要重复）\n- 没讲「赠品」" in prompt


def test_dashscope_requests_turn_off_deep_thinking_and_others_do_not():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return answer('{"summary": "ok", "points": [], "issues": []}')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dashscope = LlmReviewer(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", api_key="k", model="qwen", client=client
    )
    dashscope.review(FRAMEWORK, SEGMENTS, report())
    dashscope.ping()
    reviewer(handler).review(FRAMEWORK, SEGMENTS, report())
    assert [body.get("enable_thinking") for body in bodies] == [False, False, None]
    assert ["response_format" in body for body in bodies] == [True, False, True]


def test_unsupported_thinking_switch_is_dropped_and_not_sent_again():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "enable_thinking" in body:
            return httpx.Response(400, text="unknown parameter: enable_thinking")
        return answer('{"summary": "ok", "points": [], "issues": []}')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = LlmReviewer(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", api_key="k", model="m", client=client
    )
    assert llm.review(FRAMEWORK, SEGMENTS, report()).summary == "ok"
    llm.review(FRAMEWORK, SEGMENTS, report())
    assert ["enable_thinking" in body for body in bodies] == [True, False, False]
    assert all("response_format" in body for body in bodies)


def test_review_parses_output_and_checks_quotes_against_transcript():
    content = """```json
    {"summary": "价格说错了",
     "points": [{"point_id": "quote", "status": "partial", "comment": "价格与框架不符"},
                {"point_id": "made-up", "status": "covered"}],
     "issues": [{"category": "信息错误", "severity": "high", "quote": "到手价只要89元", "problem": "价格应为 99 元",
                 "suggestion": "更正价格"},
                {"category": "表达", "severity": "urgent", "quote": "这句话没出现过", "problem": "编造的引文"},
                {"category": "节奏", "problem": "整体节奏偏慢"}]}
    ```"""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return answer(content)

    result = reviewer(handler).review(FRAMEWORK, SEGMENTS, report())
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "qwen-plus" and seen["body"]["response_format"] == {"type": "json_object"}
    assert (result.status, result.summary, result.model) == ("done", "价格说错了", "qwen-plus")
    assert [p.point_id for p in result.points] == ["quote"]  # 框架里没有的要点被丢掉
    assert [(i.severity, i.verified) for i in result.issues] == [("high", True), ("medium", False), ("medium", False)]


def test_recognition_errors_are_not_reported_as_the_hosts_problem():
    content = json.dumps(
        {
            "issues": [
                {"category": "识别错误", "severity": "medium", "quote": "白药石黑", "problem": "应为曜石黑"},
                # 真实模型的原话：分类写“表达”，内容在说识别误差
                {"category": "表达", "severity": "medium", "quote": "白药石黑", "problem": "语音识别可能存在误差"},
                {"category": "表达", "severity": "medium", "quote": "", "problem": "疑似识别出错的颜色名"},
                {
                    "category": "信息错误",
                    "severity": "high",
                    "title": "价格说错",
                    "quote": "89元",
                    "problem": "价格应为 99 元",
                },
            ]
        },
        ensure_ascii=False,
    )
    review = parse_review(content, FRAMEWORK, SEGMENTS)
    assert [(issue.title, issue.problem) for issue in review.issues] == [("价格说错", "价格应为 99 元")]


def test_json_mode_is_dropped_when_the_service_rejects_it():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, text="response_format is not supported")
        return answer('{"summary": "ok", "points": [], "issues": []}')

    llm = reviewer(handler)
    assert llm.review(FRAMEWORK, SEGMENTS, report()).summary == "ok"
    llm.review(FRAMEWORK, SEGMENTS, report())
    assert ["response_format" in body for body in bodies] == [True, False, False]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401, text="bad key"), "API Key 无效或没有权限"),
        (httpx.Response(404, text="no model"), "接口地址或模型名称不对（404）"),
        (httpx.Response(429, text="slow down"), "请求太频繁或额度不足（429）"),
        (httpx.Response(200, json={"unexpected": True}), "格式不对"),
    ],
)
def test_service_errors_become_readable_messages(response, message):
    with pytest.raises(LlmError, match=message):
        reviewer(lambda request: response).review(FRAMEWORK, SEGMENTS, report())


def test_exhausted_free_quota_is_not_reported_as_a_bad_key():
    # 百炼免费额度用完时的真实返回（2026-10-17）
    body = {
        "error": {
            "message": "Free quota exhausted. To continue accessing the model on a paid basis, please add funds "
            'or disable the "use free tier only" mode in the management console.',
            "type": "AllocationQuota.FreeTierOnly",
            "code": "AllocationQuota.FreeTierOnly",
        }
    }
    with pytest.raises(LlmError) as error:
        reviewer(lambda request: httpx.Response(403, json=body)).review(FRAMEWORK, SEGMENTS, report())
    assert str(error.value).startswith("额度已用完")
    assert "服务端原因：Free quota exhausted" in str(error.value)
    assert "API Key 无效" not in str(error.value)


def test_timeout_is_reported():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LlmError, match="超时"):
        reviewer(handler).review(FRAMEWORK, SEGMENTS, report())


def test_non_json_answer_fails_softly():
    review = parse_review("抱歉，我无法完成。", FRAMEWORK, SEGMENTS)
    assert review.status == "failed" and "JSON" in review.error


def test_local_models_work_without_api_key():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return answer("可以连接")

    assert reviewer(handler, api_key="").ping() == "可以连接"
    assert seen["auth"] is None
