"""把一场盯播整理成 Markdown 复盘报告：总体指标、待改进问题、要点覆盖、提醒记录和完整转写。"""

from __future__ import annotations

from typing import Any

SEVERITY = {"high": "高", "medium": "中", "low": "低"}
STATUS = {"covered": "✅ 已讲", "insufficient": "⚠️ 不够", "missing": "❌ 没讲", "unchecked": "— 无关键词"}
SESSION_STATUS = {"running": "进行中", "stopped": "已结束", "failed": "异常结束", "interrupted": "已中断"}


def clock(seconds: float | None) -> str:
    """会话内时间：75 → 01:15，3725 → 1:02:05。"""
    if seconds is None:
        return "—"
    hours, rest = divmod(round(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def duration_text(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [
        f"{hours} 小时" if hours else "",
        f"{minutes} 分" if minutes else "",
        f"{secs} 秒" if secs or not total else "",
    ]
    return " ".join(part for part in parts if part)


def session_markdown(detail: dict[str, Any]) -> str:
    """detail 的结构与 Storage.get_session 的返回值一致。"""
    summary = detail.get("summary") or {}
    lines = [
        f"# 盯播复盘：{detail['title']}",
        "",
        f"- 开始时间：{detail['started_at'].replace('T', ' ')[:19]}",
        f"- 时长：{duration_text(detail['duration'])}（{SESSION_STATUS.get(detail['status'], detail['status'])}）",
        f"- 话术框架：{detail['framework_name']}",
        f"- 音频来源：{detail['source']}",
        f"- 识别引擎：{detail['engine'] or '—'}",
        f"- 分析方式：每 {detail['interval_seconds']:g} 秒分析过去 {detail['window_seconds']:g} 秒",
    ]
    if detail.get("error"):
        lines.append(f"- 异常：{detail['error']}")
    if summary:
        lines += ["", "## 总体情况", "", *_metric_lines(summary, detail["framework"])]
        findings = summary.get("findings", [])
        lines += ["", "## 需要改进", ""]
        if findings:
            order = {"high": 0, "medium": 1, "low": 2}
            for index, finding in enumerate(sorted(findings, key=lambda f: order[f["severity"]]), 1):
                line = f"{index}. 【{SEVERITY[finding['severity']]}】{finding['title']}：{finding['detail']}"
                if finding.get("suggestion"):
                    line += f" 建议：{finding['suggestion']}"
                lines.append(line)
        else:
            lines.append("没有发现问题。")
        lines += [
            "",
            "## 话术要点覆盖",
            "",
            "| 环节 | 要点 | 状态 | 次数 | 首次讲到 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for item in summary.get("coverage", []):
            name = item["point_name"] + ("" if item["required"] else "（选讲）")
            cells = [item["stage_name"], name, STATUS[item["status"]], str(item["mentions"]), clock(item["first_at"])]
            lines.append(f"| {' | '.join(cells)} |")
    alerts = detail.get("alerts", [])
    lines += ["", "## 提醒记录", ""]
    if alerts:
        lines += ["| 时间 | 级别 | 提醒 | 说明 |", "| --- | --- | --- | --- |"]
        for alert in alerts:
            detail_text = alert["detail"].replace("|", "｜")
            lines.append(
                f"| {clock(alert['time'])} | {SEVERITY[alert['severity']]} | {alert['title']} | {detail_text} |"
            )
    else:
        lines.append("没有提醒。")
    lines += ["", "## 完整转写", ""]
    lines += [f"[{clock(segment['start'])}] {segment['text']}" for segment in detail.get("segments", [])] or [
        "（没有识别到讲话）"
    ]
    return "\n".join(lines) + "\n"


def _metric_lines(summary: dict[str, Any], framework: dict[str, Any]) -> list[str]:
    metrics = summary["metrics"]
    pace = framework.get("pace", {})
    rate = f"约 {metrics['rate']:.0f} 字/分钟" if metrics["rate"] is not None else "说话太少，无法评价"
    pauses = metrics["pauses"]
    fillers = "、".join(f"{word}×{count}" for word, count in metrics["fillers"].items()) or "无"
    return [
        f"- 语速：{rate}（建议 {pace.get('min_rate', 150):g}–{pace.get('max_rate', 330):g}）",
        f"- 说话时间占比：{metrics['speech_ratio']:.0%}",
        f"- 冷场：{len(pauses)} 次超过 {pace.get('max_pause_seconds', 8):g} 秒"
        + (f"，最长 {metrics['longest_pause']:.0f} 秒" if pauses else ""),
        f"- 口头禅：{fillers}",
        f"- 违规表达：{len(summary.get('forbidden', []))} 次",
    ]
