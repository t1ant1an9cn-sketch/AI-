import { describe, expect, it } from "vitest";

import { applyEvent, initialLive, type LiveState } from "./live";
import type { Snapshot, WindowReport } from "./types";

const PACE = { min_rate: 150, max_rate: 330, max_pause_seconds: 8, min_speech_ratio: 0.5, max_fillers_per_minute: 8 };

function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    session_id: "s1",
    title: "测试",
    paced: true,
    recording: null,
    state: "running",
    error: "",
    started_at: "2026-10-17T20:00:00+08:00",
    framework: { id: "fw", name: "框架", pace: PACE },
    source: "演示音频",
    engine: "SenseVoice（本地）",
    window_seconds: 300,
    interval_seconds: 60,
    next_report_at: 60,
    partial: "",
    segments: [],
    alerts: [],
    reports: [],
    minute_rates: [],
    summary: null,
    health: { audio_time: 0, backlog_seconds: 0, dropped_seconds: 0, signal: true },
    ...overrides,
  };
}

function report(overrides: Partial<WindowReport> = {}): WindowReport {
  return {
    id: 1,
    session_id: "s1",
    start: 0,
    end: 120,
    full: true,
    final: false,
    created_at: "",
    metrics: {
      window_seconds: 120,
      speech_seconds: 30,
      speech_ratio: 0.25,
      syllables: 100,
      rate: 200,
      segment_count: 1,
      pauses: [],
      longest_pause: 0,
      fillers: {},
      fillers_per_minute: 0,
      minute_rates: [{ start: 60, end: 120, speech_seconds: 30, rate: 200 }],
    },
    coverage: [],
    forbidden: [],
    findings: [],
    llm: null,
    ...overrides,
  };
}

const withSession = (session: Snapshot): LiveState => ({ ...initialLive, session });

describe("applyEvent", () => {
  it("忽略其他场次的事件", () => {
    const state = withSession(snapshot());
    const next = applyEvent(state, {
      type: "partial",
      session_id: "another",
      text: "别的场次",
      start: 0,
      end: 1,
    });
    expect(next).toBe(state);
  });

  it("定稿句子加入转写，并清空正在识别的文字", () => {
    const state = withSession(snapshot({ partial: "正在说" }));
    const next = applyEvent(state, {
      type: "segment",
      session_id: "s1",
      segment: { id: 1, start: 1, end: 3, text: "欢迎", emotion: null },
      highlights: [],
    });
    expect(next.session?.segments.map((line) => line.segment.text)).toEqual(["欢迎"]);
    expect(next.session?.partial).toBe("");
  });

  it("窗口报告更新下次分析时间和每分钟语速", () => {
    const next = applyEvent(withSession(snapshot()), { type: "report", session_id: "s1", report: report() });
    expect(next.session?.reports).toHaveLength(1);
    expect(next.session?.next_report_at).toBe(180);
    expect(next.session?.minute_rates).toEqual([{ minute: 1, rate: 200 }]);
  });

  it("提醒和状态变化都会合并进当前场次", () => {
    let state = withSession(snapshot());
    state = applyEvent(state, {
      type: "alert",
      session_id: "s1",
      alert: {
        id: 1,
        session_id: "s1",
        time: 5,
        created_at: "",
        source: "realtime",
        kind: "forbidden",
        severity: "high",
        title: "出现违规表达",
        detail: "全网最低",
        suggestion: "",
        evidence: [],
        report_id: null,
      },
    });
    state = applyEvent(state, { type: "state", session_id: "s1", state: "stopped", error: "", engine: "" });
    expect(state.session?.alerts.map((alert) => alert.kind)).toEqual(["forbidden"]);
    expect(state.session?.state).toBe("stopped");
    expect(state.session?.engine).toBe("SenseVoice（本地）"); // 事件没带引擎名时保留原值
  });

  it("转写只保留最近 400 句", () => {
    let state = withSession(snapshot());
    for (let index = 1; index <= 405; index += 1) {
      state = applyEvent(state, {
        type: "segment",
        session_id: "s1",
        segment: { id: index, start: index, end: index + 1, text: `第${index}句`, emotion: null },
        highlights: [],
      });
    }
    const lines = state.session!.segments;
    expect(lines).toHaveLength(400);
    expect(lines[0].segment.id).toBe(6);
    expect(lines[lines.length - 1].segment.id).toBe(405);
  });
});
