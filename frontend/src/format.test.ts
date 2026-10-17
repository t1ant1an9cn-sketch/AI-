import { describe, expect, it } from "vitest";

import { clock, duration, percent, span } from "./format";

describe("时间格式", () => {
  it("会话内时间：分秒，超过一小时带小时", () => {
    expect(clock(75)).toBe("01:15");
    expect(clock(3725)).toBe("1:02:05");
    expect(clock(-3)).toBe("00:00");
    expect(clock(null)).toBe("—");
  });

  it("时长用中文口语", () => {
    expect(duration(0)).toBe("0 秒");
    expect(duration(95)).toBe("1 分 35 秒");
    expect(duration(3600)).toBe("1 小时");
    expect(duration(3725)).toBe("1 小时 2 分");
  });

  it("分析窗口：整分钟说分钟，其余说秒", () => {
    expect(span(60)).toBe("1 分钟");
    expect(span(90)).toBe("90 秒");
    expect(span(300)).toBe("5 分钟");
  });

  it("占比四舍五入到整数百分比", () => {
    expect(percent(0.706)).toBe("71%");
  });
});
